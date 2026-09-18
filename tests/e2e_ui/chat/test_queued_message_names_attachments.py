"""E2E: queued messages must identify their attached files in the queue strip.

A follow-up sent while the agent is busy is held client-side and rendered in
the composer's queued strip (``QueuedMessagesStrip``). Failure mode guarded
here: the strip renders only the message text, so a file-only queued message
shows as a blank row (controls with no identifying content) and a text+file
message shows the text without naming its attachment.

Journey:

1. Send a message; the mock LLM gate (``paused_mid_turn_session``) holds the
   turn open so the session stays busy, exactly like a long-running turn.
2. Attach a screenshot via the composer's hidden file input, leave the text
   empty, and click Send — the follow-up queues into the docked strip.
3. The queued row must identify the attachment by filename.
4. Repeat with text "Look at this": both the text and the filename must show.

Queued attachments are held as client-side ``File`` objects until the queue
flushes (no upload happens at enqueue time), so the strip is the only place a
user can identify a queued file before it is sent.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
from playwright.sync_api import Locator, Page, expect

_COMPOSER_LABEL = "Message the agent"
_HOLD_TURN_MSG = "hold this turn open for the queued-attachment check"
_MIXED_TEXT = "Look at this"
_FILE_NAME = "bug_screenshot.png"

# Smallest valid PNG (1x1 transparent) so the attachment is a real image the
# composer's accept filter and validators admit, like a real screenshot.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _wait_for_gate(page: Page, mock_url: str, *, timeout_s: float = 30.0) -> None:
    """Wait until the held turn is genuinely mid-flight (LLM call gated)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"]:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"mock LLM gate not pending within {timeout_s:.0f}s")


def _queue_followup_with_file(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
    tmp_path: Path,
    *,
    text: str,
) -> Locator:
    """Hold a turn open, then queue a follow-up carrying ``text`` + a screenshot.

    Returns the queued-strip locator once the follow-up is held in it.
    """
    base_url, session_id, mock_url = paused_mid_turn_session
    screenshot = tmp_path / _FILE_NAME
    screenshot.write_bytes(_PNG_BYTES)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill(_HOLD_TURN_MSG)
    page.get_by_role("button", name="Send", exact=True).click()
    _wait_for_gate(page, mock_url)

    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(screenshot))
    expect(page.get_by_role("button", name=f"Remove {_FILE_NAME}")).to_be_visible(timeout=10_000)
    if text:
        composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()

    strip = page.get_by_test_id("composer-queued-strip")
    expect(strip).to_be_visible(timeout=15_000)
    # Linger so the queued-row state under test is plainly visible in journey
    # recordings before assertions run.
    page.wait_for_timeout(1_500)
    return strip


def test_file_only_queued_message_identifies_its_file(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    """A queued attachment-only message must name its file, not render blank.

    Failure mode this catches: the queued row shows only ``message.text``, so
    with no text the row is empty chrome — the user cannot tell what (or even
    that anything) is queued.
    """
    strip = _queue_followup_with_file(page, paused_mid_turn_session, tmp_path, text="")
    expect(strip).to_contain_text(_FILE_NAME)


def test_text_and_file_queued_message_identifies_its_file(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    """A queued text+file message must show the text AND name its file."""
    strip = _queue_followup_with_file(page, paused_mid_turn_session, tmp_path, text=_MIXED_TEXT)
    expect(strip).to_contain_text(_MIXED_TEXT)
    expect(strip).to_contain_text(_FILE_NAME)
