"""UI journey: a deny-capable guardrails policy must not break sub-agent dispatch.

The session's agent bundle declares a ``guardrails`` CEL policy whose
expression is an allowlist of tool names with a terminal ``DENY`` branch.
The allowlist explicitly permits ``sys_session_send``, so when the user
asks the orchestrator to dispatch its ``worker`` sub-agent, the dispatch
must return a launching sub-agent handle and the Agents rail must list
the worker row — exactly as it does for an agent whose policy has no
``DENY`` branch (or no guardrails at all).

The defect this guards against: session init probes the spec's policy
gate with a synthetic ``sys_agent_start`` tool call. Any allowlist whose
terminal branch is ``DENY`` denies that probe (``sys_agent_start`` is not
in the allowlist), init aborts before the parent's session inbox is
created, and the session still opens and answers through the lazy-spawn
path — so the first ``sys_session_send`` fails with ``Error:
sys_session_send requires parent session inbox`` and no child ever
spawns. On the buggy build this test FAILS quoting that output; after a
fix the same journey dispatches the worker.

Registration and mock scripting mirror
``tests/e2e_ui/agents/test_spawn_bounds_fanout_cap.py`` (strict
``config.yaml`` bundle parser — the one that honors ``guardrails``).
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'

# Sentinel ending the parent's dispatch turn so the test can wait on it.
_PARENT_TURN_DONE = "PARENT_DISPATCH_TURN_DONE"
_WORKER_DONE = "WORKER_PING_DONE"

# The dispatch turn runs a spawn round plus the child turn and a parent
# auto-wake over the mock LLM, so give the bubble a generous budget.
_TURN_TIMEOUT_MS = 240_000

# Allowlist-by-name with a terminal DENY: allows the dispatch tools the
# journey uses, denies everything else. The deny-capable terminal branch
# is the trigger under test — flipping it to ALLOW masks the bug.
_ALLOWLIST_THEN_DENY = (
    'event.type != "tool_call"'
    ' ? {"result": "ALLOW"}'
    " : has(event.data.name)"
    " && type(event.data.name) == string"
    ' && event.data.name.matches("^(ToolSearch|sys_session_send|sys_read_inbox)$")'
    ' ? {"result": "ALLOW"}'
    ' : {"result": "DENY"}'
)


def _parent_config(name: str, model: str) -> dict[str, Any]:
    """Parent orchestrator spec with the deny-capable CEL guardrail.

    :param name: Unique agent name for this registration.
    :param model: Mock model key routing the parent's LLM calls.
    :returns: ``config.yaml`` contents as a dict.
    """
    return {
        "spec_version": 1,
        "name": name,
        "prompt": (
            "You are an orchestrator. When asked to run, dispatch the "
            "worker sub-agent with sys_session_send.\n"
        ),
        "executor": {"model": model, "config": {"harness": "openai-agents"}},
        "tools": {"agents": ["worker"]},
        "guardrails": {
            "policies": {
                "allowlist_then_deny": {
                    "type": "function",
                    "on": ["tool_call"],
                    "function": {
                        "path": "omnigent.policies.builtins.cel.cel_policy",
                        "arguments": {"expression": _ALLOWLIST_THEN_DENY},
                    },
                }
            }
        },
        "os_env": {"type": "caller_process", "cwd": "."},
    }


def _worker_config(model: str) -> dict[str, Any]:
    """Trivial worker child spec.

    :param model: Mock model key routing the worker's LLM calls.
    :returns: ``agents/worker/config.yaml`` contents as a dict.
    """
    return {
        "spec_version": 1,
        "name": "worker",
        "prompt": "You are a worker. Acknowledge the task you were given and finish.\n",
        "executor": {"model": model, "config": {"harness": "openai-agents"}},
        "os_env": {"type": "caller_process", "cwd": "."},
    }


@dataclass(frozen=True)
class DenyCapablePolicySession:
    """Handle for the deny-capable-guardrail orchestrator session fixture.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound parent session id.
    """

    base_url: str
    session_id: str


@pytest.fixture
def deny_capable_policy_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DenyCapablePolicySession]:
    """Create a runner-bound session for the guarded orchestrator.

    Scripts the parent queue to dispatch the worker once via
    ``sys_session_send``, then end the turn; the worker draws a canned
    acknowledgement. Unique per-run model keys isolate the queues.

    :param live_server: Spawned server fixture from the parent conftest.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: A :class:`DenyCapablePolicySession` handle.
    """
    uid = uuid.uuid4().hex[:8]
    agent_name = f"deny_capable_probe_{uid}"
    parent_model = f"denycapable-parent-{uid}"
    child_model = f"denycapable-child-{uid}"

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_worker",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": "ping",
                                "args": "Reply exactly PING and finish.",
                            }
                        ),
                    },
                ],
            },
            {"text": _PARENT_TURN_DONE},
        ],
        key=parent_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, parent_model, "PARENT_WAKE_DONE")
    set_fallback_mock_llm(mock_llm_server_url, child_model, _WORKER_DONE)

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, config in (
            ("config.yaml", _parent_config(agent_name, parent_model)),
            ("agents/worker/config.yaml", _worker_config(child_model)),
        ):
            data = yaml.dump(config).encode()
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield DenyCapablePolicySession(base_url=live_server, session_id=session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _dispatch_outputs(base_url: str, session_id: str) -> list[str]:
    """Return the parent's ``sys_session_send`` tool outputs, in order.

    :param base_url: Spawned server base URL.
    :param session_id: The parent session id.
    :returns: Output payloads of ``function_call_output`` items whose
        call_id matches a ``sys_session_send`` function_call.
    """
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    flattened = [
        {"type": item.get("type"), **(item.get("data") or {})}
        for item in snap.json().get("items", [])
    ]
    dispatch_call_ids = {
        p.get("call_id")
        for p in flattened
        if p.get("type") == "function_call" and p.get("name") == "sys_session_send"
    }
    return [
        str(p.get("output", ""))
        for p in flattened
        if p.get("type") == "function_call_output" and p.get("call_id") in dispatch_call_ids
    ]


def _show_failed_dispatch(page: Page) -> bool:
    """Best-effort: surface the failed dispatch on screen before failing.

    Expands the transcript's ``sys_session_send`` tool call (directly or
    inside a collapsed fold) so its error output is visible, then opens
    the Agents rail to show no worker spawned. Purely for the journey
    recording — the assertions carry the verdict.

    :param page: The Playwright page, on the parent session.
    :returns: Whether the inbox error text became visible on screen.
    """
    shown = False
    try:
        worked = page.get_by_test_id("turn-worked-fold")
        if worked.count():
            worked.first.locator('[data-slot="collapsible-trigger"]').first.click()
            page.wait_for_timeout(500)
        group = page.get_by_text(re.compile(r"^(Called|Ran) \d+ tools?$"))
        if group.count():
            group.first.click()
            page.wait_for_timeout(500)
        call = page.get_by_role("button", name=re.compile(r"^sys_session_send\("))
        if call.count():
            call.first.click()
        error_text = page.get_by_text("requires parent session inbox")
        expect(error_text.first).to_be_visible(timeout=10_000)
        shown = True
        page.wait_for_timeout(1_500)
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Agents")).click()
        page.wait_for_timeout(2_000)
    except Exception:
        pass
    return shown


@pytest.mark.timeout(600)
def test_deny_capable_guardrail_does_not_break_dispatch(
    page: Page,
    deny_capable_policy_session: DenyCapablePolicySession,
) -> None:
    """A policy that allows sys_session_send by name must not break dispatch."""
    chat = deny_capable_policy_session
    page.goto(f"{chat.base_url}/c/{chat.session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("RUN: dispatch the worker sub-agent to ping.")
    page.get_by_role("button", name="Send", exact=True).click()

    turn_settled = True
    try:
        expect(page.locator(_ASSISTANT, has_text=_PARENT_TURN_DONE).first).to_be_visible(
            timeout=_TURN_TIMEOUT_MS
        )
    except AssertionError:
        turn_settled = False

    outputs = _dispatch_outputs(chat.base_url, chat.session_id)
    if not outputs:
        pytest.fail(
            "the scripted parent turn produced no sys_session_send output"
            + ("" if turn_settled else " and the turn never settled")
        )

    failed = [o for o in outputs if "task_id" not in o]
    if failed:
        shown = _show_failed_dispatch(page)
        pytest.fail(
            "sys_session_send did not return a launching sub-agent handle even "
            "though the guardrails policy allows it by name — the deny-capable "
            f"policy broke sub-agent dispatch. Tool output: {failed[0]!r} "
            f"(error surfaced in the transcript: {shown})"
        )

    # The user-visible outcome: the dispatched worker appears in the
    # Agents rail. Lookups are scoped to the desktop "Workspace" rail so
    # they don't match the hidden mobile drawer mirroring the testids.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    rows = rail.locator(_SUBAGENT_ROW)
    expect(rows.first).to_be_visible(timeout=60_000)
    # The row is labeled by the dispatch title, not the agent name.
    expect(rows.first).to_contain_text("ping")
    assert rows.first.get_attribute("data-child-session-id"), (
        "subagent row is missing data-child-session-id"
    )
