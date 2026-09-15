import { useEffect, useRef } from "react";
import { FileTextIcon, ImageIcon, XIcon } from "lucide-react";

import { useLightbox } from "@/components/ImageLightbox";

export interface ComposerAttachmentChipProps {
  /** The attached (not yet uploaded) file this chip represents. */
  file: File;
  /** Remove this attachment from the composer. */
  onRemove: () => void;
}

/**
 * Pill chip for a file attached to a message composer. An image attachment
 * opens in the shared lightbox when clicked, so the user can check what a
 * screenshot shows before sending; other attachments render a plain label.
 */
export function ComposerAttachmentChip({ file, onRemove }: ComposerAttachmentChipProps) {
  const { open } = useLightbox();
  // Object URL handed to the lightbox for the not-yet-uploaded File. Created
  // on first view rather than on render (jsdom has no createObjectURL, and
  // most attachments are never previewed), released when the chip unmounts.
  const previewUrlRef = useRef<string | null>(null);
  useEffect(
    () => () => {
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    },
    [],
  );

  const name = file.name || "image.png";
  const isImage = file.type.startsWith("image/");
  const label = (
    <>
      {isImage ? (
        <ImageIcon className="size-3 shrink-0" />
      ) : (
        <FileTextIcon className="size-3 shrink-0" />
      )}
      <span className="max-w-[140px] truncate">{name}</span>
    </>
  );

  return (
    <span className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground">
      {isImage ? (
        <button
          type="button"
          onClick={() => {
            previewUrlRef.current ??= URL.createObjectURL(file);
            open({ src: previewUrlRef.current, alt: name });
          }}
          className="flex cursor-zoom-in items-center gap-1 hover:text-foreground"
          aria-label={`View ${name}`}
        >
          {label}
        </button>
      ) : (
        label
      )}
      <button
        type="button"
        onClick={onRemove}
        className="ml-0.5 rounded-full hover:text-foreground"
        aria-label={`Remove ${name}`}
      >
        <XIcon className="size-3" />
      </button>
    </span>
  );
}
