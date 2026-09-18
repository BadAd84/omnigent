import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesignModeSelection, DesignModeSubmit } from "@/lib/browserDesignMode";
import { BrowserPane } from "./BrowserPane";

// supportsBrowser gates the whole pane. Force it true so the pane renders; the
// unsupported-shell (returns null) path is covered by reading the early return.
vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => true,
  supportsBrowser: () => true,
}));

/**
 * Minimal `window.omnigentDesktop` stub. The empty-state tests only need the
 * subscription methods to exist (they return no-op unsubscribes) and
 * `browserHasView` to resolve "no view", so `viewActive` stays false and the
 * pane renders its cold-start (no-page-open) state — exactly the state the
 * regression made unreachable.
 */
function installBridge(overrides: Record<string, unknown> = {}) {
  const noopUnsub = () => {};
  const bridge = {
    browserHasView: vi.fn().mockResolvedValue({ exists: false }),
    onBrowserViewCreated: vi.fn().mockReturnValue(noopUnsub),
    onBrowserHostActiveChanged: vi.fn().mockReturnValue(noopUnsub),
    onBrowserViewClosed: vi.fn().mockReturnValue(noopUnsub),
    onBrowserUrlChanged: vi.fn().mockReturnValue(noopUnsub),
    onBrowserNavState: vi.fn().mockReturnValue(noopUnsub),
    browserSetActive: vi.fn().mockResolvedValue({ ok: true }),
    browserResize: vi.fn().mockResolvedValue({ ok: true }),
    browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true, created: true }),
    browserGoBack: vi.fn().mockResolvedValue({ ok: true }),
    browserGoForward: vi.fn().mockResolvedValue({ ok: true }),
    browserReload: vi.fn().mockResolvedValue({ ok: true }),
    openBrowserDevTools: vi.fn().mockResolvedValue({ ok: true }),
    browserEnableDesignMode: vi.fn().mockResolvedValue({ ok: true }),
    browserDisableDesignMode: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides,
  };
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = bridge;
  return bridge;
}

beforeEach(() => {
  // jsdom has no ResizeObserver; the measuring-container effect (viewActive path)
  // constructs one. Stub it so mounting the container doesn't throw.
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  installBridge();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;
});

describe("BrowserPane cold-start (no view yet)", () => {
  it("does not claim that the agent navigates user-created tabs", () => {
    render(<BrowserPane conversationId="browser-tab:conv_a:two" agentBrowser={false} />);
    expect(screen.getByText("Enter a URL above to get started.")).toBeInTheDocument();
    expect(screen.queryByText(/the agent will open pages here too/)).toBeNull();
  });

  it("restores a tab's URL and navigation state from its existing native view", async () => {
    const bridge = installBridge({
      browserHasView: vi.fn().mockResolvedValue({
        exists: true,
        url: "https://example.com/tab-two",
        canGoBack: true,
        canGoForward: false,
      }),
    });
    render(<BrowserPane conversationId="browser-tab:conv_a:two" />);
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Address bar" })).toHaveValue(
        "https://example.com/tab-two",
      ),
    );
    expect(screen.getByRole("button", { name: "Go back" })).toBeEnabled();
    await waitFor(() =>
      expect(bridge.browserSetActive).toHaveBeenCalledWith("browser-tab:conv_a:two"),
    );
    cleanup();
    expect(bridge.browserSetActive).toHaveBeenLastCalledWith(null);
  });

  it("reports native view-cap failures instead of silently leaving a blank tab", async () => {
    installBridge({
      browserOpenOrNavigate: vi
        .fn()
        .mockResolvedValue({ ok: false, error: "browser view cap reached — close one" }),
    });
    render(<BrowserPane conversationId="browser-tab:conv_a:two" />);
    const address = screen.getByRole("textbox", { name: "Address bar" });
    fireEvent.change(address, { target: { value: "example.com" } });
    fireEvent.keyDown(address, { key: "Enter" });
    expect(await screen.findByRole("alert")).toHaveTextContent("browser view cap reached");
    expect(address).toHaveValue("example.com");
  });

  it("renders the URL bar in the empty state so the first page is reachable", async () => {
    render(<BrowserPane conversationId="conv_a" />);

    // The address bar must be present with no view attached — this is the whole
    // point of the fix: gating it on viewActive made it unreachable from a cold
    // start (no page → no bar → no way to open the first page).
    const urlBar = await screen.findByRole("textbox", { name: /address bar/i });
    expect(urlBar).toBeInTheDocument();
    expect(urlBar).not.toBeDisabled();

    // The cold-start hint is shown instead of the measuring container.
    expect(screen.getByText(/enter a url above to get started/i)).toBeInTheDocument();
  });

  it("disables reload and devtools while no view is attached", async () => {
    render(<BrowserPane conversationId="conv_b" />);

    // Nothing to reload / no devtools target with no view — both disabled.
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByRole("button", { name: /reload/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /toggle devtools/i })).toBeDisabled();
  });

  it("disables back and forward while no view is attached", async () => {
    render(<BrowserPane conversationId="conv_c" />);

    // canGoBack/canGoForward start false with no view, so the arrows are off.
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByRole("button", { name: /go back/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /go forward/i })).toBeDisabled();
  });

  it("shows the measuring container (not the hint) once a view is created", async () => {
    // Capture the browser-view-created callback so the test can fire it and
    // drive viewActive → true, proving the toolbar stays and the hint is
    // replaced by the measuring region.
    let fireCreated: ((p: { conversationId: string }) => void) | undefined;
    installBridge({
      onBrowserViewCreated: vi.fn((cb: (p: { conversationId: string }) => void) => {
        fireCreated = cb;
        return () => {};
      }),
    });

    render(<BrowserPane conversationId="conv_d" />);
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByText(/enter a url above to get started/i)).toBeInTheDocument();

    fireCreated?.({ conversationId: "conv_d" });

    // The hint disappears (measuring container takes over) but the URL bar — the
    // always-present toolbar — is still there.
    await waitFor(() => {
      expect(screen.queryByText(/enter a url above to get started/i)).toBeNull();
    });
    expect(screen.getByRole("textbox", { name: /address bar/i })).toBeInTheDocument();
  });
});

