"""Cold loads spin; refreshes and new tabs immediately reuse the last picker label."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from playwright.async_api import Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _hosts_body,
    _run_in_fresh_loop,
)

_MODEL_LABEL = "Opus 5"


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("first_response", ["agents", "hosts"])
def test_picker_loading_handoff(
    live_server: str, browser_name: str, tmp_path: Path, width: int, first_response: str
) -> None:
    """Live configuration replaces the spinner/cache without intermediate empty labels."""
    _run_in_fresh_loop(_drive(live_server, browser_name, tmp_path, width, first_response))


async def _paint_frames(page: Page) -> None:
    await page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


async def _expect_pending(
    page: Page, cached_label: str | None, *, workspace_pending: bool = True
) -> None:
    loading = page.get_by_role("status", name="Loading session configuration")
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    if cached_label is None:
        await expect(loading).to_be_visible(timeout=30_000)
        await expect(picker).to_have_count(0)
    else:
        await expect(picker).to_contain_text(cached_label, timeout=30_000)
        await expect(picker).to_contain_text("Max")
        await expect(picker).to_be_disabled()
        await expect(picker).to_have_attribute("aria-busy", "true")
        await expect(loading).to_have_count(0)
    permissions_loading = page.get_by_role("status", name="Loading permissions")
    permissions = page.get_by_test_id("new-chat-landing-permission-chip")
    if cached_label is None:
        await expect(permissions_loading).to_be_visible()
        await expect(permissions).to_have_count(0)
    else:
        await expect(permissions).to_have_text("Plan")
        await expect(permissions).to_be_disabled()
        await expect(permissions).to_have_attribute("aria-busy", "true")
        await expect(permissions_loading).to_have_count(0)
    workspace_loading = page.get_by_role("status", name="Loading working directory")
    workspace = page.get_by_test_id("new-chat-landing-workspace-chip")
    if workspace_pending and cached_label is None:
        await expect(workspace_loading).to_be_visible()
        await expect(workspace).to_have_count(0)
    else:
        await expect(workspace).to_have_text("repo")
        await expect(workspace).to_have_attribute("title", "/work/repo")
        await expect(workspace_loading).to_have_count(0)
        if workspace_pending:
            await expect(workspace).to_be_disabled()
            await expect(workspace).to_have_attribute("aria-busy", "true")
        else:
            await expect(workspace).to_be_enabled()


async def _drive(
    base_url: str, browser_name: str, output: Path, width: int, first_response: str
) -> None:
    async with async_playwright() as playwright:
        browser = await getattr(playwright, browser_name).launch()
        context = await browser.new_context(viewport={"width": width, "height": 900})
        page = await context.new_page()
        gates = {key: asyncio.Event() for key in ("agents", "hosts", "models", "worktrees")}
        served = {key: asyncio.Event() for key in ("agents", "hosts")}
        models_requested = asyncio.Event()
        live_label = _MODEL_LABEL
        cached_label: str | None = None
        try:

            async def agents(route: Route) -> None:
                await gates["agents"].wait()
                await route.fulfill(content_type="application/json", body=_agents_body())
                served["agents"].set()

            async def hosts(route: Route) -> None:
                await gates["hosts"].wait()
                await route.fulfill(content_type="application/json", body=_hosts_body())
                served["hosts"].set()

            async def models(route: Route) -> None:
                if "/claude-native/" not in route.request.url:
                    await route.fulfill(json={"models": []})
                    return
                models_requested.set()
                await gates["models"].wait()
                await route.fulfill(
                    json={
                        "models": [
                            {"id": "sonnet", "displayName": "Sonnet 4.6", "isDefault": True},
                            {
                                "id": "opus",
                                "model": "system.ai.claude-opus-5",
                                "displayName": live_label,
                            },
                        ]
                    }
                )

            async def worktrees(route: Route) -> None:
                await gates["worktrees"].wait()
                await route.fulfill(
                    json={
                        "data": [
                            {
                                "path": "/work/repo",
                                "branch": "main",
                                "is_main": True,
                                "detached": False,
                            }
                        ]
                    }
                )

            async def info(route: Route) -> None:
                response = await route.fetch()
                body = await response.json()
                body.update(managed_sandboxes_enabled=False, smart_routing_enabled=False)
                await route.fulfill(response=response, json=body)

            await context.route(re.compile(r"/v1/agents(?:\?.*)?$"), agents)
            await context.route(re.compile(r"/v1/hosts(?:\?.*)?$"), hosts)
            await context.route("**/v1/hosts/*/harnesses/*/model-options", models)
            await context.route("**/v1/hosts/*/worktrees?*", worktrees)
            await context.route("**/v1/info", info)
            await context.route(
                re.compile(r"/v1/sessions(?:\?.*)?$"),
                lambda route: route.fulfill(json={"data": [], "has_more": False}),
            )
            await context.add_init_script(
                f"""localStorage.setItem('omnigent:last-agent-id', 'ag_claude_e2e');
                localStorage.setItem('omnigent:last-host-choice', '{_HOST_ID}');
                localStorage.setItem('omnigent:recent-workspaces',
                    JSON.stringify({{{_HOST_ID}: ['/work/repo']}}));
                localStorage.setItem('omnigent:last-mode-by-harness', JSON.stringify({{
                    'claude-native': {{model: 'opus', effort: 'max', routing: 'off', mode: 'plan'}}
                }}));"""
            )
            await context.add_init_script(
                """window.pickerLoadingSamples = [];
                window.directoryLoadingSamples = [];
                window.permissionLoadingSamples = [];
                const capture = () => {
                    const loading = document.querySelector(
                        '[data-testid="new-chat-landing-picker-loading"]');
                    const picker = document.querySelector(
                        '[data-testid="new-chat-landing-agent-select"]');
                    const value = loading ? 'loading' : picker?.textContent;
                    if (value && window.pickerLoadingSamples.at(-1) !== value) {
                        window.pickerLoadingSamples.push(value);
                    }
                    for (const [control, samples] of [
                        ['workspace', window.directoryLoadingSamples],
                        ['permission', window.permissionLoadingSamples]
                    ]) {
                        const pending = document.querySelector(
                            `[data-testid="new-chat-landing-${control}-loading"]`);
                        const trigger = document.querySelector(
                            `[data-testid="new-chat-landing-${control}-chip"]`);
                        const label = pending ? 'loading' : trigger?.textContent;
                        if (label && samples.at(-1) !== label) samples.push(label);
                    }
                };
                new MutationObserver(capture).observe(document, {
                    childList: true, subtree: true, characterData: true
                });
                const sampleFrame = () => { capture(); requestAnimationFrame(sampleFrame); };
                requestAnimationFrame(sampleFrame);"""
            )

            for visit in ("fresh", "reload", "new-tab"):
                for gate in (*gates.values(), *served.values(), models_requested):
                    gate.clear()
                if visit == "fresh":
                    await page.goto(f"{base_url}/")
                elif visit == "reload":
                    live_label = "Opus 5 (Team gateway)"
                    await page.reload()
                else:
                    previous_page = page
                    page = await context.new_page()
                    await previous_page.close()
                    await page.goto(f"{base_url}/")

                loading = page.get_by_role("status", name="Loading session configuration")
                picker = page.get_by_test_id("new-chat-landing-agent-select")
                composer = page.get_by_test_id("new-chat-landing-input")

                await _expect_pending(page, cached_label)
                await expect(composer).to_be_enabled()
                await composer.fill("I can type while configuration loads")
                await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_disabled()

                gates[first_response].set()
                await asyncio.wait_for(served[first_response].wait(), timeout=10)
                await _paint_frames(page)
                await _expect_pending(page, cached_label)

                gates["hosts" if first_response == "agents" else "agents"].set()
                await asyncio.wait_for(models_requested.wait(), timeout=10)
                await _paint_frames(page)
                await _expect_pending(page, cached_label)
                await page.get_by_test_id("new-chat-landing").screenshot(
                    path=output / f"{visit}-loading.png", animations="disabled"
                )

                gates["worktrees"].set()
                await expect(
                    page.get_by_test_id("new-chat-landing-workspace-chip")
                ).to_be_enabled()
                await _expect_pending(page, cached_label, workspace_pending=False)

                gates["models"].set()
                await expect(picker).to_contain_text(live_label)
                await expect(picker).to_contain_text("Max")
                await expect(picker).to_be_enabled()
                await expect(loading).to_have_count(0)
                await expect(composer).to_have_value("I can type while configuration loads")
                await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
                await expect(
                    page.get_by_test_id("new-chat-landing-permission-chip")
                ).to_be_enabled()
                await expect(page.get_by_test_id("new-chat-landing-permission-chip")).to_have_text(
                    "Plan"
                )
                await _paint_frames(page)
                await page.get_by_test_id("new-chat-landing").screenshot(
                    path=output / f"{visit}-ready.png", animations="disabled"
                )

                samples = await page.evaluate("window.pickerLoadingSamples")
                if cached_label is None:
                    assert samples[0] == "loading", samples
                    assert len(samples) > 1, samples
                    assert all(live_label in label and "Max" in label for label in samples[1:]), (
                        samples
                    )
                else:
                    # The boot identity probe must confirm whose cache to read.
                    cached_samples = samples[1:] if samples[0] == "loading" else samples
                    assert cached_label in cached_samples[0], samples
                    assert all(
                        (cached_label in label or live_label in label) and "Max" in label
                        for label in cached_samples
                    ), samples
                for samples_name, expected_label in (
                    ("directoryLoadingSamples", "repo"),
                    ("permissionLoadingSamples", "Plan"),
                ):
                    control_samples = await page.evaluate(f"window.{samples_name}")
                    labels = (
                        control_samples[1:] if control_samples[0] == "loading" else control_samples
                    )
                    assert labels and all(label == expected_label for label in labels), (
                        control_samples
                    )
                cached_label = live_label
        finally:
            for gate in gates.values():
                gate.set()
            await context.unroute_all(behavior="wait")
            await browser.close()
