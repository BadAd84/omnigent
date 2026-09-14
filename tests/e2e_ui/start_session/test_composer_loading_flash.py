"""E2E: composer pickers must not flash unavailable states while metadata loads.

While the landing composer's metadata requests (``GET /v1/agents``,
``GET /v1/hosts/{id}/harnesses/claude-native/model-options``) are still in
flight, the trigger must not render the definitive empty-state copy
("No agents", "Models unavailable") or disable itself; a loading state (or
the previously confirmed labels) must keep remembered controls usable.

Only the metadata edges are stubbed -- the real SPA runs against the spawned
server, and holding a stubbed response open stands in for a slow network
delaying the agent/host/model responses.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from playwright.async_api import Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _close_entry_models,
    _hosts_body,
    _open_entry_models,
    _run_in_fresh_loop,
)

_MODEL_OPTIONS = [
    {
        "id": "opus",
        "model": "system.ai.claude-opus-4-10",
        "displayName": "Opus 4.10",
        "isDefault": False,
    },
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": True,
    },
]

# One sample interval under each never-shows window; a buggy render is caught
# on the first samples, a clean run costs the full window once.
_NEVER_SHOWS_WINDOW_S = 5.0


class _Gate:
    """A stubbed JSON response that can be held open (request in flight)."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.open = True
        self._release = asyncio.Event()

    def hold(self) -> None:
        """Keep matching requests pending until :meth:`release`."""
        self.open = False

    def release(self) -> None:
        """Let held (and future) requests complete."""
        self.open = True
        self._release.set()

    async def handle(self, route: Route) -> None:
        if not self.open:
            await self._release.wait()
        await route.fulfill(status=200, content_type="application/json", body=self.body)


async def _register_metadata_routes(
    page: Page,
    *,
    agents: _Gate,
    hosts: _Gate,
    claude_models: _Gate,
) -> None:
    """Stub the landing composer's metadata edges with holdable responses.

    :param page: Playwright page, before navigation.
    :param agents: Gate for ``GET /v1/agents``.
    :param hosts: Gate for ``GET /v1/hosts``.
    :param claude_models: Gate for the claude-native model-options catalog.
    """
    await page.route("**/v1/agents", agents.handle)
    await page.route("**/v1/hosts", hosts.handle)
    await page.route(
        f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
        claude_models.handle,
    )
    # The sibling native catalogs are irrelevant here; keep them empty and
    # instant so no real-server 404 noise races the assertions. (Single-arg
    # handler on purpose: a second parameter would receive the Request.)
    empty_models = json.dumps({"models": []})

    async def _fulfill_empty_models(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=empty_models)

    for harness in ("codex-native", "pi-native"):
        await page.route(
            f"**/v1/hosts/{_HOST_ID}/harnesses/{harness}/model-options",
            _fulfill_empty_models,
        )
    # Neutralize agent discovery so only the stubbed built-in shows; leftover
    # sessions on the shared e2e_ui server would otherwise leak in.
    await page.route(
        re.compile(r"/v1/sessions\?.*kind=any"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        ),
    )


def _default_gates() -> dict[str, _Gate]:
    """Fresh gates for the three metadata responses, all initially instant."""
    return {
        "agents": _Gate(_agents_body()),
        "hosts": _Gate(_hosts_body()),
        "claude_models": _Gate(json.dumps({"models": _MODEL_OPTIONS})),
    }


async def _seed_host_workspace(page: Page) -> None:
    """Remember a real host workspace so the claude catalog fetch is enabled.

    Model options are only fetched for a real (non-sandbox) host --
    ``useHostModelOptions(hostId, "claude-native", !sandbox)``.
    """
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


