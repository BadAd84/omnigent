const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { registerBrowserIpc, DESIGN_MODE_GESTURE_WINDOW_MS } = require("../src/browserIpc");
const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");

const VIEW_ID = "browser-tab:conversation-one:form";
const ELEMENT = {
  tag: "input",
  id: "#period",
  label: "Period",
  rect: { x: 10, y: 20, width: 120, height: 30 },
};
const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });
const png = (value) => ({ toPNG: () => Buffer.from(value) });

function setup(t) {
  const handlers = new Map();
  const sent = [];
  const sender = {
    focusCount: 0,
    focus() {
      this.focusCount++;
    },
    send: (channel, payload) => sent.push({ channel, payload }),
  };
  const event = { sender, pinned: true };
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => {
      const wc = new EventEmitter();
      Object.assign(wc, {
        mainFrame: {},
        destroyed: false,
        scripts: [],
        captures: [],
        zoom: 1,
        url: "",
        navigationHistory: {
          canGoBack: () => true,
          canGoForward: () => true,
          goBack() {},
          goForward() {},
        },
        isDestroyed: () => wc.destroyed,
        getZoomFactor: () => wc.zoom,
        getURL: () => wc.url,
        setWindowOpenHandler() {},
        loadURL(url) {
          wc.url = url;
          return Promise.resolve();
        },
        executeJavaScript(script) {
          wc.scripts.push(script);
          return Promise.resolve();
        },
        capturePage(rect) {
          wc.captures.push(rect);
          return Promise.resolve(png("target-image"));
        },
        reload() {},
        close() {
          wc.destroyed = true;
          wc.emit("destroyed");
        },
      });
      let bounds = { x: 0, y: 80, width: 640, height: 480 };
      return {
        webContents: wc,
        getBounds: () => bounds,
        setBounds: (next) => (bounds = next),
        setVisible() {},
      };
    },
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer: (channel, payload) => sent.push({ channel, payload }),
  });
  registry.openOrNavigate(VIEW_ID, "https://example.com/form", {
    x: 0,
    y: 80,
    width: 640,
    height: 480,
  });
  registry.setActive(VIEW_ID);
  t.after(() => registry.closeAll());
  registerBrowserIpc({
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler) },
    isPinnedOriginSender: (candidate) => candidate.pinned,
    getRegistryForEvent: () => registry,
  });
  const entry = registry.get(VIEW_ID);
  const wc = entry.view.webContents;
  return {
    registry,
    entry,
    wc,
    sender,
    event,
    sent,
    invoke: (name, args = {}, from = event) =>
      handlers.get(`omnigent:browser-${name}`)(from, { conversationId: VIEW_ID, ...args }),
    messages: (channel) =>
      sent.filter((item) => item.channel === channel).map((item) => item.payload),
  };
}

function nonce(ctx) {
  const script = ctx.wc.scripts.findLast((value) =>
    /__omni_[a-f0-9]{32}_element_select__/.test(value),
  );
  return /__omni_([a-f0-9]{32})_element_select__/.exec(script)[1];
}

function marker(ctx, kind, payload, options = {}) {
  const message = `__omni_${options.nonce ?? nonce(ctx)}_element_${kind}__${
    payload === undefined ? "" : JSON.stringify(payload)
  }`;
  ctx.wc.emit("console-message", { frame: options.frame ?? ctx.wc.mainFrame, message });
}

function gesture(ctx, input = { type: "mouseDown" }) {
  ctx.wc.emit("input-event", {}, input);
}

async function enable(ctx) {
  assert.deepEqual(await ctx.invoke("enable-design-mode", { opts: { promptHost: "shell" } }), {
    ok: true,
    promptHost: "shell",
  });
}

async function select(ctx, element = ELEMENT) {
  gesture(ctx);
  marker(ctx, "select", element);
  await tick();
  return ctx.messages("browser-element-selected").at(-1);
}

