import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browserViewId } from "@/hooks/useBrowserTabs";
import type { DesignModeSubmit } from "@/lib/browserDesignMode";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { useBrowserDesignPrompt } from "./useBrowserDesignPrompt";

const chat = vi.hoisted(() => ({ conversationId: "session-1", send: vi.fn() }));
vi.mock("@/store/chatStore", () => ({
  useChatStore: { getState: () => chat },
  isTempConvId: (id: string) => id.startsWith("temp:"),
}));
vi.mock("@/lib/nativeBridge", () => ({ supportsBrowser: () => true }));

const request: DesignModeSubmit = {
  conversationId: "session-1",
  selectionId: "selection-1",
  element: { tag: "input", id: "#period", label: "Period" },
  screenshot: "data:image/png;base64,AQID",
  prompt: "Add a week picker",
};

function legacyBridge() {
  const listeners = new Map<string, (payload: Record<string, unknown>) => void>();
  const unsubscribe = vi.fn();
  const subscribe = (name: string) => (callback: (payload: Record<string, unknown>) => void) => {
    listeners.set(name, callback);
    return unsubscribe;
  };
  const bridge = {
    onBrowserElementSelected: subscribe("selected"),
    onBrowserElementPromptSubmit: subscribe("submit"),
    onBrowserElementPromptDismiss: subscribe("dismiss"),
    onBrowserUrlChanged: subscribe("navigate"),
    browserSignalDesignResult: vi.fn().mockResolvedValue({ ok: true }),
  };
  Object.assign(window, { omnigentDesktop: bridge });
  return {
    ...bridge,
    unsubscribe,
    emit: (name: string, payload: Record<string, unknown>) => listeners.get(name)?.(payload),
  };
}