async def _assert_never_shows(page: Page, needle: str, *, while_loading: str) -> None:
    """Fail if *needle* renders in the agent/model trigger during the window.

    A plain ``not_to_contain_text`` passes the instant the text is absent --
    including the pre-render frame -- so it can false-pass on the buggy build.
    Sampling the whole window catches the flash whenever it appears.

    :param page: Playwright page showing the landing composer.
    :param needle: Empty-state copy that must not render, e.g. ``"No agents"``.
    :param while_loading: The request being delayed, for the failure message.
    """
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _NEVER_SHOWS_WINDOW_S
    while loop.time() < deadline:
        if await trigger.count():
            text = await trigger.inner_text()
            if needle in text:
                await _screenshot(page, f"composer-loading-{needle.lower().replace(' ', '-')}")
                # Dwell on the buggy state so a recorded run shows it clearly.
                await asyncio.sleep(1.5)
                raise AssertionError(
                    f"composer trigger rendered the empty-state copy {needle!r} "
                    f"while {while_loading} was still loading -- expected a "
                    f"loading state (or the previously confirmed label) instead"
                )
        await asyncio.sleep(0.15)


async def _screenshot(page: Page, name: str) -> None:
    """Save a demo screenshot when E2E_SCREENSHOT_DIR is set (local runs)."""
    shot_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    if shot_dir:
        await page.screenshot(path=str(Path(shot_dir) / f"{name}.png"))


def test_cold_load_pending_agents_never_claims_no_agents(
    seeded_session: tuple[str, str],
) -> None:
    """A pending ``/v1/agents`` must read as loading, not "No agents".

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, _session_id = seeded_session
    _run_in_fresh_loop(_drive_cold_load_agents_delay(base_url))


async def _drive_cold_load_agents_delay(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        gates = _default_gates()
        try:
            await _register_metadata_routes(page, **gates)
            await _seed_host_workspace(page)
            # The slow network: the agent list stays in flight while the
            # composer renders.
            gates["agents"].hold()

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            trigger = page.get_by_test_id("new-chat-landing-agent-select")
            # While metadata loads, the picker row shows either the trigger
            # (a remembered label) or an explicit loading placeholder; a build
            # rendering neither fails here instead of sampling a blank row.
            picker_row = trigger.or_(page.get_by_test_id("new-chat-landing-picker-loading"))
            await expect(picker_row.first).to_be_visible(timeout=15_000)
            await _screenshot(page, "composer-loading-cold-load-agents-pending")

            # The bug: the trigger claims "No agents" for the whole window the
            # request is pending.
            await _assert_never_shows(page, "No agents", while_loading="GET /v1/agents")

            # The journey completes: once the response lands, the real agent
            # and its default model label appear.
            gates["agents"].release()
            await expect(trigger).to_contain_text("Sonnet 5", timeout=15_000)
            await _screenshot(page, "composer-loading-cold-load-agents-loaded")
        finally:
            for gate in gates.values():
                gate.release()
            # Let any just-released handlers fulfill before teardown.
            await page.wait_for_timeout(200)
            await context.close()
            await browser.close()


def test_pending_model_catalog_never_claims_models_unavailable(
    seeded_session: tuple[str, str],
) -> None:
    """A pending model catalog must read as loading, not "Models unavailable".

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, _session_id = seeded_session
    _run_in_fresh_loop(_drive_model_catalog_delay(base_url))


