"""E2E: viewing an attached screenshot from the chat composer.

An image attached to the composer renders only as a pill chip (icon +
filename + remove button), so a user who forgets what their screenshot
shows has no way to look at it before sending. The journey asserted here
is: attach a screenshot, then view it — either an inline thumbnail or a
preview reached from the attachment chip satisfies it.

The rendered preview is identified by the attachment's unique pixel
dimensions rather than a selector, so the test holds for any fix shape
(inline thumbnail, lightbox dialog, dedicated view button) without
pinning one implementation.
"""

from __future__ import annotations

import re
import struct
import time
import zlib
from pathlib import Path

from playwright.sync_api import Page, expect

_COMPOSER = "Send a message…"
_SHOT_NAME = "bug_screenshot.png"
# Dimensions no other <img> on the page plausibly has, so a rendered
# preview of this exact attachment is identifiable.
_SHOT_W = 345
_SHOT_H = 123


def _png_bytes(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x66\x33\x99" * width
    idat = zlib.compress(row * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


_PREVIEW_RENDERED = """
([w, h]) => {
  return Array.from(document.querySelectorAll("img")).some((img) => {
    if (img.naturalWidth !== w || img.naturalHeight !== h) return false;
    const rect = img.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
}
"""


def _preview_visible(page: Page, timeout_s: float = 0.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if page.evaluate(_PREVIEW_RENDERED, [_SHOT_W, _SHOT_H]):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)


def test_attached_screenshot_can_be_viewed(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """Attach a screenshot → the composer offers a way to actually see it."""
    base_url, session_id = seeded_session
    shot = tmp_path / _SHOT_NAME
    shot.write_bytes(_png_bytes(_SHOT_W, _SHOT_H))

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(shot))

    # Premise: attaching works — this test must fail on viewing, not attaching.
    expect(page.get_by_role("button", name=f"Remove {_SHOT_NAME}")).to_be_visible(timeout=10_000)
    chip_label = page.get_by_text(_SHOT_NAME, exact=True)
    expect(chip_label).to_be_visible()
    page.wait_for_timeout(500)

    if not _preview_visible(page):
        # No inline thumbnail: the user's gesture is to click the attachment.
        # Prefer a dedicated non-remove control on the chip when one exists.
        chip = chip_label.locator("xpath=..")
        clicked = False
        buttons = chip.get_by_role("button")
        for i in range(buttons.count()):
            button = buttons.nth(i)
            label = (button.get_attribute("aria-label") or button.inner_text() or "").strip()
            if not re.match(r"remove\b", label, re.IGNORECASE):
                button.click()
                clicked = True
                break
        if not clicked:
            chip_label.click()

    shown = _preview_visible(page, timeout_s=4.0)
    page.wait_for_timeout(1_500)
    assert shown, (
        f"attached screenshot {_SHOT_NAME} cannot be viewed from the composer: "
        "no rendered preview of the image appeared inline or after clicking the "
        "attachment chip (the chip offers only a remove button)"
    )
