"""Claude-native delivery must not reap a terminal awaiting startup input.

Claude-native message delivery must not treat a terminal that is *alive and
waiting for human startup input* as a failed boot. When the runner auto-launches
Claude Code and the terminal stalls at a startup screen that needs the user --
a configured launcher's ``Password:`` prompt, an OAuth ``Waiting for OAuth
callback`` browser sign-in, or a Claude confirmation dialog -- the first web-UI
message hits the readiness deadline. With the bug present,
``omnigent.harnesses.claude_native.bridge._wait_for_claude_prompt_ready`` raises
``ClaudePromptTimeout`` (it only looks for Claude's ``❯`` input glyph, which a
startup screen never renders), and
``omnigent.inner.claude_native_executor.ClaudeNativeExecutor.run_turn``
unconditionally passes that through ``_reap_failed_turn`` -- which ``kill``\\ s
the tmux session. The healthy terminal the user was about to complete startup on
is destroyed, the send is lost, and there is no pane left to retry against.

Expected (the fix's target): a startup screen awaiting input is recognized as a
*recoverable* delivery state -- the send still reports an error (so the user is
told to complete startup), but the pane is **preserved** so they can finish the
password/OAuth/confirmation and send again. Empty captures and genuine boot
crashes must still be reaped.

This test drives the **real** bridge + **real** executor against a **real** tmux
pane, so it exercises the exact production path (``run_turn`` ->
``inject_user_message`` -> ``_wait_for_claude_prompt_ready`` -> reap), not a
mock. The only seam is a shortened readiness deadline so the timeout fires
promptly instead of after the 30s production budget.

Red -> green: with the bug present, the startup-prompt panes are killed and
:func:`test_startup_input_prompt_pane_is_preserved` fails; the fix preserves
them. :func:`test_empty_capture_boot_failure_is_still_reaped` is a control that
passes both before and after so an over-broad fix (preserving *everything*) is
caught.

Runs without credentials, a real Claude login, or a live server -- only a
``tmux`` binary is required.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native terminal reproduction needs a real tmux binary",
)

# Shortened readiness deadline. The production default is 30s
# (``_TMUX_READY_TIMEOUT_S``); the report reproduces "with a zero deadline".
# A few seconds is enough for the readiness gate to poll the pane, see no
# Claude input box, and time out -- while keeping the test fast.
_READY_DEADLINE_S = 3.0


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``tmux -S <socket> <args...>`` and return the completed process."""
    return subprocess.run(
        ["tmux", "-S", socket, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _session_alive(socket: str, tmux_target: str) -> bool:
    """Return whether the tmux session backing *tmux_target* still exists."""
    session = tmux_target.split(":", 1)[0]
    return _tmux(socket, "has-session", "-t", session).returncode == 0


class _RealTerminal:
    """A real tmux session standing in for the runner's Claude pane.

    Launches *pane_command* on its own per-session socket and advertises the
    pane exactly as the runner does (``tmux.json`` in the bridge dir), so the
    bridge's real ``_capture_pane`` / ``inject_user_message`` / ``kill_session``
    operate on it unchanged.
    """

    def __init__(self, root: Path, pane_command: str) -> None:
        self.bridge_dir = root / "bridge"
        self.bridge_dir.mkdir()
        self.socket = str(root / "tmux.sock")
        self.session_name = f"claude_{uuid.uuid4().hex[:8]}"
        self.tmux_target = f"{self.session_name}:0.0"
        _tmux(
            self.socket,
            "new-session",
            "-d",
            "-s",
            self.session_name,
            "-x",
            "120",
            "-y",
            "40",
            pane_command,
        )
        # Let the pane render its startup screen before it is advertised.
        time.sleep(0.6)
        (self.bridge_dir / "tmux.json").write_text(
            f'{{"socket_path": "{self.socket}", "tmux_target": "{self.tmux_target}"}}'
        )

    def capture(self) -> str:
        return bridge._capture_pane(self.socket, self.tmux_target)

    def alive(self) -> bool:
        return _session_alive(self.socket, self.tmux_target)

    def kill(self) -> None:
        _tmux(self.socket, "kill-server")


@pytest.fixture
def short_readiness_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the executor's inject through the real bridge with a short deadline.

    ``inject_user_message``'s ``timeout_s`` default is bound at import, so it
    cannot be lowered by patching the module constant. Redirect the executor's
    module-level name to a ``functools.partial`` over the **real** bridge
    function with the shortened deadline -- the whole readiness / capture / reap
    path stays production code.
    """
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        functools.partial(bridge.inject_user_message, timeout_s=_READY_DEADLINE_S),
    )


async def _drive_first_message(bridge_dir: Path) -> list[object]:
    """Send a first web-UI message through the real executor; return its events."""
    executor = ClaudeNativeExecutor(bridge_dir)
    return [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello from the web UI"}],
            tools=[],
            system_prompt="",
        )
    ]


@pytest.mark.parametrize(
    "startup_screen",
    [
        pytest.param("Password: ", id="launcher-password"),
        pytest.param("Waiting for OAuth callback on port 12345", id="oauth-signin"),
    ],
)
async def test_startup_input_prompt_pane_is_preserved(
    tmp_path: Path,
    short_readiness_deadline: None,
    startup_screen: str,
) -> None:
    """A terminal awaiting startup input must survive a first-message timeout.

    The pane is alive and merely waiting for the user (password / OAuth), so the
    readiness timeout is *recoverable*: the send may report an error, but the
    pane must be preserved so the user can complete startup and retry. With the
    bug present the executor reaps (kills) the pane -- this fails until the fix
    distinguishes a startup screen from a boot failure.
    """
    # A live terminal that prints the startup screen then blocks on input,
    # faithfully modeling a launcher password prompt / OAuth sign-in that waits
    # for the human before Claude Code ever renders its input box.
    pane_command = f"printf {startup_screen!r}; read -r _answer; echo continuing"
    terminal = _RealTerminal(tmp_path, pane_command)
    try:
        # Precondition: the pane is alive and shows the startup screen, but is
        # NOT Claude's ready input box (no prompt glyph).
        assert terminal.alive()
        pane_before = terminal.capture()
        assert startup_screen.strip() in pane_before
        assert not bridge._claude_prompt_rendered(pane_before)

        events = await _drive_first_message(terminal.bridge_dir)

        # The send did not silently succeed: the user is told delivery failed.
        assert any(isinstance(event, ExecutorError) for event in events), (
            f"expected a delivery ExecutorError, got {events!r}"
        )
        # The core regression: the healthy terminal waiting for startup input
        # must NOT be killed -- the user can still complete startup and retry.
        assert terminal.alive(), (
            "claude-native reaped a terminal that was alive and waiting for "
            f"startup input ({startup_screen!r}); the pane must be preserved so "
            "the user can finish startup and send again"
        )
    finally:
        terminal.kill()


async def test_empty_capture_boot_failure_is_still_reaped(
    tmp_path: Path,
    short_readiness_deadline: None,
) -> None:
    """Control: an empty capture (a genuine boot failure) is still cleaned up.

    A pane that renders no text at the deadline is a real dead/failed boot, not
    a startup screen awaiting input. The fix must keep reaping these -- this
    passes both before and after, guarding against an over-broad fix that
    preserves every timed-out pane.
    """
    # A live pane that renders nothing -> _capture_pane returns empty text.
    terminal = _RealTerminal(tmp_path, "sleep 300")
    try:
        assert terminal.alive()
        assert terminal.capture().strip() == ""

        events = await _drive_first_message(terminal.bridge_dir)

        assert any(isinstance(event, ExecutorError) for event in events), (
            f"expected a delivery ExecutorError, got {events!r}"
        )
        assert not terminal.alive(), (
            "an empty-capture boot failure must still be reaped (cleanup retained)"
        )
    finally:
        terminal.kill()