describe("shell design selection — trust and ownership", () => {
  it("negotiates shell mode while preserving the legacy flattened event", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const shell = await select(ctx);
    assert.equal(shell.conversationId, VIEW_ID);
    assert.equal(shell.element.label, "Period");
    assert.equal(typeof shell.selectionId, "string");
    assert.equal(shell.tag, undefined);

    assert.deepEqual(await ctx.invoke("enable-design-mode"), { ok: true, promptHost: "page" });
    marker(ctx, "select", { ...ELEMENT, conversationId: "page-forged-owner" });
    await tick();
    const legacy = ctx.messages("browser-element-selected").at(-1);
    assert.equal(legacy.conversationId, VIEW_ID);
    assert.equal(legacy.tag, "input");
    assert.equal(legacy.selectionId, undefined);
    assert.equal(legacy.element, undefined);
    assert.match(ctx.wc.scripts.at(-1), /__omni-popup-input/);
  });

  it("rejects unsupported hosts and inactive or suppressed shell pickers", async (t) => {
    const ctx = setup(t);
    assert.equal(
      (await ctx.invoke("enable-design-mode", { opts: { promptHost: "unknown" } })).ok,
      false,
    );
    ctx.registry.setActive(null);
    assert.equal(
      (await ctx.invoke("enable-design-mode", { opts: { promptHost: "shell" } })).ok,
      false,
    );
    ctx.registry.setActive(VIEW_ID);
    ctx.registry.setSuppressed(true);
    assert.equal(
      (await ctx.invoke("enable-design-mode", { opts: { promptHost: "shell" } })).ok,
      false,
    );
    assert.equal(ctx.wc.scripts.length, 0);
  });

  it("requires a primary native mouse gesture, matching nonce, and owning main frame", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    marker(ctx, "select", ELEMENT);
    for (const input of [
      { type: "keyDown" },
      { type: "rawKeyDown" },
      { type: "mouseMove" },
      { type: "mouseDown", modifiers: ["rightbuttondown"] },
      { type: "mouseDown", modifiers: ["middlebuttondown"] },
    ]) {
      gesture(ctx, input);
      marker(ctx, "select", ELEMENT);
    }
    gesture(ctx);
    ctx.entry.designModeActivation.gestureState.lastMouseDownAt =
      Date.now() - DESIGN_MODE_GESTURE_WINDOW_MS - 1;
    marker(ctx, "select", ELEMENT);
    gesture(ctx);
    ctx.entry.designModeActivation.gestureState.lastMouseDownAt = Date.now() + 10_000;
    marker(ctx, "select", ELEMENT);
    gesture(ctx);
    marker(ctx, "select", ELEMENT, { nonce: "wrong-nonce" });
    marker(ctx, "select", ELEMENT, { frame: {} });
    await tick();
    assert.equal(ctx.messages("browser-element-selected").length, 0);
    assert.equal(ctx.wc.captures.length, 0);

    marker(ctx, "select", ELEMENT);
    marker(ctx, "select", { ...ELEMENT, label: "Replay" });
    await tick();
    assert.equal(ctx.messages("browser-element-selected").length, 1);
    assert.equal(ctx.messages("browser-element-selected")[0].element.label, "Period");
  });

  it("bounds metadata and ignores page-provided tickets, owners, prompts, and screenshots", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const selected = await select(ctx, {
      ...ELEMENT,
      label: `  Period\n${"x".repeat(300)}  `,
      role: ["textbox"],
      component: 99,
      conversationId: "attacker-conversation",
      selectionId: "attacker-ticket",
      screenshot: "attacker-image",
      prompt: "untrusted instruction",
      styles: { color: "red" },
    });
    assert.equal(selected.conversationId, VIEW_ID);
    assert.notEqual(selected.selectionId, "attacker-ticket");
    assert.match(selected.selectionId, /^[a-f0-9-]{36}$/);
    assert.deepEqual(selected.element, {
      tag: "input",
      id: "#period",
      label: `Period ${"x".repeat(193)}`,
    });
    assert.equal(
      selected.screenshot,
      `data:image/png;base64,${Buffer.from("target-image").toString("base64")}`,
    );
    assert.equal(selected.prompt, undefined);
  });

  it("drops malformed and oversized page selection messages", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    gesture(ctx);
    for (const info of [
      null,
      [],
      {},
      { tag: "   " },
      { tag: 1 },
      { tag: "input", text: "x".repeat(16_384) },
    ]) {
      marker(ctx, "select", info);
    }
    ctx.wc.emit("console-message", {
      frame: ctx.wc.mainFrame,
      message: `__omni_${nonce(ctx)}_element_select__{bad-json`,
    });
    await tick();
    assert.equal(ctx.messages("browser-element-selected").length, 0);
    assert.equal(ctx.wc.captures.length, 0);
  });

  it("allows only the pinned owning renderer to focus once or take the current ticket", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const selected = await select(ctx);
    const args = { selectionId: selected.selectionId };
    const stranger = { pinned: true, sender: { ...ctx.sender, focusCount: 0 } };
    for (const name of ["focus-design-prompt", "clear-design-selection", "take-design-selection"]) {
      assert.equal(ctx.invoke(name, args, { ...ctx.event, pinned: false }).ok, false);
      assert.equal(ctx.invoke(name, args, stranger).ok, false);
      assert.equal(ctx.invoke(name, { selectionId: "forged-ticket" }).ok, false);
      assert.equal(ctx.invoke(name, { ...args, conversationId: "another-view" }).ok, false);
    }
    assert.equal(ctx.sender.focusCount, 0);
    assert.deepEqual(await ctx.invoke("focus-design-prompt", args), { ok: true });
    assert.equal((await ctx.invoke("focus-design-prompt", args)).ok, false);
    assert.equal(ctx.sender.focusCount, 1);
    assert.equal(stranger.sender.focusCount, 0);

    const dismissCount = ctx.messages("browser-element-prompt-dismiss").length;
    assert.deepEqual(
      await ctx.invoke("take-design-selection", {
        ...args,
        element: { tag: "forged" },
        screenshot: "forged",
      }),
      {
        ok: true,
        element: selected.element,
        screenshot: selected.screenshot,
      },
    );
    assert.equal(ctx.messages("browser-element-prompt-dismiss").length, dismissCount);
    assert.equal((await ctx.invoke("take-design-selection", args)).ok, false);
    assert.equal((await ctx.invoke("focus-design-prompt", args)).ok, false);
  });

  it("expires automatic native focus without discarding the shell-owned selection", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const selected = await select(ctx);
    ctx.entry.designModeActivation.selection.clickedAt =
      Date.now() - DESIGN_MODE_GESTURE_WINDOW_MS - 1;
    assert.equal(
      (await ctx.invoke("focus-design-prompt", { selectionId: selected.selectionId })).ok,
      false,
    );
    assert.equal(ctx.sender.focusCount, 0);
    assert.equal(
      (await ctx.invoke("take-design-selection", { selectionId: selected.selectionId })).ok,
      true,
    );
  });

  it("rejects page-origin submit and dismiss markers and keeps feedback outside the page", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const selected = await select(ctx);
    const before = ctx.wc.scripts.length;
    gesture(ctx);
    marker(ctx, "prompt_submit", { prompt: "page instruction", element: ELEMENT });
    marker(ctx, "dismiss");
    assert.equal(ctx.messages("browser-element-prompt-submit").length, 0);
    assert.equal(ctx.messages("browser-element-prompt-dismiss").length, 0);
    assert.equal(ctx.entry.designModeActivation.selection.id, selected.selectionId);
    assert.equal(
      (await ctx.invoke("signal-design-result", { id: 1, ok: true, message: "Done" })).ok,
      false,
    );
    assert.equal(ctx.wc.scripts.length, before);
  });

  it("clears only the current ticket and keeps the picker enabled", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const first = await select(ctx);
    const second = await select(ctx, { ...ELEMENT, label: "Name" });
    assert.notEqual(first.selectionId, second.selectionId);
    assert.deepEqual(ctx.messages("browser-element-prompt-dismiss"), [
      {
        conversationId: VIEW_ID,
        selectionId: first.selectionId,
        reason: "replaced",
      },
    ]);
    assert.equal(
      (await ctx.invoke("clear-design-selection", { selectionId: first.selectionId })).ok,
      false,
    );
    assert.equal(ctx.entry.designModeActivation.selection.id, second.selectionId);
    assert.deepEqual(
      await ctx.invoke("clear-design-selection", { selectionId: second.selectionId }),
      { ok: true },
    );
    assert.equal(ctx.entry.designModeActivation.selection, null);
    assert.equal(ctx.messages("browser-element-prompt-dismiss").at(-1).reason, "cleared");
    assert.equal(ctx.wc.listenerCount("input-event"), 1);
    assert.equal(ctx.wc.listenerCount("console-message"), 1);
    assert.match(ctx.wc.scripts.at(-1), /__omniClearDesignSelection/);
    assert.ok((await select(ctx)).selectionId);
  });
});

