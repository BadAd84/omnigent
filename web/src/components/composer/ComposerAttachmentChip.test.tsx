// Tests for the composer attachment chip: an image attachment opens in the
// shared lightbox when clicked (so the user can check a screenshot before
// sending), non-image attachments stay a plain label, and the preview's
// object URL is created lazily and released on unmount.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// getEmbedRoot decides the Radix portal container; null → portal to body.
vi.mock("@/lib/host", () => ({
  getEmbedRoot: () => null,
}));

import { ImageLightboxProvider } from "@/components/ImageLightbox";
import { ComposerAttachmentChip } from "./ComposerAttachmentChip";

// jsdom has no createObjectURL/revokeObjectURL; the chip's preview needs both.
let blobUrlCounter = 0;
const createObjectURL = vi.fn((_blob: Blob | MediaSource) => `blob:mock-${++blobUrlCounter}`);
const revokeObjectURL = vi.fn();
const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

beforeEach(() => {
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
});

const IMAGE_FILE = () => new File(["png-bytes"], "shot.png", { type: "image/png" });
const TEXT_FILE = () => new File(["hello"], "notes.txt", { type: "text/plain" });

function renderChip(file: File, onRemove = vi.fn()) {
  const result = render(
    <ImageLightboxProvider>
      <ComposerAttachmentChip file={file} onRemove={onRemove} />
    </ImageLightboxProvider>,
  );
  return { ...result, onRemove };
}

describe("ComposerAttachmentChip", () => {
  it("opens an attached image in the lightbox when its chip is clicked", () => {
    renderChip(IMAGE_FILE());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // No preview bytes are materialized until the user asks to view.
    expect(createObjectURL).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "View shot.png" }));

    const dialog = screen.getByRole("dialog");
    const img = screen.getByRole("img", { name: "shot.png" });
    expect(dialog).toContainElement(img);
    expect(img).toHaveAttribute("src", createObjectURL.mock.results[0]!.value);
  });

  it("reuses one object URL across repeated views", () => {
    renderChip(IMAGE_FILE());
    const view = screen.getByRole("button", { name: "View shot.png" });
    fireEvent.click(view);
    fireEvent.keyDown(document.body, { key: "Escape" });
    fireEvent.click(view);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("releases the preview object URL when the chip unmounts", () => {
    const { unmount } = renderChip(IMAGE_FILE());
    fireEvent.click(screen.getByRole("button", { name: "View shot.png" }));
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith(createObjectURL.mock.results[0]!.value);
  });

  it("does not revoke anything when the image was never viewed", () => {
    const { unmount } = renderChip(IMAGE_FILE());
    unmount();
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });

  it("offers no view control for a non-image attachment", () => {
    renderChip(TEXT_FILE());
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View notes.txt" })).not.toBeInTheDocument();
    // The only control is Remove.
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Remove notes.txt" })).toBeInTheDocument();
  });

  it("removes the attachment via the Remove button", () => {
    const { onRemove } = renderChip(IMAGE_FILE());
    fireEvent.click(screen.getByRole("button", { name: "Remove shot.png" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