describe("BrowserPane design-mode toggle", () => {
  it("renders the design-mode toggle in the toolbar", async () => {
    render(<BrowserPane conversationId="conv_dm1" />);
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByRole("button", { name: /enter design mode/i })).toBeInTheDocument();
  });

  it("disables the design-mode toggle while no view is attached", async () => {
    render(<BrowserPane conversationId="conv_dm2" />);
    await screen.findByRole("textbox", { name: /address bar/i });
    // No injected picker target without a view — the button is disabled, same
    // as reload / devtools.
    expect(screen.getByRole("button", { name: /enter design mode/i })).toBeDisabled();
  });

  it("calls enable then disable IPC as it toggles, and disables on unmount", async () => {
    let fireCreated: ((p: { conversationId: string }) => void) | undefined;
    const bridge = installBridge({
      onBrowserViewCreated: vi.fn((cb: (p: { conversationId: string }) => void) => {
        fireCreated = cb;
        return () => {};
      }),
    });

    const { unmount } = render(<BrowserPane conversationId="conv_dm3" />);
    await screen.findByRole("textbox", { name: /address bar/i });

    // Activate a view so the toggle is enabled.
    fireCreated?.({ conversationId: "conv_dm3" });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /enter design mode/i })).not.toBeDisabled();
    });

    // First click enables design mode (button flips to aria-pressed + "exit").
    screen.getByRole("button", { name: /enter design mode/i }).click();
    await waitFor(() => {
      expect(bridge.browserEnableDesignMode).toHaveBeenCalledWith("conv_dm3");
    });
    const pressed = screen.getByRole("button", { name: /exit design mode/i });
    expect(pressed).toHaveAttribute("aria-pressed", "true");

    // Second click disables it again.
    pressed.click();
    await waitFor(() => {
      expect(bridge.browserDisableDesignMode).toHaveBeenCalledWith("conv_dm3");
    });
    expect(screen.getByRole("button", { name: /enter design mode/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    screen.getByRole("button", { name: /enter design mode/i }).click();
    await waitFor(() => {
      expect(bridge.browserEnableDesignMode).toHaveBeenCalledTimes(2);
    });
    unmount();
    expect(bridge.browserDisableDesignMode).toHaveBeenCalledTimes(2);
  });
});

