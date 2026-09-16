"""E2E: a screenshot PNG larger than the server's read cap previews and downloads.

Screenshot-driven UI iteration drops large raster captures (a Retina/4K
screenshot easily exceeds the 10 MiB filesystem read cap) into the session
workspace. Opening one in the FileViewer must render the actual image, and
"Download file" must save the complete bytes.

The PNG is seeded by writing directly into the runner's materialized workspace
(the filesystem PUT endpoint only accepts text, and raster bytes are not valid
UTF-8), matching the seeding used by the existing over-cap download test.
"""

from __future__ import annotations

import io
import random
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_PNG_NAME = "ui-screenshot.png"


def _build_large_png(min_bytes: int) -> bytes:
    """Return a valid PNG strictly larger than ``min_bytes``.

    Deterministic random noise keeps the compressed stream near raw size, so a
    modest canvas lands past the cap.
    """
    from PIL import Image

    rng = random.Random(0)
    width, height = 2200, 1750
    image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    assert len(payload) > min_bytes, f"fixture PNG too small: {len(payload)} <= {min_bytes}"
    return payload


def _seed_large_png(
    page: Page,
    base_url: str,
    session_id: str,
    request: pytest.FixtureRequest,
) -> bytes:
    """Write an over-cap PNG into the session workspace and return its bytes."""
    from omnigent.runner.environment_filesystem import _MAX_READ_BYTES

    # Listing the root materializes a fresh session's workspace on the runner.
    listing = page.request.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    )
    assert listing.status == 200, listing.text()
    target = Path(listing.json()["base"]) / _PNG_NAME
    payload = _build_large_png(_MAX_READ_BYTES + 1)
    target.write_bytes(payload)
    request.addfinalizer(lambda: target.unlink(missing_ok=True))
    return payload


def test_large_png_previews_in_file_viewer(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """A screenshot PNG past the read cap renders as an image, not an error.

    The viewer's JSON read truncates at the server's cap and a truncated
    raster stream cannot render, so the viewer used to give up with
    "Image is too large to preview (truncated by the server)."
    """
    base_url, session_id = seeded_session
    _seed_large_png(page, base_url, session_id, request)

    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_PNG_NAME)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    img = file_viewer.locator(f'img[alt="{_PNG_NAME}"]')
    expect(img).to_be_visible(timeout=20_000)
    page.wait_for_function(
        "(el) => el.complete && el.naturalWidth > 0",
        arg=img.element_handle(),
        timeout=20_000,
    )
    expect(file_viewer.get_by_text("too large to preview")).to_have_count(0)
    expect(file_viewer.get_by_text("Unable to render image")).to_have_count(0)


def test_large_png_download_saves_complete_file(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """ "Download file" on an over-cap PNG saves every byte, uncorrupted."""
    base_url, session_id = seeded_session
    payload = _seed_large_png(page, base_url, session_id, request)

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    row = rail.get_by_role("button", name=re.compile(re.escape(_PNG_NAME))).filter(
        has_text=_PNG_NAME
    )
    expect(row).to_be_visible(timeout=30_000)
    row.click()
    expect(rail.get_by_test_id("file-viewer")).to_be_visible()
    rail.get_by_role("button", name="View settings").click()
    with page.expect_download() as download_info:
        page.get_by_role("menuitem", name="Download file").click()
    download = download_info.value
    assert download.suggested_filename == _PNG_NAME
    saved = tmp_path / _PNG_NAME
    download.save_as(saved)
    assert saved.read_bytes() == payload