describe("shell design selection — capture and lifecycle", () => {
  it("re-enables after a shell reload without dismissing the new activation", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const previous = ctx.entry.designModeActivation;
    const first = await select(ctx);

    // A shell reload detaches the view without tearing down its native picker.
    ctx.registry.setActive(null);
    assert.equal(ctx.entry.designModeActivation, previous);
    assert.equal(ctx.wc.isDestroyed(), false);
    ctx.registry.setActive(VIEW_ID);
    const dismissals = ctx.messages("browser-element-prompt-dismiss");

    await enable(ctx);
    assert.notEqual(ctx.entry.designModeActivation, previous);
    assert.deepEqual(ctx.messages("browser-element-prompt-dismiss"), dismissals);
    assert.equal(ctx.wc.listenerCount("console-message"), 1);
    assert.equal(ctx.wc.listenerCount("input-event"), 1);
    assert.equal(
      (await ctx.invoke("take-design-selection", { selectionId: first.selectionId })).ok,
      false,
    );

    const selected = await select(ctx);
    assert.notEqual(selected.selectionId, first.selectionId);
    assert.deepEqual(
      await ctx.invoke("focus-design-prompt", { selectionId: selected.selectionId }),
      { ok: true },
    );
    assert.deepEqual(await ctx.invoke("disable-design-mode"), { ok: true });
    assert.deepEqual(ctx.messages("browser-element-prompt-dismiss"), [
      ...dismissals,
      {
        conversationId: VIEW_ID,
        selectionId: selected.selectionId,
        reason: "disabled",
      },
    ]);
    assert.equal(ctx.entry.designModeActivation, null);
  });

  it("clamps zoomed screenshot crops to the view and keeps metadata paired with the capture", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    ctx.wc.zoom = 2;
    const selected = await select(ctx, {
      ...ELEMENT,
      rect: { x: -5, y: 220, width: 500, height: 60 },
    });
    assert.deepEqual(ctx.wc.captures, [{ x: 0, y: 440, width: 640, height: 40 }]);
    assert.ok(selected.screenshot);
    assert.equal(selected.element.rect, undefined);
  });

  it("keeps selection usable when a crop is invalid, capture fails, or the PNG is oversized", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    for (const rect of [
      undefined,
      { x: 0, y: 0, width: -1, height: 1 },
      { x: 900, y: 0, width: 1, height: 1 },
      { x: "0", y: 0, width: 1, height: 1 },
    ]) {
      // Each selection deliberately replaces the previous ticket.
      // eslint-disable-next-line no-await-in-loop
      assert.equal((await select(ctx, { ...ELEMENT, rect })).screenshot, null);
    }
    assert.equal(ctx.wc.captures.length, 0);
    ctx.wc.capturePage = async () => {
      throw new Error("capture failed");
    };
    assert.equal((await select(ctx)).screenshot, null);
    ctx.wc.capturePage = async () => ({ toPNG: () => Buffer.alloc(8 * 1024 * 1024 + 1) });
    assert.equal((await select(ctx)).screenshot, null);
  });

  it("does not authorize focus or take while capture is pending", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    let finish;
    ctx.wc.capturePage = () =>
      new Promise((resolve) => {
        finish = resolve;
      });
    gesture(ctx);
    marker(ctx, "select", ELEMENT);
    const selectionId = ctx.entry.designModeActivation.selection.id;
    assert.equal((await ctx.invoke("focus-design-prompt", { selectionId })).ok, false);
    assert.equal((await ctx.invoke("take-design-selection", { selectionId })).ok, false);
    finish(png("finished"));
    await tick();
    assert.equal((await ctx.invoke("focus-design-prompt", { selectionId })).ok, true);
  });

  it("never pairs an old asynchronous screenshot with a newer selected element", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const captures = [];
    ctx.wc.capturePage = () =>
      new Promise((resolve) => {
        captures.push(resolve);
      });
    gesture(ctx);
    marker(ctx, "select", { ...ELEMENT, label: "First" });
    gesture(ctx);
    marker(ctx, "select", { ...ELEMENT, label: "Second" });
    captures[1](png("second-image"));
    await tick();
    captures[0](png("first-image"));
    await tick();
    const selections = ctx.messages("browser-element-selected");
    assert.equal(selections.length, 1);
    assert.equal(selections[0].element.label, "Second");
    assert.equal(
      selections[0].screenshot,
      `data:image/png;base64,${Buffer.from("second-image").toString("base64")}`,
    );
  });

  const lifecycleCases = [
    ["suppression", "suppressed", (ctx) => ctx.registry.setSuppressed(true)],
    ["detachment", "inactive", (ctx) => ctx.registry.setActive(null)],
    ["missing-tab switch", "inactive", (ctx) => ctx.registry.setActive("missing-view")],
    [
      "tab switch",
      "inactive",
      (ctx) => {
        ctx.registry.openOrNavigate("another-view", "https://example.com/other");
        ctx.registry.setActive("another-view");
      },
    ],
    [
      "URL navigation",
      "navigation",
      (ctx) => ctx.registry.openOrNavigate(VIEW_ID, "https://example.com/next"),
    ],
    [
      "main-frame navigation",
      "navigation",
      (ctx) => ctx.wc.emit("did-start-navigation", { isMainFrame: true, isSameDocument: false }),
    ],
    [
      "legacy navigation event",
      "navigation",
      (ctx) => ctx.wc.emit("did-start-navigation", {}, "https://example.com/next", false, true),
    ],
    [
      "in-page navigation",
      "navigated-in-page",
      (ctx) => ctx.wc.emit("did-navigate-in-page", {}, "https://example.com/#next", true),
    ],
    ["reload", "navigation", (ctx) => ctx.invoke("reload")],
    ["back", "navigation", (ctx) => ctx.invoke("go-back")],
    ["forward", "navigation", (ctx) => ctx.invoke("go-forward")],
    ["disable", "disabled", (ctx) => ctx.invoke("disable-design-mode")],
    [
      "re-enable",
      null,
      (ctx) => ctx.invoke("enable-design-mode", { opts: { promptHost: "shell" } }),
    ],
    ["close", "closed", (ctx) => ctx.registry.close(VIEW_ID)],
    ["destroy", "closed", (ctx) => ctx.wc.close()],
  ];
  for (const [name, reason, transition] of lifecycleCases) {
    it(`invalidates selection on ${name}`, async (t) => {
      const ctx = setup(t);
      await enable(ctx);
      const selected = await select(ctx);
      await transition(ctx);
      const dismissals = ctx.messages("browser-element-prompt-dismiss");
      if (reason === null) {
        assert.deepEqual(dismissals, []);
      } else {
        assert.ok(
          dismissals.some(
            (event) => event.selectionId === selected.selectionId && event.reason === reason,
          ),
        );
      }
      assert.equal(
        (await ctx.invoke("focus-design-prompt", { selectionId: selected.selectionId })).ok,
        false,
      );
      assert.equal(
        (await ctx.invoke("take-design-selection", { selectionId: selected.selectionId })).ok,
        false,
      );
      assert.equal(ctx.sender.focusCount, 0);
    });

    it(`drops a capture that finishes after ${name}`, async (t) => {
      const ctx = setup(t);
      await enable(ctx);
      let finish;
      ctx.wc.capturePage = () =>
        new Promise((resolve) => {
          finish = resolve;
        });
      gesture(ctx);
      marker(ctx, "select", ELEMENT);
      const selectionId = ctx.entry.designModeActivation.selection.id;
      await transition(ctx);
      finish(png("stale-image"));
      await tick();
      assert.equal(ctx.messages("browser-element-selected").length, 0);
      assert.equal((await ctx.invoke("focus-design-prompt", { selectionId })).ok, false);
      assert.equal((await ctx.invoke("take-design-selection", { selectionId })).ok, false);
    });
  }

  it("ignores subframe navigations and same-view geometry reapplication", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    const selected = await select(ctx);
    ctx.wc.emit("did-start-navigation", { isMainFrame: false, isSameDocument: false });
    ctx.wc.emit("did-navigate-in-page", {}, "https://example.com/#frame", false);
    ctx.registry.setActive(VIEW_ID);
    ctx.registry.setSuppressed(false);
    ctx.registry.openOrNavigate(VIEW_ID, "https://example.com/form");
    assert.equal(ctx.messages("browser-element-prompt-dismiss").length, 0);
    assert.equal(ctx.entry.designModeActivation.selection.id, selected.selectionId);
  });

  it("removes activation listeners when disabled or closed", async (t) => {
    const ctx = setup(t);
    await enable(ctx);
    await ctx.invoke("disable-design-mode");
    for (const event of [
      "input-event",
      "console-message",
      "did-start-navigation",
      "did-navigate-in-page",
      "destroyed",
    ]) {
      assert.equal(ctx.wc.listenerCount(event), 0);
    }
    assert.equal(ctx.entry.designModeActivation, null);
    await enable(ctx);
    ctx.registry.close(VIEW_ID);
    for (const event of [
      "input-event",
      "console-message",
      "did-start-navigation",
      "did-navigate-in-page",
      "destroyed",
    ]) {
      assert.equal(ctx.wc.listenerCount(event), 0);
    }
    assert.equal(ctx.entry.designModeActivation, null);
  });
});
