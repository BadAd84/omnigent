import { useCallback, useEffect, useRef } from "react";
import { browserViewId } from "@/hooks/useBrowserTabs";
import type { DesignModeSubmit } from "@/lib/browserDesignMode";
import {
  buildDesignModePrompt,
  dataUrlToFile,
  type DesignModeElement,
} from "@/lib/designModePrompt";
import { supportsBrowser } from "@/lib/nativeBridge";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { isTempConvId, useChatStore } from "@/store/chatStore";

interface LegacyDesignBridge {
  onBrowserElementSelected?: (
    callback: (payload: {
      conversationId?: string;
      selectionId?: string;
      screenshot?: string | null;
    }) => void,
  ) => () => void;
  onBrowserElementPromptSubmit?: (
    callback: (payload: {
      conversationId?: string;
      id?: number;
      element?: DesignModeElement;
      prompt?: string;
    }) => void,
  ) => () => void;
  onBrowserElementPromptDismiss?: (
    callback: (payload: { conversationId?: string }) => void,
  ) => () => void;
  onBrowserUrlChanged?: (callback: (payload: { conversationId: string }) => void) => () => void;
  browserSignalDesignResult?: (
    viewId: string,
    result: { id: number; ok: boolean; message: string },
  ) => Promise<{ ok: boolean; error?: string }>;
}

function selectedViewId(sessionId: string): string {
  const saved = readSessionWorkspaceState(sessionId);
  const selected = saved.selectedBrowserId;
  return browserViewId(
    sessionId,
    selected && saved.openBrowsers?.includes(selected) ? selected : null,
  );
}

/** Route both shell-owned instructions and older desktop prompts through chat. */
export function useBrowserDesignPrompt(
  conversationId: string | undefined,
  boundAgentId: string | null,
): (request: DesignModeSubmit) => Promise<void> {
  const scope = useRef({ conversationId, boundAgentId });
  scope.current = { conversationId, boundAgentId };
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const submit = useCallback(
    async (request: DesignModeSubmit) => {
      const store = useChatStore.getState();
      if (
        !mounted.current ||
        !conversationId ||
        isTempConvId(conversationId) ||
        scope.current.conversationId !== conversationId ||
        scope.current.boundAgentId !== boundAgentId ||
        store.conversationId !== conversationId ||
        selectedViewId(conversationId) !== request.conversationId
      ) {
        throw new Error(
          "The browser selection is no longer in the active session. Select it again.",
        );
      }
      if (!boundAgentId) throw new Error("No agent bound to this session yet.");
      if (!request.prompt.trim()) throw new Error("Describe the change before sending.");
      const text = buildDesignModePrompt(request.element, request.prompt);
      const file = dataUrlToFile(request.screenshot, "design-element.png");
      await store.send(text, boundAgentId, file ? [file] : undefined, {
        pinnedConversationId: conversationId,
        rejectOnError: true,
      });
    },
    [conversationId, boundAgentId],
  );

  // Compatibility with desktop versions lacking the shell-prompt bridge.
  useEffect(() => {
    if (!supportsBrowser() || !conversationId) return;
    const desktop = (window as unknown as { omnigentDesktop?: LegacyDesignBridge }).omnigentDesktop;
    if (!desktop) return;
    let screenshot: { viewId: string; data: string | null } | null = null;
    const clearScreenshot = (payload: { conversationId?: string }) => {
      if (screenshot?.viewId === payload.conversationId) screenshot = null;
    };
    const unsubSelected = desktop.onBrowserElementSelected?.((payload) => {
      if (payload.selectionId || payload.conversationId !== selectedViewId(conversationId)) return;
      screenshot = {
        viewId: payload.conversationId,
        data: typeof payload.screenshot === "string" ? payload.screenshot : null,
      };
    });
    const unsubSubmit = desktop.onBrowserElementPromptSubmit?.((payload) => {
      const viewId = payload.conversationId;
      if (!viewId) return;
      const signal = (ok: boolean, message: string) => {
        void desktop
          .browserSignalDesignResult?.(viewId, { id: payload.id ?? 0, ok, message })
          .catch(() => {});
      };
      if (!payload.element || !payload.prompt) {
        signal(false, "Missing element or prompt.");
        return;
      }
      const shot = screenshot?.viewId === viewId ? screenshot.data : null;
      screenshot = null;
      void submit({
        conversationId: viewId,
        selectionId: `legacy-${payload.id ?? 0}`,
        element: payload.element,
        screenshot: shot,
        prompt: payload.prompt,
      }).then(
        () => signal(true, "Sent to agent."),
        (error: unknown) => signal(false, `Send failed: ${String(error)}`),
      );
    });
    const unsubDismiss = desktop.onBrowserElementPromptDismiss?.(clearScreenshot);
    const unsubNavigate = desktop.onBrowserUrlChanged?.(clearScreenshot);
    return () => {
      screenshot = null;
      unsubSelected?.();
      unsubSubmit?.();
      unsubDismiss?.();
      unsubNavigate?.();
    };
  }, [conversationId, submit]);

  return submit;
}
