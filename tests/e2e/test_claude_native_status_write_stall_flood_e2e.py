"""E2E regression: a failing statusLine write must not silence chat, and a
persistently-failing forward loop must back off with throttled logging.

The bug
-------
The claude-native transcript forwarder normalizes the statusLine shim's raw
capture into ``context.json`` on every poll, *before* it forwards any transcript
items::

    status_raw_sig = sync_raw_status_context(bridge_dir, status_raw_sig)   # optional
    transcript_path = read_transcript_path(bridge_dir)                     # then forward
    ...  # _forward_available_items posts the chat items

``sync_raw_status_context`` (``omnigent/harnesses/claude_native/status.py``)
wraps its ``stat`` / ``read_text`` in ``try/except OSError`` but calls
``_write_record_atomic`` **without** catching its ``OSError``. So when the bridge
directory is full or read-only the *optional* context-window write raises, and
because that call sits ahead of the forwarding block inside the poll's single
``try``, the exception aborts the **entire** poll iteration -- no transcript
items are forwarded. The context write never changes its remembered signature,
so every subsequent poll re-attempts the same failing write and aborts again:
chat is silently and permanently stalled while the terminal keeps running.

The outer ``while True`` loop's ``except Exception`` then logs a full traceback
(``_logger.exception``) and sleeps only ``poll_interval_s`` (0.25s in
production). Under a persistent disk / file-descriptor error this is a flat
0.25s retry cadence that emits a traceback on *every* failure -- a sustained
error storm with forwarding still stalled and no backoff.

Two facets
----------
A. **Forwarding stall** (surface: web -- the user's chat view silently stops
   updating). A ``_write_record_atomic`` that raises ``OSError(ENOSPC)`` must not
   abort the rest of the poll: transcript items must still be forwarded. On the
   unfixed build the optional write aborts every poll, so the seeded transcript
   items never reach the session store (``GET /items`` -- the store the web chat
   renders). This test FAILS on the unfixed build (0 forwarded items).

B. **Error storm / no backoff** (surface: api -- internal retry + logging
   behavior in the runner). With the poll body's bridge read raising ``OSError``
   every iteration, the outer loop must back off with a bounded, growing delay
   and throttle its logging. On the unfixed build it retries at a flat
   ``poll_interval_s`` cadence and logs a traceback on every failure. This test
   FAILS on the unfixed build (too many poll iterations, flat cadence, and a
   traceback per failure).

Both drive the REAL user path: a real ``omnigent server`` subprocess (so the
real ``POST /v1/sessions/<id>/events`` route + commit path run), a real
claude-native session, and the real ``forward_claude_transcript_to_session``
loop. The reported triggers -- a full/read-only bridge dir and repeated bridge
read errors -- are injected as faults exactly as the ticket prescribes ("an
atomic writer that raises OSError(ENOSPC)"; "make the forward loop's bridge read
repeatedly raise OSError and measure retry delays and emitted errors").

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_status_write_stall_flood_e2e.py -v

No ``--llm-api-key`` or ``--profile`` needed -- no LLM is invoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import itertools
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
_http = httpx.Client(trust_env=False)

# The spawned server resolves worktree imports from the repo root and the SDKs.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# Plain server launch -- the bug lives in the forwarder + the server's real
# commit path; no store monkeypatch needed.
_SERVER_BOOTSTRAP = "from omnigent.cli import main\n\nmain()\n"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5

# Distinct transcript markers so the assertion can count committed items.
_MARKER_ONE = "status-write-stall-item-one-should-still-forward"
_MARKER_TWO = "status-write-stall-item-two-should-still-forward"


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy/credentials in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # Header auth + single-user keeps the spawned server out of login
        # mode; ambient auth/OIDC vars would otherwise 401 every call.
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    # Strip ambient credentials/config that would alter server behaviour:
    # any Databricks or OIDC setting, any cookie/signing secret, and the
    # specific provider/tunnel vars below.
    for name in list(env):
        if (
            name.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or name.endswith("_SECRET")
            or name
            in (
                "ANTHROPIC_API_KEY",
                "OMNIGENT_AUTH_ENABLED",
                "OMNIGENT_RUNNER_TUNNEL_TOKEN",
            )
        ):
            env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the created session is a real
    claude-native conversation -- the kind whose transcript the forwarder
    mirrors in production.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat translator
        # (the wrapper spec has no ``spec_version``).
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _seed_transcript(bridge_dir: Path) -> Path:
    """Write a two-item Claude JSONL transcript + a Stop hook.

    Two assistant text entries with distinct uuids, each of which the forwarder
    mirrors as one ``external_conversation_item``. A recorded ``Stop`` hook
    reports the transcript path so the loop resolves it on the first poll.

    :param bridge_dir: Native Claude bridge directory.
    :returns: The transcript path.
    """
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"
    lines = [
        {
            "type": "assistant",
            "uuid": "assistant-stall-one",
            "message": {"role": "assistant", "content": [{"type": "text", "text": _MARKER_ONE}]},
        },
        {
            "type": "assistant",
            "uuid": "assistant-stall-two",
            "message": {"role": "assistant", "content": [{"type": "text", "text": _MARKER_TWO}]},
        },
    ]
    transcript_path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session-status-write-stall",
            "transcript_path": str(transcript_path),
        },
    )
    return transcript_path


def _seed_raw_status_context(bridge_dir: Path) -> None:
    """Seed a valid statusLine raw capture the forwarder will try to persist.

    Mirrors what the statusLine shim writes: a ``context_window`` block that
    ``normalize_status_payload`` accepts, so ``sync_raw_status_context`` gets
    past its stat/read and reaches the atomic write on every poll.

    :param bridge_dir: Native Claude bridge directory.
    """
    from omnigent.harnesses.claude_native.status import CONTEXT_RAW_FILE

    payload = {
        "context_window": {
            "context_window_size": 200000,
            "current_usage": {"input_tokens": 1234, "output_tokens": 56},
            "used_percentage": 12.5,
        }
    }
    (bridge_dir / CONTEXT_RAW_FILE).write_text(json.dumps(payload), encoding="utf-8")


def _count_marker(base_url: str, session_id: str, marker: str) -> int:
    """Count committed conversation items whose payload contains *marker*.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation to query.
    :param marker: Substring to match against each item's serialized data.
    :returns: Number of committed items carrying the marker.
    """
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return sum(1 for item in resp.json()["data"] if marker in json.dumps(item))


class _ForwarderErrorCounter(logging.Handler):
    """Capture the forwarder's per-iteration failure log records.

    The outer loop's ``except Exception`` logs ``"Claude transcript forwarder
    loop failed"`` at ERROR on every failing poll. Counting those records
    measures whether the loop throttles its logging under a persistent fault.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.loop_failed_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            if "forwarder loop failed" in record.getMessage():
                self.loop_failed_count += 1


