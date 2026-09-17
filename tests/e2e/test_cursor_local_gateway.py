"""Real Cursor CLI turns through Omnigent, with a local OpenAI-compatible gateway."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.cursor_native.bridge import bridge_dir_for_session_id, kill_session
from tests.e2e._native_resume_helpers import (
    cli_env,
    inject_user_message,
    omnigent_console_script,
    poll_for_assistant_marker,
    spawn_cli_background,
    wait_for_terminal_ready,
)
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_mock_served_models


@pytest.fixture(autouse=True)
def cursor_local_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = os.environ.get("OMNIGENT_TEST_CURSOR_LOCAL_PATH") or shutil.which(
        "cursor-agent-local"
    )
    missing = (
        pytest.fail
        if os.environ.get("OMNIGENT_REQUIRE_CURSOR_LOCAL_TESTS") == "1"
        else pytest.skip
    )
    if not binary or not Path(binary).is_file():
        missing(
            "Install the pinned cursor-agent-local build or set OMNIGENT_TEST_CURSOR_LOCAL_PATH"
        )
    if shutil.which("tmux") is None:
        missing("Cursor native tests require tmux")
    for name in tuple(os.environ):
        if name.startswith(
            ("CURSOR_", "HARNESS_CURSOR_", "DATABRICKS_", "ANTHROPIC_", "OPENAI_", "RUNNER_")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OMNIGENT_CURSOR_PATH", str(Path(binary).resolve()))
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(tmp_path / "cursor-config"))
    monkeypatch.setenv("CURSOR_LOCAL_AGENT_API_KEY", "local-test-key")
    monkeypatch.setenv("AGENT_CLI_CREDENTIAL_STORE", "memory")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIGENT_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv(
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH",
        ",".join(
            [
                "OMNIGENT_CURSOR_PATH",
                "CURSOR_CONFIG_DIR",
                "CURSOR_LOCAL_AGENT_API_KEY",
                "CURSOR_LOCAL_AGENT_BASE_URL",
                "AGENT_CLI_CREDENTIAL_STORE",
            ]
        ),
    )


@pytest.mark.parametrize("base_url_source", ["flag", "env"])
def test_cursor_local_gateway_conversation(
    resume_test_server: str,
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url_source: str,
) -> None:
    model = "cursor-test-model"
    markers = [f"CURSOR_{uuid.uuid4().hex}" for _ in range(3)]
    gateway = isolated_mock_llm_server_url
    set_mock_served_models(gateway, [model])
    # Cursor also requests a chat title; only conversation turns consume this queue.
    configure_mock_llm(
        gateway, [{"text": marker} for marker in markers], key="cursor-turns", match="<user_query>"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cursor_args = ["--authless", "--model", model, "--trust", "--force"]
    if base_url_source == "flag":
        cursor_args.extend(["--base-url", gateway + "/v1"])
    else:
        monkeypatch.setenv("CURSOR_LOCAL_AGENT_BASE_URL", gateway + "/v1")
    command = [str(omnigent_console_script()), "cursor", "--server", resume_test_server]
    handle = spawn_cli_background(
        [*command, "--", *cursor_args],
        env=cli_env(),
        cwd=str(workspace),
    )
    conversation_id = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            match = re.search(r"/c/([A-Za-z0-9_-]{8,})", handle.output())
            if match:
                conversation_id = match[1]
                break
            time.sleep(0.2)
        assert conversation_id, handle.output()[-4000:]
        with httpx.Client(base_url=resume_test_server, timeout=15) as client:
            wait_for_terminal_ready(
                client, conversation_id=conversation_id, harness="cursor", timeout=45
            )
            for marker in markers[:2]:
                inject_user_message(
                    client, conversation_id=conversation_id, text="Say hello without using tools."
                )
                poll_for_assistant_marker(
                    client, conversation_id=conversation_id, marker=marker, timeout=45
                )
            kill_session(bridge_dir_for_session_id(conversation_id), timeout_s=2)
            handle.terminate()
            handle = spawn_cli_background(
                [*command, "--resume", conversation_id, "--", *cursor_args],
                env=cli_env(),
                cwd=str(workspace),
            )
            wait_for_terminal_ready(
                client, conversation_id=conversation_id, harness="cursor", timeout=45
            )
            inject_user_message(
                client,
                conversation_id=conversation_id,
                text="Say hello again without using tools.",
            )
            poll_for_assistant_marker(
                client, conversation_id=conversation_id, marker=markers[2], timeout=45
            )
        requests = get_mock_requests(gateway)
        turns = [
            r
            for r in requests
            if r.get("model") == model and "<user_query>" in str(r.get("messages"))
        ]
        assert len(turns) == 3
        assert markers[0] in str(turns[1]["messages"])
        assert all(marker in str(turns[2]["messages"]) for marker in markers[:2])
    except BaseException:
        (tmp_path / "cursor-terminal.log").write_text(handle.output())
        with contextlib.suppress(httpx.HTTPError):
            (tmp_path / "gateway-requests.json").write_text(
                json.dumps(get_mock_requests(gateway), indent=2)
            )
        raise
    finally:
        try:
            if conversation_id:
                with contextlib.suppress(RuntimeError):
                    kill_session(bridge_dir_for_session_id(conversation_id), timeout_s=2)
        finally:
            handle.terminate()
            subprocess.run(
                [
                    str(omnigent_console_script()),
                    "host",
                    "stop",
                    "--server",
                    resume_test_server,
                    "--force",
                ],
                env=cli_env(),
                capture_output=True,
                timeout=20,
                check=False,
            )
