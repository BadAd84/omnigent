// The real SPA hosts the design prompt outside a modal page's focus trap.
// Run after building web; OMNIGENT_PYTHON selects the backend interpreter.
// OMNIGENT_DESKTOP_FORM_URL may point to an equivalent Radix-dialog fixture.

"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, saveRecording } = require("./desktopHarness");
const {
  MOCK_REPLY,
  eventually,
  startDesignBackend,
  startFormFixture,
  launchDesignDesktop,
  startMacCapture,
} = require("./desktopDesignPromptHarness");

const deps = desktopDepsAvailable();
const INSTRUCTION = "Use a week picker for Period.";

async function assertShellFocus(electronApp, shellId, input) {
  let observed;
  try {
    await eventually(async () => {
      const native = await electronApp.evaluate(({ webContents, BrowserWindow }) => ({
        focusedId: webContents.getFocusedWebContents()?.id,
        focusedWindow: BrowserWindow.getFocusedWindow()?.id,
      }));
      const renderer = await input.evaluate((element) => ({
        activeInput: document.activeElement === element,
        documentFocused: document.hasFocus(),
        disabled: element.disabled,
        activeElement: document.activeElement?.outerHTML.slice(0, 250),
      }));
      observed = { expectedShellId: shellId, ...native, ...renderer };
      return native.focusedId === shellId && renderer.activeInput;
    }, "native shell focus and focused instruction input");
  } catch (error) {
    throw new Error(`${error.message}: ${JSON.stringify(observed)}`, { cause: error });
  }
}

async function assertPromptAboveView(electronApp, windowId, region, formUrl) {
  const row = await region.boundingBox();
  assert.ok(row);
  await eventually(async () => {
    const bounds = await electronApp.evaluate(
      ({ BrowserWindow }, { id, url }) => {
        const child = BrowserWindow.fromId(id).contentView.children.find(
          (view) => view.webContents?.getURL() === url,
        );
        return child?.getBounds();
      },
      { id: windowId, url: formUrl },
    );
    return bounds && bounds.y >= row.y + row.height - 2;
  }, "native browser bounds below the shell prompt");
}

