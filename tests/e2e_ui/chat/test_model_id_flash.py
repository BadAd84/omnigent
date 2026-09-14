"""E2E: an existing session must not flash the raw wire model id pre-catalog.

Reloading a claude-native session whose reported model id differs from the
catalog's display name renders the raw wire id (``system.ai.claude-sonnet-5``)
in the composer chip until the delayed model catalog arrives. Expected: a
loading state (or the previously confirmed label) until the catalog resolves,
then the advertised ``displayName``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native

_RAW_WIRE_ID = "system.ai.claude-sonnet-5"
_DISPLAY_NAME = "Sonnet 5"
# Window sampled for the raw-id flash after the snapshot (which carries the
# raw ``llm_model`` but an empty catalog) has landed. The buggy render is one
# React commit behind the snapshot, so five seconds is generous.
_FLASH_WINDOW_S = 5.0


@pytest.fixture(autouse=True)
def _finish_snapshot_routes(page: Page) -> Iterator[None]:
    """Drain snapshot response handlers before Playwright disposes the page."""
    yield
    page.unroute_all(behavior="wait")


def _block_stream_until_released(page: Page, session_id: str) -> None:
    """Replace the session SSE stream with a test-controlled one.

    Mirrors ``test_claude_model_picker``'s delayed-catalog setup: the page
    never sees a real stream frame until the test enqueues one, so the
    pre-catalog window stays open exactly as long as the test needs.

    :param page: Playwright page, before navigation.
    :param session_id: Session whose ``/stream`` fetch is replaced.
    """
    stream_script = """
        (() => {
          const sessionId = __SESSION_ID__;
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            const streamPath = `/v1/sessions/${sessionId}/stream`;
            if (new URL(url, window.location.origin).pathname === streamPath) {
              const body = new ReadableStream({
                start(controller) {
                  window.__modelIdFlashStreamController = controller;
                },
              });
              return Promise.resolve(new Response(body, {
                status: 200,
                headers: { "content-type": "text/event-stream" },
              }));
            }
            return originalFetch(input, init);
          };
        })()
        """.replace("__SESSION_ID__", json.dumps(session_id))
    page.add_init_script(stream_script)


def _screenshot(page: Page, name: str) -> None:
    """Save a demo screenshot when E2E_SCREENSHOT_DIR is set (local runs)."""
    shot_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    if shot_dir:
        page.screenshot(path=str(Path(shot_dir) / f"{name}.png"))


def test_reload_pre_catalog_never_presents_raw_wire_model_id(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The composer chip must never show the wire id while the catalog loads.

    The snapshot reports ``llm_model = system.ai.claude-sonnet-5`` with an
    empty (still-loading) catalog; on the buggy build the chip renders that
    raw id for the whole pre-catalog window. Once the catalog arrives the
    chip must show the advertised display name.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native.
    """
    base_url, session_id = seeded_session
    catalog_state = {"ready": False}
    _patch_session_as_claude_native(
        page,
        session_id,
        catalog_state=catalog_state,
        llm_model=_RAW_WIRE_ID,
    )
    _block_stream_until_released(page, session_id)

    # Anchor on the snapshot response: from here the store knows the raw
    # ``llm_model`` and the (empty) catalog, so the pre-catalog window is
    # open.
    with page.expect_response(
        lambda response: (
            response.request.method == "GET"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
        ),
        timeout=30_000,
    ):
        page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    model_value = page.get_by_test_id("composer-agent-model-value")

    # The bug: sample the whole pre-catalog window -- the raw wire id must
    # never be presented as the model label. (A plain not_to_contain_text
    # would pass on the pre-render frame, masking the flash.)
    deadline = time.monotonic() + _FLASH_WINDOW_S
    while time.monotonic() < deadline:
        if model_value.count() and _RAW_WIRE_ID in (model_value.inner_text() or ""):
            _screenshot(page, "model-id-flash-raw-wire-id-flash")
            # Dwell on the buggy state so a recorded run shows it clearly.
            page.wait_for_timeout(1_500)
            raise AssertionError(
                f"composer chip presented the raw wire model id {_RAW_WIRE_ID!r} "
                f"while the model catalog was still loading -- expected a loading "
                f"state (or a previously confirmed label) until the catalog's "
                f"displayName is available"
            )
        # Cooperative wait so route handlers keep being serviced.
        page.wait_for_timeout(150)
    _screenshot(page, "model-id-flash-pre-catalog-clean")

    # Deliver the catalog (snapshot refetches now carry it) and nudge the SPA
    # with the stream event that announces resolved model options.
    catalog_state["ready"] = True
    page.wait_for_function("window.__modelIdFlashStreamController !== undefined")
    page.evaluate(
        """
        ({ sessionId }) => {
          const frame = `event: session.model_options\\ndata: ${JSON.stringify({
            conversation_id: sessionId,
          })}\\n\\n`;
          window.__modelIdFlashStreamController.enqueue(new TextEncoder().encode(frame));
        }
        """,
        {"sessionId": session_id},
    )

    # Live metadata replaces the pending state: the advertised displayName --
    # never the wire id -- is what the user reads.
    expect(model_value).to_contain_text(_DISPLAY_NAME, timeout=15_000)
    expect(model_value).not_to_contain_text(_RAW_WIRE_ID)
    _screenshot(page, "model-id-flash-catalog-display-name")