describe("BrowserPane toolbar navigation + URL bar", () => {
  /** Render the pane, activate a view (so toolbar buttons enable), and return
   *  the bridge + handles to the captured event callbacks. */
  async function renderActive(conversationId: string, overrides: Record<string, unknown> = {}) {
    let fireCreated: ((p: { conversationId: string }) => void) | undefined;
    let fireUrl: ((p: { conversationId: string; url: string }) => void) | undefined;
    let fireNav:
      | ((p: { conversationId: string; canGoBack: boolean; canGoForward: boolean }) => void)
      | undefined;
    const bridge = installBridge({
      onBrowserViewCreated: vi.fn((cb: (p: { conversationId: string }) => void) => {
        fireCreated = cb;
        return () => {};
      }),
      onBrowserUrlChanged: vi.fn((cb: (p: { conversationId: string; url: string }) => void) => {
        fireUrl = cb;
        return () => {};
      }),
      onBrowserNavState: vi.fn(
        (
          cb: (p: { conversationId: string; canGoBack: boolean; canGoForward: boolean }) => void,
        ) => {
          fireNav = cb;
          return () => {};
        },
      ),
      ...overrides,
    });
    render(<BrowserPane conversationId={conversationId} />);
    await screen.findByRole("textbox", { name: /address bar/i });
    fireCreated?.({ conversationId });
    await waitFor(() => expect(screen.getByRole("button", { name: /reload/i })).not.toBeDisabled());
    return { bridge, fireUrl: () => fireUrl, fireNav: () => fireNav };
  }

  it("reload button calls the reload IPC once a view is active", async () => {
    const { bridge } = await renderActive("conv_reload");
    screen.getByRole("button", { name: /reload/i }).click();
    await waitFor(() => expect(bridge.browserReload).toHaveBeenCalledWith("conv_reload"));
  });

  it("devtools button calls the open-devtools IPC", async () => {
    const { bridge } = await renderActive("conv_dt");
    screen.getByRole("button", { name: /toggle devtools/i }).click();
    await waitFor(() => expect(bridge.openBrowserDevTools).toHaveBeenCalledWith("conv_dt"));
  });

  it("back/forward buttons enable when nav-state reports history available", async () => {
    const { fireNav } = await renderActive("conv_hist");

    // Both arrows start disabled (canGoBack/Forward false).
    expect(screen.getByRole("button", { name: /go back/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /go forward/i })).toBeDisabled();

    // A nav-state event enabling history flips both buttons on — the
    // browser-nav-state SSE → setCanGoBack/Forward → disabled-prop chain.
    act(() => {
      fireNav()?.({ conversationId: "conv_hist", canGoBack: true, canGoForward: true });
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /go back/i })).not.toBeDisabled(),
    );
    expect(screen.getByRole("button", { name: /go forward/i })).not.toBeDisabled();
  });

  it("the URL bar reflects the real url pushed by browser-url-changed", async () => {
    const { fireUrl } = await renderActive("conv_url");
    act(() => {
      fireUrl()?.({ conversationId: "conv_url", url: "https://myhost/landed" });
    });
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: /address bar/i })).toHaveValue(
        "https://myhost/landed",
      ),
    );
  });

  it("submitting a dotless address normalizes it to http:// and navigates", async () => {
    const { bridge } = await renderActive("conv_nav");
    const bar = screen.getByRole("textbox", { name: /address bar/i }) as HTMLInputElement;

    bar.focus();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
    setter?.call(bar, "myhost");
    bar.dispatchEvent(new Event("input", { bubbles: true }));
    bar.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    await waitFor(() =>
      expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
        "conv_nav",
        "http://myhost",
        undefined,
        { force: true },
      ),
    );
  });
});