it(
  "desktop design instructions stay outside modal forms and use normal chat submission",
  {
    skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`,
    timeout: 240_000,
  },
  async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-design-prompt-"));
    const recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(tmpDir, "recordings");
    fs.mkdirSync(recordDir, { recursive: true });
    let backend;
    let fixture;
    let electronApp;
    let stopCapture = async () => {};
    let formPage;
    const messages = [];
    try {
      backend = await startDesignBackend(tmpDir);
      fixture = await startFormFixture();
      const desktop = await launchDesignDesktop(tmpDir, recordDir, backend.serverUrl);
      electronApp = desktop.electronApp;
      const window = desktop.window;
      const nativeWindow = await electronApp.browserWindow(window);
      const { windowId, shellId } = await nativeWindow.evaluate((win) => ({
        windowId: win.id,
        shellId: win.webContents.id,
      }));
      await nativeWindow.evaluate((win) => {
        win.setSize(1400, 900);
        win.show();
        win.focus();
      });
      // macOS may launch a test app in the background despite showing its window.
      await electronApp.evaluate(({ app }) => {
        if (process.platform === "darwin") app.focus({ steal: true });
      });
      await eventually(
        () => nativeWindow.evaluate((win) => win.isFocused()),
        "foreground test window",
      );
      await window.goto(`${backend.serverUrl}/c/${backend.sessionId}`);
      window.on("request", (request) => {
        if (
          request.method() === "POST" &&
          request.url().endsWith(`/v1/sessions/${backend.sessionId}/events`)
        ) {
          const body = request.postDataJSON();
          if (body?.type === "message") messages.push(body);
        }
      });

      await window.getByRole("button", { name: /^(Expand|Collapse) right panel$/ }).waitFor();
      const expand = window.getByRole("button", { name: "Expand right panel", exact: true });
      if (await expand.isVisible()) await expand.click();
      await window.getByRole("tab", { name: "Browser", exact: true }).click();
      if (process.env.OMNIGENT_DESKTOP_COMPOSITED_VIDEO === "1") {
        const closeSidebar = window.getByRole("button", { name: "Close sidebar", exact: true });
        if (await closeSidebar.isVisible()) await closeSidebar.click();
        const resize = window.getByRole("separator", { name: "Resize panel", exact: true });
        for (let step = 0; step < 10; step += 1) {
          // oxlint-disable-next-line no-await-in-loop -- Keyboard resizing is an ordered UI gesture.
          await resize.press("ArrowLeft");
        }
      }
      const address = window.getByRole("textbox", { name: "Address bar", exact: true });
      await address.click();
      await window.keyboard.type(fixture.url);
      await window.keyboard.press("Enter");
      formPage = await eventually(
        () =>
          electronApp
            .context()
            .pages()
            .find((page) => page.url() === fixture.url),
        "embedded form WebContentsView",
      );
      const period = formPage.locator("#scenario-period");
      await period.waitFor();
      stopCapture = await startMacCapture(electronApp, windowId, recordDir);
      await window.waitForTimeout(750);

      // Establish ordinary form editing through input events before enabling design mode.
      await period.click();
      await formPage.keyboard.press("ControlOrMeta+A");
      await formPage.keyboard.type("W42", { delay: 120 });
      assert.equal(await period.inputValue(), "W42");
      await formPage.keyboard.press("ControlOrMeta+A");
      await formPage.keyboard.type("W41", { delay: 120 });
      assert.equal(await period.inputValue(), "W41");
      if (process.env.OMNIGENT_DESKTOP_COMPOSITED_VIDEO === "1") await window.waitForTimeout(800);

      await window.getByRole("button", { name: "Enter design mode", exact: true }).click();
      await period.click();
      const prompt = window.getByRole("region", { name: "Design instruction", exact: true });
      const instruction = prompt.getByRole("textbox", { name: "Describe the change", exact: true });
      await instruction.waitFor();
      assert.match(await prompt.innerText(), /Selected:\s*Period/);
      await assertPromptAboveView(electronApp, windowId, prompt, fixture.url);
      await assertShellFocus(electronApp, shellId, instruction);
      if (process.env.OMNIGENT_DESKTOP_COMPOSITED_VIDEO === "1") await window.waitForTimeout(800);
      await window.keyboard.type("asdf", { delay: 150 });
      assert.equal(await instruction.inputValue(), "asdf");
      assert.equal(
        await period.inputValue(),
        "W41",
        "design typing must not mutate the page's Period",
      );
      await window.waitForTimeout(1_500);

      await prompt.getByRole("button", { name: "Cancel", exact: true }).click();
      await instruction.waitFor({ state: "hidden" });
      assert.equal(messages.length, 0, "Cancel must not post a chat message");
      assert.equal(await period.inputValue(), "W41");

      await period.click();
      await instruction.waitFor();
      await assertShellFocus(electronApp, shellId, instruction);
      await window.keyboard.type(INSTRUCTION, { delay: 45 });
      assert.equal(await instruction.inputValue(), INSTRUCTION);
      assert.equal(await period.inputValue(), "W41");
      const accepted = window.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().endsWith(`/v1/sessions/${backend.sessionId}/events`) &&
          response.request().postDataJSON()?.type === "message",
      );
      await prompt.getByRole("button", { name: "Send", exact: true }).click();
      assert.equal(
        (await accepted).status(),
        202,
        "design instructions use the normal message endpoint",
      );
      assert.equal(messages.length, 1);
      assert.match(JSON.stringify(messages[0]), /Design Mode/);
      assert.ok(JSON.stringify(messages[0]).includes(INSTRUCTION));
      await window.getByText(MOCK_REPLY, { exact: true }).first().waitFor({ timeout: 45_000 });
      assert.equal(await period.inputValue(), "W41");
      await window.waitForTimeout(1_500);

      // Reloading the shell preserves the native view and its existing picker.
      await window.reload();
      await window.getByRole("button", { name: "Enter design mode", exact: true }).click();
      await period.click();
      await instruction.waitFor();
      await assertShellFocus(electronApp, shellId, instruction);
      await window.keyboard.type("After reload");
      assert.equal(await instruction.inputValue(), "After reload");
      assert.equal(await period.inputValue(), "W41");
      await window.keyboard.press("Escape");
      await instruction.waitFor({ state: "hidden" });
      assert.equal(messages.length, 1, "Cancel after reload must not post a second message");
    } finally {
      if (formPage && !formPage.isClosed()) {
        const diagnostics = await formPage
          .evaluate(() => ({
            focusedElement: document.activeElement?.id,
            values: Object.fromEntries(
              [...document.querySelectorAll("input")].map((input) => [input.id, input.value]),
            ),
            events: window["__reproEvents"],
          }))
          .catch((error) => ({ error: error.message }));
        fs.writeFileSync(
          path.join(recordDir, "form-diagnostics.json"),
          JSON.stringify(diagnostics, null, 2),
        );
      }
      try {
        await stopCapture();
      } finally {
        if (electronApp) await electronApp.close();
        if (fixture) await fixture.close();
        if (backend) await backend.close();
        saveRecording(recordDir, "design-prompt-renderer");
        console.log(`Desktop design prompt artifacts: ${recordDir}`);
      }
    }
  },
);