async def _drive_forwarder_with_failing_status_write(
    base_url: str, session_id: str, bridge_dir: Path, *, run_for_s: float
) -> None:
    """Run the real forwarder loop with the statusLine write raising ENOSPC.

    Injects the reported trigger -- a full/read-only bridge dir -- by making
    ``_write_record_atomic`` raise ``OSError(ENOSPC)`` on every call, exactly as
    the ticket prescribes. The seeded raw context guarantees the write is
    attempted each poll, ahead of the transcript-forwarding block.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation the forwarder mirrors into.
    :param bridge_dir: Seeded native Claude bridge directory.
    :param run_for_s: Wall-clock seconds to let the loop run before cancelling.
    """
    import omnigent.harnesses.claude_native.forwarder as fwd
    import omnigent.harnesses.claude_native.status as status_mod

    real_write = status_mod._write_record_atomic

    def _write_enospc(bridge_dir: Path, record: dict[str, object]) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    # The forwarder calls the real ``sync_raw_status_context``; only the atomic
    # writer it invokes is faulted, so the bug's real seam (an uncaught write
    # OSError propagating out of the optional status sync) is exercised.
    status_mod._write_record_atomic = _write_enospc
    try:
        task = asyncio.create_task(
            fwd.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.05,
            )
        )
        try:
            await asyncio.sleep(run_for_s)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        status_mod._write_record_atomic = real_write


