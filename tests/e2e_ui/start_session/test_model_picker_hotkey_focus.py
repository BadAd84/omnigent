"""Ctrl+Shift+M on the New Chat screen must drill straight into the selected
harness's Models/Effort submenu and focus an actionable item there, even when
the chord is pressed on a freshly-opened screen while the host is still probing
the harness/model catalog. The reported bug: a chord pressed before the
picker's config content is ready opens only the root harness list (no focused
model item, arrow keys navigate harnesses), and a second press is needed to
reach the models submenu.

Driven against the real SPA in a browser; only the host/agent/model edges the
landing screen consults are faked, exactly like the sibling start_session
tests. The ``/v1/agents`` response is held until just after the first chord so
the race is deterministic rather than timing-dependent.
"""

from __future__ import annotations

import asyncio
import json
import re

from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _register_common_routes,
    _run_in_fresh_loop,
)

_AGENTS = [
    {
        "id": "ag_claude_e2e",
        "name": "claude-native-ui",
        "display_name": "Claude Code",
        "description": "Anthropic's coding agent",
        "harness": "claude-native",
        "skills": [],
    },
    {
        "id": "ag_codex_e2e",
        "name": "codex-native-ui",
        "display_name": "Codex",
        "description": "OpenAI's coding agent",
        "harness": "codex-native",
        "skills": [],
    },
]

_MODEL_ROWS = [
    {"id": "m1", "model": "model-one", "displayName": "Model One", "isDefault": False},
    {"id": "m2", "model": "model-two", "displayName": "Model Two", "isDefault": True},
]


def test_hotkey_drills_into_models_when_pressed_during_catalog_load(
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive(base_url, session_id))


async def _drive(base_url: str, session_id: str) -> None:
    release_agents = asyncio.Event()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_common_routes(page, created_session_id=session_id, create_bodies=[])
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(json={"data": []}),
            )
            await page.route(
                "**/v1/hosts/host_e2e/harnesses/*/model-options",
                lambda route: route.fulfill(json={"models": _MODEL_ROWS}),
            )

            async def gated_agents(route):
                await release_agents.wait()
                await route.fulfill(json={"data": _AGENTS})

            await page.route("**/v1/agents", gated_agents)

            await page.goto(f"{base_url}/")
            composer = page.get_by_test_id("new-chat-landing-input")
            await composer.wait_for(state="visible", timeout=30_000)
            await composer.click()

            # Press the chord once while the catalog is still resolving, then let
            # it resolve — the real fresh-screen timing a user hits.
            await page.keyboard.press("Control+Shift+M")
            release_agents.set()
            trigger = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(trigger).to_contain_text("Model Two", timeout=30_000)

            # The single press must land on the selected harness's Models submenu,
            # not the root harness list.
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models).to_be_visible(timeout=5_000)

            # ...and focus must sit on an actionable model item inside that submenu
            # so arrow keys navigate models immediately.
            focused_in_models = await page.evaluate(
                """() => {
                    const el = document.activeElement;
                    const submenu = document.querySelector(
                        '[data-testid="new-chat-landing-agent-models"]');
                    return !!(el && submenu && submenu.contains(el)
                        && el.getAttribute('role')?.startsWith('menuitem'));
                }"""
            )
            assert focused_in_models, (
                "Ctrl+Shift+M left focus outside the Models submenu; arrow keys "
                "cannot navigate models on the first press."
            )
        finally:
            await browser.close()
