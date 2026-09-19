"""Ctrl+C during the boot-time model-catalog probe must shut down cleanly.

``omnigent host ""`` prewarms the shared Claude model catalog right after the
tunnel comes up, via a probe of the ``claude`` CLI. A probe still in flight
when SIGINT lands (a slow gateway, a cold store) must be drained and its
subprocess transport closed while the event loop lives. A stranded probe
shows the user either:

- a Python spew on exit (``Exception ignored in ... BaseSubprocessTransport
  .__del__ ... RuntimeError: Event loop is closed``), or
- a hang before the stop-server prompt / before exit, when teardown
  cancellation lands inside subprocess pipe setup.

This test pins a fake ``claude`` on PATH that blocks in the catalog probe, so
the probe is reliably in flight at Ctrl+C, then requires both a prompt within
budget (no hang) and a clean transcript (no undrained-teardown spew).
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import threading
from pathlib import Path

import pexpect

from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _EXIT_TIMEOUT,
    _PROMPT_MARKER,
    _PROMPT_TIMEOUT,
    _STOPPED_MARKER,
    _boot_connect_and_get_server,
    _connect_env,
    _force_stop_server,
    _spawn_connect,
)

# How long the fake probe blocks. It only has to straddle the Ctrl+C window;
# short enough that an orphan can't outlive the test run by much.
_PROBE_BLOCK_S = 90
# Budget for the boot prewarm to actually spawn the probe after the host is up.
_PROBE_SPAWN_TIMEOUT = 30.0

# Undrained-shutdown signatures: a transport or task that outlived the loop.
_TEARDOWN_SPEW_MARKERS = (
    "Event loop is closed",
    "Exception ignored in",
    "Task was destroyed but it is pending",
)

_POLL_PAUSE = threading.Event()

_SHIM_SCRIPT = """#!/usr/bin/env bash
case " $* " in
  *" --version "*)
    echo "2.1.236 (Claude Code)"
    exit 0
    ;;
  *"auth status"*)
    echo '{"loggedIn":true}'
    exit 0
    ;;
esac
echo "SPAWN $$" >> "$OMNI_TEST_PROBE_LOG"
# Do not die instantly on the group SIGINT Ctrl+C delivers: hold the pipes
# open (the trap only marks the exit) so the shared probe is still in
# flight while the host shuts down, then exit.
trap 'exit 130' INT TERM
sleep %d &
wait $!
"""


def _write_probe_shim(shim_dir: Path) -> Path:
    """Install a fake ``claude`` that answers cheap probes but blocks in the
    model-catalog probe, recording the blocking process's pid."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "claude"
    shim.write_text(_SHIM_SCRIPT % _PROBE_BLOCK_S, encoding="utf-8")
    shim.chmod(0o755)
    return shim


def _recorded_probe_pids(probe_log: Path) -> list[int]:
    if not probe_log.exists():
        return []
    pids = []
    for line in probe_log.read_text().splitlines():
        if line.startswith("SPAWN "):
            with contextlib.suppress(ValueError):
                pids.append(int(line.split()[1]))
    return pids


def _wait_for_probe_spawn(probe_log: Path, timeout: float) -> bool:
    elapsed = 0.0
    while elapsed < timeout:
        if _recorded_probe_pids(probe_log):
            return True
        _POLL_PAUSE.wait(0.05)
        elapsed += 0.05
    return bool(_recorded_probe_pids(probe_log))


def test_host_ctrl_c_with_inflight_catalog_probe_exits_clean(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Ctrl+C while the Claude catalog probe is in flight: the prompt must still
    appear promptly and the exit transcript must carry no teardown spew.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    home = tmp_path / "home"
    probe_log = tmp_path / "probe.log"
    shim_dir = tmp_path / "bin"
    _write_probe_shim(shim_dir)

    env = _connect_env(mock_credentials_env, home)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    env["OMNI_TEST_PROBE_LOG"] = str(probe_log)

    child = _spawn_connect(omnigent_python, omnigent_repo_root, env)
    transcript = io.StringIO()
    child.logfile_read = transcript
    server_pid = -1
    try:
        server_pid, _port = _boot_connect_and_get_server(child, home)

        # The prewarm probe must be mid-flight when SIGINT lands — the window
        # this test guards.
        assert _wait_for_probe_spawn(probe_log, _PROBE_SPAWN_TIMEOUT), (
            "the boot prewarm never spawned the claude model-catalog probe; "
            "the test environment did not reach the in-flight-probe window"
        )

        child.sendcontrol("c")

        # The hang symptom: teardown wedges (cancellation caught the probe's
        # subprocess pipe setup) and the prompt never arrives.
        try:
            child.expect_exact(_PROMPT_MARKER, timeout=_PROMPT_TIMEOUT)
        except pexpect.TIMEOUT:
            raise AssertionError(
                "host hung after Ctrl+C: the stop-server prompt did not appear "
                f"within {_PROMPT_TIMEOUT}s while the model-catalog probe was "
                "in flight — shutdown did not drain the shared probe"
            ) from None
        child.send("y\r")
        child.expect(_STOPPED_MARKER, timeout=_PROMPT_TIMEOUT)
        child.expect(pexpect.EOF, timeout=_EXIT_TIMEOUT)

        # The spew symptom: a stranded probe's subprocess transport is
        # destroyed after the loop closed, spewing onto the user's terminal.
        output = transcript.getvalue()
        spew = [marker for marker in _TEARDOWN_SPEW_MARKERS if marker in output]
        assert not spew, (
            "host shutdown returned without draining the in-flight model-catalog "
            f"probe: exit transcript contains {spew}; transcript tail:\n"
            + output[-2000:]
        )
    finally:
        if server_pid > 0:
            _force_stop_server(server_pid)
        for pid in _recorded_probe_pids(probe_log):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        if not child.closed:
            child.close(force=True)