beforeEach(() => {
  chat.conversationId = "session-1";
  chat.send.mockReset().mockResolvedValue(undefined);
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("shell design instructions", () => {
  it("sends the selected element and exact screenshot through the pinned chat path", async () => {
    const { result } = renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    await result.current(request);
    expect(chat.send).toHaveBeenCalledWith(
      expect.stringContaining("Add a week picker\n\n---\n[Design Mode"),
      "agent-1",
      [expect.any(File)],
      { pinnedConversationId: "session-1", rejectOnError: true },
    );
    const [text, , files] = chat.send.mock.calls[0]!;
    expect(text).toContain("CSS selector: #period");
    expect(text).toContain('Label: "Period"');
    expect(files[0]).toMatchObject({ name: "design-element.png", type: "image/png", size: 3 });
  });

  it("does not reuse a screenshot when the next selection has none", async () => {
    const { result } = renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    await result.current(request);
    await result.current({ ...request, selectionId: "selection-2", screenshot: null });
    expect(chat.send.mock.calls[1]?.[2]).toBeUndefined();
  });

  it("maps a session-scoped browser tab to its owning session", async () => {
    writeSessionWorkspaceState("session-1", {
      openBrowsers: ["tab-1"],
      selectedBrowserId: "tab-1",
    });
    const { result } = renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    await result.current({ ...request, conversationId: browserViewId("session-1", "tab-1") });
    expect(chat.send.mock.calls[0]?.[3]).toEqual({
      pinnedConversationId: "session-1",
      rejectOnError: true,
    });
  });

  it("rejects a different session or a no-longer-selected browser tab", async () => {
    const { result } = renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    await expect(result.current({ ...request, conversationId: "session-2" })).rejects.toThrow(
      "active session",
    );
    writeSessionWorkspaceState("session-1", {
      openBrowsers: ["tab-1", "tab-2"],
      selectedBrowserId: "tab-2",
    });
    await expect(
      result.current({ ...request, conversationId: browserViewId("session-1", "tab-1") }),
    ).rejects.toThrow("active session");
    expect(chat.send).not.toHaveBeenCalled();
  });

  it("rejects a delayed callback after the session or bound agent changes", async () => {
    const { result, rerender } = renderHook(
      ({ session, agent }) => useBrowserDesignPrompt(session, agent),
      { initialProps: { session: "session-1", agent: "agent-1" } },
    );
    const stale = result.current;
    rerender({ session: "session-2", agent: "agent-2" });
    await expect(stale(request)).rejects.toThrow("active session");
    rerender({ session: "session-1", agent: "agent-2" });
    await expect(stale(request)).rejects.toThrow("active session");
    expect(chat.send).not.toHaveBeenCalled();
  });

  it("rejects a changed chat store or an unmounted shell", async () => {
    const { result, unmount } = renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    chat.conversationId = "session-2";
    await expect(result.current(request)).rejects.toThrow("active session");
    chat.conversationId = "session-1";
    unmount();
    await expect(result.current(request)).rejects.toThrow("active session");
    expect(chat.send).not.toHaveBeenCalled();
  });

  it("requires a bound agent and a nonempty instruction", async () => {
    const { result, rerender } = renderHook(
      ({ agent }) => useBrowserDesignPrompt("session-1", agent),
      {
        initialProps: { agent: null as string | null },
      },
    );
    await expect(result.current(request)).rejects.toThrow("No agent bound");
    rerender({ agent: "agent-1" });
    await expect(result.current({ ...request, prompt: " " })).rejects.toThrow(
      "Describe the change",
    );
    expect(chat.send).not.toHaveBeenCalled();
  });

  it("returns a send failure to the instruction row", async () => {
    chat.send.mockRejectedValue(new Error("runner unavailable"));
    const { result } = renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    await expect(result.current(request)).rejects.toThrow("runner unavailable");
  });
});

describe("older desktop compatibility", () => {
  it("routes the legacy prompt with its screenshot and signals the result", async () => {
    const bridge = legacyBridge();
    renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    bridge.emit("selected", { conversationId: "session-1", screenshot: request.screenshot });
    bridge.emit("submit", {
      conversationId: "session-1",
      id: 7,
      element: request.element,
      prompt: request.prompt,
    });
    await vi.waitFor(() =>
      expect(bridge.browserSignalDesignResult).toHaveBeenCalledWith("session-1", {
        id: 7,
        ok: true,
        message: "Sent to agent.",
      }),
    );
    expect(chat.send.mock.calls[0]?.[2]?.[0]).toBeInstanceOf(File);
  });

  it.each(["dismiss", "navigate"])("clears the legacy screenshot on %s", async (event) => {
    const bridge = legacyBridge();
    renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    bridge.emit("selected", { conversationId: "session-1", screenshot: request.screenshot });
    bridge.emit(event, { conversationId: "session-1" });
    bridge.emit("submit", {
      conversationId: "session-1",
      element: request.element,
      prompt: request.prompt,
    });
    await vi.waitFor(() => expect(chat.send).toHaveBeenCalled());
    expect(chat.send.mock.calls[0]?.[2]).toBeUndefined();
  });

  it("ignores shell-owned screenshots in the legacy cache and unsubscribes on unmount", async () => {
    const bridge = legacyBridge();
    const { unmount } = renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    bridge.emit("selected", { ...request });
    bridge.emit("submit", {
      conversationId: "session-1",
      element: request.element,
      prompt: request.prompt,
    });
    await vi.waitFor(() => expect(chat.send).toHaveBeenCalled());
    expect(chat.send.mock.calls[0]?.[2]).toBeUndefined();
    unmount();
    expect(bridge.unsubscribe).toHaveBeenCalledTimes(4);
  });

  it("rejects a legacy submit from another session", async () => {
    const bridge = legacyBridge();
    renderHook(() => useBrowserDesignPrompt("session-1", "agent-1"));
    bridge.emit("submit", {
      conversationId: "session-2",
      id: 9,
      element: request.element,
      prompt: request.prompt,
    });
    await vi.waitFor(() =>
      expect(bridge.browserSignalDesignResult).toHaveBeenCalledWith("session-2", {
        id: 9,
        ok: false,
        message: expect.stringContaining("active session"),
      }),
    );
    expect(chat.send).not.toHaveBeenCalled();
  });
});
