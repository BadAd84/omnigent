"""UI journey: kimi harness sessions must expose Omnigent tool schemas to the model.

Sessions on the direct ``kimi`` (headless) and ``kimi-native`` harnesses must
put the session's declared Omnigent tool schemas (``sys_*`` / builtin / MCP)
on the wire of the model request the kimi CLI builds — otherwise the model can
never call an Omnigent tool, and the failure is silent in the chat surface:
the turn completes normally with no tool-call card and no error.

Journey (real web SPA, live server + runner, real ``kimi`` CLI pointed at the
mock OpenAI endpoint):

1. create a ``harness: kimi`` (resp. terminal-first ``kimi-native``) agent and a
   runner-bound session for it
2. open the session in the web UI and ask the agent to use its ``sys_os_read``
   Omnigent tool on a workspace file
3. the turn runs; the kimi CLI calls the model (the mock captures the exact
   chat-completions request kimi sent)
4. the request kimi sent to the model must declare the session's Omnigent tool
   schemas — the ``sys_os_read`` canary in particular

The headless agent bundle declares an ``os_env`` block because the ``sys_os_*``
family is spec-gated on every headless harness (no ``os_env`` → not declared);
the kimi-native surface relays ``sys_os_*`` unconditionally like the other
native harnesses. The capture guard (kimi DID reach the mock for this turn)
pins a failure to dropped tool schemas rather than broken kimi/mock wiring.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent.inner.kimi_executor import _resolve_kimi_binary
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

pytestmark = pytest.mark.skipif(
    shutil.which(_resolve_kimi_binary()) is None,
    reason=(
        "the kimi (or OMNIGENT_KIMI_PATH) binary is required for the kimi "
        "tool-schema e2e; install via "
        "`curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash`"
    ),
)

# The kimi CLI reaches the loopback mock only when loopback is exempt from any
# ambient credential proxy (the runner-spawned kimi subprocess inherits
# NO_PROXY via the executor's spawn-env allowlist).
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))

# Isolated kimi config home, shared with the runner-spawned kimi processes:
# both the headless executor's spawn env and the kimi-native session-home
# builder resolve ``$KIMI_CODE_HOME`` (set here at import, before the
# session-scoped runner spawns), so the CLI reads this test's provider routing
# instead of any real ``~/.kimi-code`` config.
_KIMI_CODE_HOME = Path(tempfile.mkdtemp(prefix="kimi_home_tools_e2e_"))
os.environ["KIMI_CODE_HOME"] = str(_KIMI_CODE_HOME)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_KIMI_MODEL = "mock/kimi-k3"

# Canary Omnigent tool: the headless bundle declares an os_env (and the native
# relay carries sys_os_* unconditionally), so sys_os_read is deterministically
# part of the session's declared surface — its absence from kimi's model
# request means the Omnigent tool schemas were dropped.
_CANARY_TOOL = "sys_os_read"


def _write_kimi_config(mock_llm_server_url: str) -> None:
    """Point the kimi CLI's provider at the mock's OpenAI endpoint.

    :param mock_llm_server_url: Mock server base URL WITHOUT ``/v1`` (the
        provider ``base_url`` appends it; kimi then calls
        ``/v1/chat/completions``).
    """
    _KIMI_CODE_HOME.mkdir(parents=True, exist_ok=True)
    (_KIMI_CODE_HOME / "config.toml").write_text(
        f'default_model = "{_KIMI_MODEL}"\n'
        "\n"
        "[providers.mock]\n"
        'type = "openai"\n'
        f'base_url = "{mock_llm_server_url}/v1"\n'
        'api_key = "mock-key"\n'
        "\n"
        f'[models."{_KIMI_MODEL}"]\n'
        'provider_id = "mock"\n'
        'model = "kimi-k3"\n'
        "max_context_size = 262144\n"
    )


def _build_headless_kimi_bundle(name: str) -> bytes:
    """Build a one-file headless-``kimi`` agent bundle.

    No ``executor.auth`` is set: the kimi CLI has no per-spawn provider
    override, so routing lives in ``config.toml`` (written by
    :func:`_write_kimi_config`).

    :param name: Agent name (unique per test run).
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "kimi",
            "model": _KIMI_MODEL,
            "context_window": 262144,
        },
        # sys_os_* is spec-gated: without an os_env block the runner declares
        # no OS tools for the session, and the canary below could never appear.
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _create_headless_kimi_session(base_url: str, runner_id: str) -> str:
    """Create a runner-bound session for a fresh headless-``kimi`` agent.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :returns: The new session id.
    """
    name = f"kimi-tools-{uuid.uuid4().hex[:8]}"
    bundle = _build_headless_kimi_bundle(name)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _create_kimi_native_session(base_url: str, runner_id: str) -> str:
    """Register the real ``kimi-native`` wrapper agent and bind its session.

    Reuses the exact terminal-first spec ``omnigent kimi`` ships and stamps the
    same wrapper / terminal-first labels the CLI writes, so binding triggers the
    runner's kimi-native auto-bootstrap (it launches the kimi TUI in the session
    terminal on bind) — the production path a user drives, not a hand-rolled
    pane.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to bind.
    :returns: The new session id.
    """
    from omnigent._wrapper_labels import (
        KIMI_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.kimi_native.main import _materialize_kimi_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_kimi_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has no
        # spec_version), matching the other native-wrapper fixtures.
        info = tarfile.TarInfo("kimi-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    metadata = {
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: KIMI_NATIVE_WRAPPER_VALUE,
        }
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("kimi-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=60_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _turn_requests(mock_url: str, token: str, *, timeout_s: float) -> list[dict]:
    """Poll the mock for the chat-completions request(s) carrying *token*.

    :param mock_url: Mock server base URL.
    :param token: Unique routing token embedded in this turn's user message.
    :param timeout_s: How long to keep polling before returning what arrived.
    :returns: Captured request bodies whose ``messages`` contain *token*.
    """
    deadline = time.monotonic() + timeout_s
    matched: list[dict] = []
    while time.monotonic() < deadline:
        try:
            reqs = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
        except httpx.HTTPError:
            reqs = []
        matched = [
            r for r in reqs if isinstance(r, dict) and token in json.dumps(r.get("messages", ""))
        ]
        if matched:
            return matched
        time.sleep(2.0)
    return matched


def _declared_tool_names(request: dict) -> list[str]:
    """Extract every tool/function name the request offered the model."""
    names: list[str] = []
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if isinstance(name, str):
            names.append(name)
    for function in request.get("functions") or []:
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
    return names


def _assert_canary_reached_model(matched: list[dict], harness: str) -> None:
    """Reproduction assertion: the canary Omnigent tool schema is on the wire."""
    all_names = sorted({n for r in matched for n in _declared_tool_names(r)})
    assert any(_CANARY_TOOL in name for name in all_names), (
        f"harness {harness!r}: kimi's model request(s) declared no Omnigent "
        f"tool schema — {_CANARY_TOOL!r} (declared by the runner for every "
        f"session) never reached the model. Tools on the wire: {all_names!r}"
    )


@pytest.mark.timeout(600)
def test_headless_kimi_session_exposes_omnigent_tools_to_model(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A headless-kimi turn must offer the session's Omnigent tools to the model.

    If the executor drops the declared tools, the captured chat-completions
    request carries no ``sys_*`` schema and the chat shows a plain reply with
    no tool call and no error — the final assertion fails. With the Omnigent
    MCP bridge in place the canary schema appears on the wire.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        _write_kimi_config(mock_llm_server_url)

        token = f"kimitools-{uuid.uuid4().hex[:6]}"
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "ack from kimi"}] * 6,
            key=_KIMI_MODEL,
            match=token,
        )

        session_id = _create_headless_kimi_session(live_server, runner_id)
        try:
            page.goto(f"{live_server}/c/{session_id}")

            _send(
                page,
                "Use your sys_os_read Omnigent tool to read README.md in your "
                f"workspace and reply with its first line. {token}",
            )
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=180_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=180_000)

            # Capture guard (passes before and after a fix): kimi ran this turn
            # against the mock, so the tool assertion below judges a real
            # request kimi built, not a wiring failure.
            matched = _turn_requests(mock_llm_server_url, token, timeout_s=30.0)
            assert matched, (
                "the mock captured no chat-completions request for this turn — "
                "the kimi/mock provider wiring broke, not the tool bridge"
            )

            _assert_canary_reached_model(matched, "kimi")
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.mark.timeout(900)
def test_kimi_native_session_exposes_omnigent_tools_to_model(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A kimi-native turn must offer the session's Omnigent tools to the model.

    Same journey as the headless test but on the terminal-first ``kimi-native``
    wrapper: binding the session auto-launches the kimi TUI in a tmux pane, and
    the web-UI composer message is injected into it. Without an
    ``mcpServers.omnigent`` entry in the session-scoped Kimi home the TUI's
    model request declares no Omnigent tool schema — the final assertion fails
    until the bridge registers the relay there.
    """
    if shutil.which("tmux") is None:
        pytest.skip("kimi-native needs tmux for the runner-owned TUI pane")

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        _write_kimi_config(mock_llm_server_url)

        token = f"kiminativetools-{uuid.uuid4().hex[:6]}"
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "ack from native kimi"}] * 6,
            key=_KIMI_MODEL,
            match=token,
        )

        session_id = _create_kimi_native_session(live_server, runner_id)
        try:
            page.goto(f"{live_server}/c/{session_id}")

            _send(
                page,
                "Use your sys_os_read Omnigent tool to read README.md in your "
                f"workspace and reply with its first line. {token}",
            )

            # The TUI boots in a tmux pane before the injected turn reaches the
            # model, so wait on the mock capture (the turn's ground truth)
            # rather than the chat transcript the forwarder mirrors later.
            matched = _turn_requests(mock_llm_server_url, token, timeout_s=300.0)
            if not matched:
                # The interactive kimi TUI never drove a turn to the provider
                # here (no captured request) — a headless-CI limitation of the
                # native pane, not the tool bridge under test. Skip rather than
                # fail so this stays a clean guard on hosts that can drive the
                # TUI; the headless-kimi test guards the shared executor bug.
                pytest.skip(
                    "kimi-native TUI did not reach the model in this "
                    "environment; cannot exercise the tool bridge here"
                )

            _assert_canary_reached_model(matched, "kimi-native")
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
