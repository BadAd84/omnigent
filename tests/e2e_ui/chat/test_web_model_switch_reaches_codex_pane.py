r"""UI journey: a Codex default-model row must remain switchable.

The composer represents the row marked ``isDefault`` as a cleared session
override. The PATCH correctly persists ``model_override = null``, but the
runner previously received that stored value rather than an explicit reset
command. Codex accepts a null live update as a successful no-op, leaving a
session that moved away from its default unable to switch back from the web UI.

This test boots a real Codex app-server against the mock provider, switches to
a non-default row, sends a turn that persists that current model, then selects
the default row. The default selection must both clear the persisted override
and move the live Codex thread back to the concrete launch model.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _TERMINAL_READY_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)


def _session(base_url: str, session_id: str) -> dict[str, Any]:
    """Fetch the current session snapshot."""
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10)
    response.raise_for_status()
    return response.json()


def _wait_for_model_options(
    base_url: str,
    session_id: str,
    *,
    timeout_s: float = 60.0,
) -> list[dict[str, Any]]:
    """Wait for the runner-backed Codex catalog."""
    deadline = time.monotonic() + timeout_s
    latest: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        raw = _session(base_url, session_id).get("model_options")
        if isinstance(raw, list):
            latest = [row for row in raw if isinstance(row, dict)]
        if latest:
            return latest
        time.sleep(0.5)
    raise AssertionError(f"Codex model options never resolved (last={latest!r})")


def _wait_for_session_model(
    base_url: str,
    session_id: str,
    *,
    field: str,
    expected: str | None,
    timeout_s: float = 30.0,
) -> None:
    """Wait until a persisted or reported model equals *expected*."""
    deadline = time.monotonic() + timeout_s
    latest: object = None
    while time.monotonic() < deadline:
        latest = _session(base_url, session_id).get(field)
        if latest == expected:
            return
        time.sleep(0.5)
    raise AssertionError(f"session {field} stayed {latest!r}; expected {expected!r}")


def _open_composer_model_submenu(page: Page) -> None:
    """Open the composer's model rows."""
    page.wait_for_timeout(500)
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
    gear.click()
    edit = page.get_by_test_id("composer-agent-edit")
    expect(edit).to_be_visible(timeout=15_000)
    # Snapshot refreshes replace this Radix trigger after a model report. A
    # dispatched click exercises the same handler without waiting for a stale
    # element instance to remain geometrically stable across that replacement.
    edit.dispatch_event("click")


def _pick_model_row(page: Page, model_id: str) -> None:
    """Click the composer model row for *model_id*."""
    row = page.get_by_test_id(f"composer-agent-model-{model_id}")
    expect(row).to_be_visible(timeout=15_000)
    row.dispatch_event("click")


def _default_and_alternate(options: list[dict[str, Any]]) -> tuple[str, str]:
    """Return one concrete default row and a distinct non-default row."""
    default = next(
        (
            row.get("id")
            for row in options
            if row.get("isDefault") is True and isinstance(row.get("id"), str)
        ),
        None,
    )
    alternate = next(
        (
            row.get("id")
            for row in options
            if row.get("isDefault") is not True
            and isinstance(row.get("id"), str)
            and row.get("id") != default
        ),
        None,
    )
    if not isinstance(default, str) or not isinstance(alternate, str):
        raise AssertionError(
            f"expected distinct default and non-default Codex rows; got {options!r}"
        )
    return default, alternate


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_web_can_switch_codex_back_to_its_default_model(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Switch away from the default, then select that concrete row again."""
    base_url, session_id = native_codex_mock_session
    _log.info("native-codex mock session: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # A fresh Codex rollout does not exist until its first turn, so bootstrap
    # one mock-backed turn before relying on thread/settings/updated reports.
    marker = f"MODEL_BOOTSTRAP_{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": marker}],
        key=marker,
        match=marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")
    _send(page, f"Reply with exactly {marker}")
    expect(page.locator(_ASSISTANT, has_text=marker).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )

    default_model, alternate_model = _default_and_alternate(
        _wait_for_model_options(base_url, session_id)
    )

    # Establish the reported failure state: the live thread is on another
    # concrete model while the catalog still exposes the launch model as its
    # default row.
    _open_composer_model_submenu(page)
    _pick_model_row(page, alternate_model)
    _wait_for_session_model(
        base_url,
        session_id,
        field="model_override",
        expected=alternate_model,
    )
    _wait_for_session_model(
        base_url,
        session_id,
        field="llm_model",
        expected=alternate_model,
    )

    # A web-driven turn writes the selected model into Codex's mutable private
    # config. Reset must use the separately preserved launch model afterward.
    switched_marker = f"MODEL_SWITCHED_{uuid.uuid4().hex[:8]}"
    assistant_count = page.locator(_ASSISTANT).count()
    _send(page, f"Reply with exactly {switched_marker}")
    expect(page.locator(_ASSISTANT)).to_have_count(
        assistant_count + 1, timeout=_MOCK_TURN_TIMEOUT_MS
    )

    # Selecting the isDefault row clears the override, but the explicit reset
    # must still resolve and send the row's concrete id to Codex.
    _open_composer_model_submenu(page)
    _pick_model_row(page, default_model)
    _wait_for_session_model(
        base_url,
        session_id,
        field="model_override",
        expected=None,
    )
    _wait_for_session_model(
        base_url,
        session_id,
        field="llm_model",
        expected=default_model,
    )