async def _drive_model_catalog_delay(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        gates = _default_gates()
        try:
            await _register_metadata_routes(page, **gates)
            await _seed_host_workspace(page)
            # Agents and hosts land fast; only the catalog is slow.
            gates["claude_models"].hold()

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            trigger = page.get_by_test_id("new-chat-landing-agent-select")
            # With agents landed and only the catalog in flight, the picker row
            # renders either the loaded trigger or an explicit loading
            # placeholder -- never the definitive empty-state copy.
            picker_row = trigger.or_(page.get_by_test_id("new-chat-landing-picker-loading"))
            await expect(picker_row.first).to_be_visible(timeout=15_000)
            await _screenshot(page, "composer-loading-model-catalog-pending")

            # The bug: the trigger claims "Models unavailable" while the
            # catalog request is merely in flight.
            await _assert_never_shows(
                page,
                "Models unavailable",
                while_loading=f"GET /v1/hosts/{_HOST_ID}/.../model-options",
            )

            gates["claude_models"].release()
            await expect(trigger).to_contain_text("Sonnet 5", timeout=15_000)
            await _screenshot(page, "composer-loading-model-catalog-loaded")
        finally:
            for gate in gates.values():
                gate.release()
            await page.wait_for_timeout(200)
            await context.close()
            await browser.close()


def test_reload_keeps_remembered_picker_usable_while_metadata_refreshes(
    seeded_session: tuple[str, str],
) -> None:
    """After a confirmed pick, a reload with slow metadata keeps the control.

    The user picked Opus 4.10 on a fully loaded composer; reloading while
    every metadata request hangs must keep the trigger enabled and labeled
    with the previously confirmed pick, not flip it to a disabled
    "No agents".

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, _session_id = seeded_session
    _run_in_fresh_loop(_drive_reload_remembered_pick(base_url))


async def _drive_reload_remembered_pick(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        gates = _default_gates()
        try:
            await _register_metadata_routes(page, **gates)
            await _seed_host_workspace(page)

            # First load: everything is fast; confirm the catalog and pick a
            # concrete model, which the composer remembers per harness.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            trigger = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(trigger).to_contain_text("Sonnet 5", timeout=15_000)
            await _open_entry_models(page, "ag_claude_e2e")
            await page.get_by_test_id("new-chat-landing-agent-model-opus").click()
            await _close_entry_models(page)
            await expect(trigger).to_contain_text("Opus 4.10", timeout=15_000)
            await _screenshot(page, "composer-loading-reload-pick-confirmed")

            # Reload with every metadata request hanging -- a new tab or
            # refresh whose menus are tried before metadata returns.
            for gate in gates.values():
                gate.hold()
            await page.reload()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(trigger).to_be_visible(timeout=15_000)
            await _screenshot(page, "composer-loading-reload-metadata-pending")

            # The bug (all three fail on the flashing build, which renders a
            # disabled "No agents" trigger instead): the remembered control
            # stays labeled and immediately clickable while live data
            # refreshes.
            await _assert_never_shows(
                page, "No agents", while_loading="the reload's metadata refresh"
            )
            await expect(trigger).to_contain_text("Opus 4.10", timeout=4_000)
            await expect(trigger).to_be_enabled(timeout=4_000)

            # Live metadata replaces the cache: the confirmed pick survives.
            for gate in gates.values():
                gate.release()
            await expect(trigger).to_contain_text("Opus 4.10", timeout=15_000)
            await _screenshot(page, "composer-loading-reload-metadata-loaded")
        finally:
            for gate in gates.values():
                gate.release()
            await page.wait_for_timeout(200)
            await context.close()
            await browser.close()


def test_reload_keeps_permission_control_present_while_metadata_refreshes(
    seeded_session: tuple[str, str],
) -> None:
    """The permissions control must not vanish-and-jump back on a slow reload.

    After a fully loaded visit rendered the claude permission-mode chip, a
    reload whose metadata requests are still in flight must keep that
    remembered control present instead of dropping it until the agent list
    arrives (the controls would otherwise jump in as metadata lands).

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, _session_id = seeded_session
    _run_in_fresh_loop(_drive_reload_permission_control(base_url))


async def _drive_reload_permission_control(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        gates = _default_gates()
        try:
            await _register_metadata_routes(page, **gates)
            await _seed_host_workspace(page)

            # First load: metadata is fast; the claude agent's permission-mode
            # chip is one of the confirmed composer controls.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            permission_chip = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(permission_chip).to_be_visible(timeout=15_000)
            await _screenshot(page, "composer-loading-permission-chip-confirmed")

            # Reload with the metadata refresh hanging.
            for gate in gates.values():
                gate.hold()
            await page.reload()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _screenshot(page, "composer-loading-permission-chip-pending")

            # The bug: the remembered control disappears for the whole pending
            # window and only jumps back in when the agent list arrives.
            await expect(permission_chip).to_be_visible(timeout=4_000)

            for gate in gates.values():
                gate.release()
            await expect(permission_chip).to_be_visible(timeout=15_000)
            await _screenshot(page, "composer-loading-permission-chip-loaded")
        finally:
            for gate in gates.values():
                gate.release()
            await page.wait_for_timeout(200)
            await context.close()
            await browser.close()