async def _drive_forwarder_with_failing_bridge_read(
    base_url: str, session_id: str, bridge_dir: Path, *, run_for_s: float
) -> tuple[list[float], int]:
    """Run the real forwarder loop with the poll body's bridge read failing.

    Injects the reported trigger -- repeated bridge read errors -- by making
    ``read_active_session_id`` (the first bridge read in the poll body) raise
    ``OSError`` on every iteration. Records the wall-clock time of each poll
    attempt (to measure retry cadence / backoff) and counts the loop's emitted
    failure logs (to measure log throttling).

    :param base_url: Spawned server base URL.
    :param session_id: Conversation the forwarder mirrors into.
    :param bridge_dir: Native Claude bridge directory.
    :param run_for_s: Wall-clock seconds to let the loop run before cancelling.
    :returns: ``(poll_attempt_times, emitted_failure_log_count)``.
    """
    import omnigent.harnesses.claude_native.forwarder as fwd

    real_read = fwd.read_active_session_id
    attempt_times: list[float] = []

    def _read_oserror(bridge_dir: Path) -> str | None:
        attempt_times.append(time.monotonic())
        raise OSError(errno.EIO, "Input/output error reading bridge state")

    counter = _ForwarderErrorCounter()
    fwd._logger.addHandler(counter)
    fwd.read_active_session_id = _read_oserror
    try:
        task = asyncio.create_task(
            fwd.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.05,
            )
        )
        try:
            await asyncio.sleep(run_for_s)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        fwd.read_active_session_id = real_read
        fwd._logger.removeHandler(counter)
    return attempt_times, counter.loop_failed_count