describe("BrowserPane shell-owned design instruction", () => {
  interface DismissPayload {
    conversationId: string;
    selectionId?: string;
    reason?: string;
  }
  const selection: DesignModeSelection = {
    conversationId: "conv_design",
    selectionId: "selection-period",
    element: { tag: "input", id: "#scenario-period", label: "Period" },
    screenshot: "data:image/png;base64,c2VsZWN0aW9u",
  };

  function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => {
      resolve = done;
    });
    return { promise, resolve };
  }

  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 120,
      top: 120,
      left: 0,
      right: 800,
      bottom: 600,
      width: 800,
      height: 480,
      toJSON: () => ({}),
    });
  });

  afterEach(() => vi.restoreAllMocks());

  async function renderDesignPane(
    overrides: Record<string, unknown> = {},
    submit = vi.fn<(request: DesignModeSubmit) => Promise<void>>().mockResolvedValue(undefined),
  ) {
    const selectedListeners = new Set<(payload: DesignModeSelection) => void>();
    const dismissListeners = new Set<(payload: DismissPayload) => void>();
    const urlListeners = new Set<(payload: { conversationId: string; url: string }) => void>();
    const closedListeners = new Set<
      (payload: { conversationId: string; reason: string | null }) => void
    >();
    const native = {
      browserHasView: vi.fn().mockResolvedValue({ exists: true, url: "https://capacity.test" }),
      browserEnableDesignMode: vi.fn().mockResolvedValue({ ok: true, promptHost: "shell" }),
      browserFocusDesignPrompt: vi.fn().mockResolvedValue({ ok: true }),
      browserClearDesignSelection: vi.fn().mockResolvedValue({ ok: true }),
      browserTakeDesignSelection: vi.fn().mockResolvedValue({
        ok: true,
        element: selection.element,
        screenshot: selection.screenshot,
      }),
      onBrowserElementSelected: vi.fn((callback: (payload: DesignModeSelection) => void) => {
        selectedListeners.add(callback);
        return () => selectedListeners.delete(callback);
      }),
      onBrowserElementPromptDismiss: vi.fn((callback: (payload: DismissPayload) => void) => {
        dismissListeners.add(callback);
        return () => dismissListeners.delete(callback);
      }),
      onBrowserUrlChanged: vi.fn(
        (callback: (payload: { conversationId: string; url: string }) => void) => {
          urlListeners.add(callback);
          return () => urlListeners.delete(callback);
        },
      ),
      onBrowserViewClosed: vi.fn(
        (callback: (payload: { conversationId: string; reason: string | null }) => void) => {
          closedListeners.add(callback);
          return () => closedListeners.delete(callback);
        },
      ),
      ...overrides,
    };
    const bridge = Object.assign(installBridge(native), native);
    const rendered = render(
      <BrowserPane conversationId="conv_design" onDesignPromptSubmit={submit} />,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Enter design mode" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Enter design mode" }));
    await act(async () => {});
    const emitSelection = (payload = selection) => {
      act(() => {
        for (const listener of selectedListeners) listener(payload);
      });
    };
    const selectReady = async (payload = selection) => {
      emitSelection(payload);
      const input = screen.getByRole("textbox", { name: "Describe the change" });
      await waitFor(() => expect(input).toBeEnabled());
      return input;
    };
    return {
      ...rendered,
      bridge,
      submit,
      emitSelection,
      selectReady,
      emitDismiss: (payload: DismissPayload) => {
        act(() => {
          for (const listener of dismissListeners) listener(payload);
        });
      },
      emitUrl: (url = "https://capacity.test/another") => {
        act(() => {
          for (const listener of urlListeners) listener({ conversationId: "conv_design", url });
        });
      },
      emitClosed: () => {
        act(() => {
          for (const listener of closedListeners)
            listener({ conversationId: "conv_design", reason: "closed" });
        });
      },
    };
  }

  it("negotiates shell mode and renders an editable instruction above the native viewport", async () => {
    const { bridge, container, selectReady } = await renderDesignPane();
    expect(bridge.browserEnableDesignMode).toHaveBeenCalledWith("conv_design", {
      promptHost: "shell",
    });
    const input = await selectReady();
    expect(screen.getByText("Selected: Period")).toBeInTheDocument();
    expect(input).toHaveFocus();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "asdf" } });
    expect(input).toHaveValue("asdf");
    const row = screen.getByRole("region", { name: "Design instruction" });
    const viewport = container.querySelector("[data-browser-viewport]");
    expect(viewport).not.toContainElement(input);
    expect(row.compareDocumentPosition(viewport!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(bridge.browserFocusDesignPrompt).toHaveBeenCalledWith("conv_design", "selection-period");
  });

  it("waits for native resize and native shell focus before focusing the instruction", async () => {
    const resized = deferred<{ ok: boolean }>();
    const focused = deferred<{ ok: boolean }>();
    const { bridge, emitSelection } = await renderDesignPane();
    bridge.browserResize.mockReturnValue(resized.promise);
    bridge.browserFocusDesignPrompt.mockReturnValue(focused.promise);
    emitSelection();
    const input = screen.getByRole("textbox", { name: "Describe the change" });
    expect(input).toBeDisabled();
    expect(input).not.toHaveFocus();
    expect(bridge.browserFocusDesignPrompt).not.toHaveBeenCalled();
    await act(async () => resized.resolve({ ok: true }));
    expect(bridge.browserFocusDesignPrompt).toHaveBeenCalledOnce();
    expect(input).not.toHaveFocus();
    await act(async () => focused.resolve({ ok: true }));
    expect(input).toBeEnabled();
    expect(input).toHaveFocus();
  });

  it("submits the claimed element and screenshot together, then reports success in the shell", async () => {
    const { bridge, submit, selectReady } = await renderDesignPane();
    const authoritativeElement = { tag: "input", id: "#period", label: "Period" };
    bridge.browserTakeDesignSelection.mockResolvedValue({
      ok: true,
      element: authoritativeElement,
      screenshot: "data:image/png;base64,c2FmZQ==",
    });
    const input = await selectReady();
    fireEvent.change(input, { target: { value: "  Make this field wider  " } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(submit).toHaveBeenCalledWith({
        conversationId: "conv_design",
        selectionId: "selection-period",
        element: authoritativeElement,
        screenshot: "data:image/png;base64,c2FmZQ==",
        prompt: "Make this field wider",
      }),
    );
    expect(bridge.browserTakeDesignSelection).toHaveBeenCalledWith(
      "conv_design",
      "selection-period",
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Instruction sent to chat.");
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
  });

  it.each(["resize", "focus"])(
    "leaves instruction entry disabled when native %s is rejected",
    async (operation) => {
      const { bridge, emitSelection } = await renderDesignPane();
      if (operation === "resize") bridge.browserResize.mockResolvedValue({ ok: false });
      else
        bridge.browserFocusDesignPrompt.mockResolvedValue({
          ok: false,
          error: "Selection expired.",
        });
      emitSelection();
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Select the element again to retry.",
      );
      const input = screen.getByRole("textbox", { name: "Describe the change" });
      expect(input).toBeDisabled();
      expect(input).not.toHaveFocus();
      if (operation === "resize") expect(bridge.browserFocusDesignPrompt).not.toHaveBeenCalled();
    },
  );

  it("ignores composing Enter and prevents duplicate submissions while a request is pending", async () => {
    const sent = deferred<void>();
    const submit = vi
      .fn<(request: DesignModeSubmit) => Promise<void>>()
      .mockReturnValue(sent.promise);
    const { bridge, selectReady } = await renderDesignPane({}, submit);
    const input = await selectReady();
    fireEvent.change(input, { target: { value: "Use a longer period" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(bridge.browserTakeDesignSelection).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    expect(bridge.browserTakeDesignSelection).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Sending…" })).toBeDisabled();
    await act(async () => sent.resolve());
    expect(screen.getByRole("status")).toHaveTextContent("Instruction sent to chat.");
  });

  it.each(["Cancel", "Escape"])(
    "clears the scoped selection using %s without sending",
    async (action) => {
      const { bridge, submit, selectReady, emitSelection } = await renderDesignPane();
      const input = await selectReady();
      fireEvent.change(input, { target: { value: "Do not send" } });
      if (action === "Cancel") fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      else fireEvent.keyDown(input, { key: "Escape" });
      expect(bridge.browserClearDesignSelection).toHaveBeenCalledWith(
        "conv_design",
        "selection-period",
      );
      expect(submit).not.toHaveBeenCalled();
      emitSelection();
      expect(screen.queryByRole("textbox", { name: "Describe the change" })).toBeNull();
    },
  );

  it("does not refocus a cancelled selection when its delayed resize completes", async () => {
    const resized = deferred<{ ok: boolean }>();
    const { bridge, emitSelection } = await renderDesignPane();
    bridge.browserResize.mockReturnValue(resized.promise);
    emitSelection();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await act(async () => resized.resolve({ ok: true }));
    expect(bridge.browserFocusDesignPrompt).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
  });

  it("ignores selections and dismissals for another view", async () => {
    const { emitSelection, emitDismiss, selectReady } = await renderDesignPane();
    emitSelection({ ...selection, conversationId: "other_view" });
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
    await selectReady();
    emitDismiss({
      conversationId: "other_view",
      selectionId: selection.selectionId,
      reason: "navigation",
    });
    expect(screen.getByRole("region", { name: "Design instruction" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exit design mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("does not let a delayed dismissal of the previous selection clear the current draft", async () => {
    const { selectReady, emitDismiss } = await renderDesignPane();
    await selectReady();
    const next = {
      ...selection,
      selectionId: "selection-name",
      element: { tag: "input", label: "Scenario name" },
    };
    const input = await selectReady(next);
    fireEvent.change(input, { target: { value: "Rename this label" } });
    emitDismiss({
      conversationId: "conv_design",
      selectionId: selection.selectionId,
      reason: "replaced",
    });
    expect(input).toHaveValue("Rename this label");
    expect(screen.getByText("Selected: Scenario name")).toBeInTheDocument();
  });

  it("clears a selection on navigation and disables mode when its document is replaced", async () => {
    const { bridge, selectReady, emitUrl, emitSelection, emitDismiss } = await renderDesignPane();
    await selectReady();
    emitUrl();
    expect(bridge.browserClearDesignSelection).toHaveBeenCalledWith(
      "conv_design",
      selection.selectionId,
    );
    emitSelection();
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
    emitDismiss({ conversationId: "conv_design", reason: "navigation" });
    expect(screen.getByRole("button", { name: "Enter design mode" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    emitSelection({ ...selection, selectionId: "late-navigation" });
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
  });

  it("does not send an already-claimed selection after the view navigates", async () => {
    const claimed = deferred<{ ok: boolean; element: DesignModeSelection["element"] }>();
    const { bridge, submit, selectReady, emitUrl } = await renderDesignPane();
    bridge.browserTakeDesignSelection.mockReturnValue(claimed.promise);
    const input = await selectReady();
    fireEvent.change(input, { target: { value: "Make this blue" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    emitUrl();
    await act(async () => claimed.resolve({ ok: true, element: selection.element }));
    expect(submit).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("clears the instruction when design mode is disabled or the native view closes", async () => {
    const { selectReady, emitClosed, bridge } = await renderDesignPane();
    await selectReady();
    fireEvent.click(screen.getByRole("button", { name: "Exit design mode" }));
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
    expect(bridge.browserDisableDesignMode).toHaveBeenCalledWith("conv_design");
    fireEvent.click(screen.getByRole("button", { name: "Enter design mode" }));
    await act(async () => {});
    await selectReady({ ...selection, selectionId: "after-toggle" });
    emitClosed();
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
    expect(screen.getByRole("button", { name: "Enter design mode" })).toBeDisabled();
  });

  it("unsubscribes and cannot focus or send after unmount", async () => {
    const resized = deferred<{ ok: boolean }>();
    const { bridge, emitSelection, unmount, submit } = await renderDesignPane();
    bridge.browserResize.mockReturnValue(resized.promise);
    emitSelection();
    unmount();
    await act(async () => resized.resolve({ ok: true }));
    emitSelection({ ...selection, selectionId: "late-unmount" });
    expect(bridge.browserFocusDesignPrompt).not.toHaveBeenCalled();
    expect(bridge.browserDisableDesignMode).toHaveBeenCalledWith("conv_design");
    expect(submit).not.toHaveBeenCalled();
  });

  it("drops the previous view's selection and pending focus when its view key changes", async () => {
    const resized = deferred<{ ok: boolean }>();
    const { bridge, emitSelection, rerender, submit } = await renderDesignPane();
    bridge.browserResize.mockReturnValue(resized.promise);
    emitSelection();
    rerender(<BrowserPane conversationId="another_view" onDesignPromptSubmit={submit} />);
    await act(async () => resized.resolve({ ok: true }));
    emitSelection({ ...selection, selectionId: "late-previous-view" });
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
    expect(bridge.browserFocusDesignPrompt).not.toHaveBeenCalled();
    expect(bridge.browserDisableDesignMode).toHaveBeenCalledWith("conv_design");
    expect(screen.getByRole("button", { name: "Enter design mode" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("keeps a failed draft visible and does not report it as sent", async () => {
    const submit = vi
      .fn<(request: DesignModeSubmit) => Promise<void>>()
      .mockRejectedValue(new Error("Runner unavailable."));
    const { selectReady } = await renderDesignPane({}, submit);
    const input = await selectReady();
    fireEvent.change(input, { target: { value: "Keep this instruction" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Runner unavailable. Select the element again to retry.",
    );
    expect(input).toHaveValue("Keep this instruction");
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("refuses to send when main rejects the selection ticket", async () => {
    const { bridge, submit, selectReady } = await renderDesignPane();
    bridge.browserTakeDesignSelection.mockResolvedValue({ ok: false, error: "Selection expired." });
    const input = await selectReady();
    fireEvent.change(input, { target: { value: "Make this blue" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Selection expired.");
    expect(submit).not.toHaveBeenCalled();
  });

  it("leaves legacy desktop prompts available when shell capabilities are absent", async () => {
    const submit = vi.fn();
    const bridge = installBridge({ browserHasView: vi.fn().mockResolvedValue({ exists: true }) });
    render(<BrowserPane conversationId="legacy" onDesignPromptSubmit={submit} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Enter design mode" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Enter design mode" }));
    expect(bridge.browserEnableDesignMode).toHaveBeenCalledWith("legacy");
    expect(screen.queryByRole("region", { name: "Design instruction" })).toBeNull();
  });
});
