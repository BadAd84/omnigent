import type { DesignModeElement } from "@/lib/designModePrompt";

/** A main-process selection ticket, scoped to one native browser view. */
export interface DesignModeSelection {
  conversationId: string;
  selectionId: string;
  element: DesignModeElement;
  screenshot: string | null;
}

export interface DesignModeSubmit extends DesignModeSelection {
  prompt: string;
}