# ---------------------------------------------------------------------------
# Module-scoped server + claude-native session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Start a real ``omnigent server`` subprocess; yield its base URL.

    :yields: Base URL, e.g. ``"http://127.0.0.1:<port>"``.
    """
    tmp_path = tmp_path_factory.mktemp("status-write-stall-server")
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    database_uri = f"sqlite:///{tmp_path / 'chat.db'}"
    server_log = (tmp_path / "server.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SERVER_BOOTSTRAP,
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                database_uri,
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)
        yield base_url
    finally:
        _terminate(server_proc)
        server_log.close()


# ---------------------------------------------------------------------------
# Facet A: an optional statusLine write failure must not silence chat
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_status_write_oserror_does_not_stall_transcript_forwarding(
    _server: str, tmp_path: Path
) -> None:
    """A failing statusLine write must not abort transcript forwarding.

    Journey (the reporter's): a claude-native session's forwarder mirrors its
    transcript to chat; the bridge directory fills / goes read-only, so the
    optional context-window write raises ``OSError(ENOSPC)``; because that
    write runs ahead of the forwarding block in the same poll ``try``, the
    whole poll aborts and no transcript items are forwarded -- the web chat
    view goes silent while the terminal keeps working.

    Expected: the failed context update is contained so the rest of the poll
    still runs; the two seeded transcript items reach the session store (the
    store the web chat renders). Buggy behavior: the optional write aborts
    every poll and the items never forward -- this test FAILS with a forwarded
    count of 0.

    :param _server: Base URL of the module server.
    :param tmp_path: Per-test temp dir (workspace / bridge dir).
    """
    base_url = _server
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir: Path | None = None
    try:
        session_id = _create_claude_native_session(base_url)
        from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

        bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)
        _seed_transcript(bridge_dir)
        _seed_raw_status_context(bridge_dir)

        # 5s at a 0.05s poll interval is ~100 polls -- ample time to forward the
        # two items were the poll not aborted by the optional status write.
        asyncio.run(
            _drive_forwarder_with_failing_status_write(
                base_url, session_id, bridge_dir, run_for_s=5.0
            )
        )

        one = _count_marker(base_url, session_id, _MARKER_ONE)
        two = _count_marker(base_url, session_id, _MARKER_TWO)

        assert one >= 1 and two >= 1, (
            "An optional statusLine context write raising OSError(ENOSPC) "
            "aborted every poll before the transcript-forwarding block, so the "
            f"seeded items never reached the session store (item-one={one}, "
            f"item-two={two}; expected >= 1 each). sync_raw_status_context does "
            "not catch the OSError from _write_record_atomic, and that call "
            "sits ahead of _forward_available_items in the poll's single try -- "
            "so a full/read-only bridge dir silences chat."
        )
    finally:
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Facet B: a persistently-failing poll must back off with throttled logging
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_repeated_forward_failures_back_off_and_throttle_logging(
    _server: str, tmp_path: Path
) -> None:
    """Repeated poll failures must back off and stop flooding error logs.

    Journey (the reporter's): a claude-native session's forwarder hits a
    persistent bridge read error (a full disk / exhausted file descriptors);
    the outer loop retries at its flat 0.25s cadence and logs a full traceback
    on every failure -- a sustained error storm.

    Expected: the loop backs off with a bounded, growing delay and throttles
    its logging, so a long failure streak produces few poll attempts, a growing
    inter-attempt gap, and few emitted error logs. Buggy behavior: a flat
    ``poll_interval_s`` cadence with a traceback per failure. Over an ~8s window
    the unfixed loop makes ~50 poll attempts with flat ~0.15s gaps and ~50
    error logs -- this test FAILS on all three counts.

    :param _server: Base URL of the module server.
    :param tmp_path: Per-test temp dir (workspace / bridge dir).
    """
    base_url = _server
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir: Path | None = None
    try:
        session_id = _create_claude_native_session(base_url)
        from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

        bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)

        run_for_s = 8.0
        poll_interval_s = 0.05
        attempt_times, error_logs = asyncio.run(
            _drive_forwarder_with_failing_bridge_read(
                base_url, session_id, bridge_dir, run_for_s=run_for_s
            )
        )

        attempts = len(attempt_times)
        gaps = [b - a for a, b in itertools.pairwise(attempt_times)]
        max_gap = max(gaps) if gaps else 0.0

        # Sanity: the loop actually retried (the fault fires and the loop
        # resumes), so the backoff assertions below are not vacuous.
        assert attempts >= 2, (
            "the failing bridge read never fired more than once; the forwarder "
            f"loop did not retry as expected (attempts={attempts})."
        )

        # A flat ~0.15s cadence over ~8s is ~50 attempts; a bounded backoff
        # collapses that to a small number. Comfortably separates fixed
        # (few polls) from unfixed (~50).
        assert attempts <= 25, (
            "The forward loop retried a persistent failure at a flat "
            f"poll-interval cadence: {attempts} poll attempts in {run_for_s:.0f}s "
            f"(gaps~{poll_interval_s}s). A bounded exponential backoff must slow "
            "repeated failures instead of hammering the bridge every "
            f"{poll_interval_s}s. Attempt gaps: "
            f"{[round(g, 3) for g in gaps[:12]]}..."
        )

        # Backoff must grow the delay between retries well past the poll
        # interval. Unfixed keeps every gap ~= poll_interval.
        assert max_gap >= 0.3, (
            "The forward loop did not back off between repeated failures: the "
            f"largest inter-attempt gap was {max_gap:.3f}s (~poll interval "
            f"{poll_interval_s}s). A bounded backoff should grow the delay "
            "(e.g. 0.25s -> 0.5s -> 1s ...) so a persistent fault is not retried "
            "at the raw poll cadence."
        )

        # Logging must be throttled: the unfixed loop logs a full traceback on
        # every failure (~50 over the window). Fixed throttles to a handful.
        assert error_logs <= 15, (
            "The forward loop logged a traceback on every failing poll: "
            f"{error_logs} 'forwarder loop failed' error records in "
            f"{run_for_s:.0f}s. Repeated identical failures must be throttled "
            "(log the first, then rate-limit) so a persistent disk / "
            "file-descriptor error does not flood the logs."
        )
    finally:
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)
