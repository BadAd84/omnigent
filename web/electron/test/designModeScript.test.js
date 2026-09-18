const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const { JSDOM } = require("jsdom");

const { buildDesignModeScript } = require("../src/designModeScript");

const NONCE = "picker-test-nonce";
const SELECT_MARKER = `__omni_${NONCE}_element_select__`;

function captureListeners(target) {
  const listeners = [];
  const add = target.addEventListener;
  const remove = target.removeEventListener;
  const captureFlag = (options) =>
    typeof options === "boolean" ? options : Boolean(options?.capture);

  target.addEventListener = function (type, listener, options) {
    listeners.push({ type, listener, capture: captureFlag(options) });
    return add.call(this, type, listener, options);
  };
  target.removeEventListener = function (type, listener, options) {
    const index = listeners.findIndex(
      (entry) =>
        entry.type === type &&
        entry.listener === listener &&
        entry.capture === captureFlag(options),
    );
    if (index !== -1) listeners.splice(index, 1);
    return remove.call(this, type, listener, options);
  };

  return {
    count: (type) => listeners.filter((entry) => entry.type === type).length,
    invoke(type, event) {
      const matching = listeners.filter((entry) => entry.type === type);
      assert.equal(matching.length, 1, `expected one ${type} listener`);
      matching[0].listener.call(target, event);
    },
  };
}

function setRect(element, { x = 20, y = 40, width = 120, height = 30 } = {}) {
  element.getBoundingClientRect = () => ({
    x,
    y,
    width,
    height,
    left: x,
    top: y,
    right: x + width,
    bottom: y + height,
  });
}

function createPicker(t, html = '<button id="target" type="button">Target</button>', options = {}) {
  const dom = new JSDOM(html, {
    url: "https://picker.test/",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const { document } = window;
  options.beforeEnable?.(window);
  const documentListeners = captureListeners(document);
  const windowListeners = captureListeners(window);
  const messages = [];
  const focusCalls = [];
  const hitTests = [];
  let hitElement = document.querySelector("#target");
  const focus = window.HTMLElement.prototype.focus;
  window.HTMLElement.prototype.focus = function (...args) {
    focusCalls.push(this);
    return focus.apply(this, args);
  };
  window.console.log = (message) => messages.push(String(message));
  document.elementFromPoint = (x, y) => {
    hitTests.push({ x, y });
    return hitElement;
  };
  const script = buildDesignModeScript(NONCE, { promptHost: options.promptHost ?? "shell" });
  window.eval(script);
  t.after(() => {
    window["__omniDisableDesignMode"]?.();
    window.close();
  });

  return {
    window,
    document,
    documentListeners,
    windowListeners,
    messages,
    focusCalls,
    hitTests,
    script,
    hit: (element) => (hitElement = element),
    selections: () =>
      messages
        .filter((message) => message.startsWith(SELECT_MARKER))
        .map((message) => JSON.parse(message.slice(SELECT_MARKER.length))),
    // JSDOM events are never trusted; native input is covered by desktop E2E.
    invoke(type, overrides = {}) {
      const event = {
        isTrusted: true,
        button: 0,
        clientX: 25,
        clientY: 45,
        target: hitElement,
        defaultPrevented: false,
        propagationStopped: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopImmediatePropagation() {
          this.propagationStopped = true;
        },
        ...overrides,
      };
      documentListeners.invoke(type, event);
      return event;
    },
  };
}

describe("shell-hosted design picker", () => {
  it("does not inject a page prompt, focus a control, or change a modal form", (t) => {
    const picker = createPicker(
      t,
      `<body style="pointer-events:none">
        <form role="dialog" style="pointer-events:auto">
          <label for="period">Period</label><input id="period" value="W41">
          <button id="save" type="submit">Save</button>
        </form>
      </body>`,
      { beforeEnable: (window) => window.document.querySelector("#period").focus() },
    );
    const { document, window } = picker;
    const period = document.querySelector("#period");
    const save = document.querySelector("#save");
    const layer = document.querySelector("#__omni-design-layer");
    setRect(save);
    picker.hit(save);
    for (const type of ["pointerdown", "mousedown", "click"]) picker.invoke(type);

    assert.equal(layer.parentElement, document.documentElement);
    assert.equal(layer.style.pointerEvents, "none");
    assert.equal(layer.querySelector("input, textarea, button, [tabindex]"), null);
    assert.equal(document.querySelector("#__omni-popup"), null);
    assert.equal(document.querySelectorAll("input").length, 1);
    assert.equal(document.activeElement, period);
    assert.equal(period.value, "W41");
    assert.deepEqual(picker.focusCalls, []);
    assert.equal(window["__omniOnDesignResult"], undefined);
    assert.equal(window["__omniSelectedEl"], undefined);
    assert.doesNotMatch(picker.script, /\.focus\s*\(|_element_prompt_submit__|_element_dismiss__/);
    assert.equal(picker.messages.length, 1);
    assert.equal(picker.selections()[0].id, "#save");
  });

  it("captures normalized element metadata and prefers aria-label", (t) => {
    const picker = createPicker(
      t,
      `<label for="period">Associated label</label><span id="heading">Heading</span>
       <input id="period" class="field  wide muted extra" data-testid="  period-field  "
         aria-label=" Billing   period " aria-labelledby="heading" role="spinbutton" value="W41">`,
    );
    const element = picker.document.querySelector("#period");
    function Field() {}
    Field.displayName = "BillingField";
    element["__reactFiber$test"] = { type: "input", return: { type: Field } };
    setRect(element, { x: 10, y: 60, width: 160, height: 32 });
    picker.hit(element);
    picker.invoke("click");

    assert.deepEqual(picker.selections(), [
      {
        tag: "input",
        id: "#period",
        classes: ".field.wide.muted",
        text: "",
        testId: "period-field",
        ariaLabel: "Billing period",
        label: "Billing period",
        role: "spinbutton",
        component: "BillingField",
        rect: { x: 10, y: 60, width: 160, height: 32 },
      },
    ]);
    assert.equal(picker.document.querySelector("#__omni-label").textContent, "Billing period");
    assert.ok(!picker.messages[0].includes("W41"));
  });

  for (const { name, html, expected } of [
    {
      name: "aria-labelledby references",
      html: `<span id="first"> Week\n of </span><span id="second"> September 18 </span>
        <label for="target">Fallback</label>
        <input id="target" aria-label="  " aria-labelledby="missing first second">`,
      expected: "Week of September 18",
    },
    {
      name: "multiple associated labels",
      html: `<label for="target"> Weekly\n period </label>
        <label for="target"> Required </label><input id="target">`,
      expected: "Weekly period Required",
    },
    {
      name: "a wrapping label",
      html: '<label> Display\n name <input id="target"></label>',
      expected: "Display name",
    },
    {
      name: "an unlabeled field",
      html: '<input id="target">',
      expected: "",
    },
  ]) {
    it(`extracts a field label from ${name}`, (t) => {
      const picker = createPicker(t, html);
      picker.invoke("click");
      assert.equal(picker.selections()[0].label, expected);
      assert.equal(
        picker.document.querySelector("#__omni-label").textContent,
        expected || "<input>",
      );
    });
  }

  it("bounds textual metadata and detects a forwarded React component", (t) => {
    const picker = createPicker(t);
    const element = picker.document.querySelector("#target");
    element.textContent = `  ${"caption ".repeat(50)} `;
    element.setAttribute("aria-label", "a".repeat(300));
    element.setAttribute("data-testid", "b".repeat(300));
    element.setAttribute("role", "c".repeat(300));
    element["__reactInternalInstance$test"] = {
      type: { render: Object.assign(function Field() {}, { displayName: "ForwardedField" }) },
    };
    picker.invoke("click");
    const selection = picker.selections()[0];
    assert.equal(selection.text.length, 80);
    assert.ok(selection.text.startsWith("caption caption"));
    for (const key of ["ariaLabel", "label", "testId", "role"]) {
      assert.equal(selection[key].length, 200, key);
    }
    assert.equal(selection.component, "ForwardedField");
  });

  for (const type of ["pointerdown", "mousedown"]) {
    it(`prevents trusted primary ${type} from reaching the form`, (t) => {
      const picker = createPicker(t);
      const event = picker.invoke(type);
      assert.equal(event.defaultPrevented, true);
      assert.equal(event.propagationStopped, true);
      assert.deepEqual(picker.messages, []);
      assert.deepEqual(picker.focusCalls, []);
    });
  }

  it("ignores untrusted, middle-button, and right-button presses and clicks", (t) => {
    const picker = createPicker(t);
    for (const type of ["pointerdown", "mousedown", "click"]) {
      for (const overrides of [{ isTrusted: false }, { button: 1 }, { button: 2 }]) {
        const event = picker.invoke(type, overrides);
        assert.equal(event.defaultPrevented, false);
        assert.equal(event.propagationStopped, false);
      }
      const event = new picker.window.MouseEvent(type, {
        button: 0,
        bubbles: true,
        cancelable: true,
        clientX: 25,
        clientY: 45,
      });
      assert.equal(event.isTrusted, false);
      assert.equal(picker.document.querySelector("#target").dispatchEvent(event), true);
      assert.equal(event.defaultPrevented, false);
    }
    assert.deepEqual(picker.messages, []);
  });

  it("selects the current hit-test target instead of the previously hovered element", (t) => {
    const picker = createPicker(
      t,
      '<button id="first">First</button><button id="second">Second</button>',
    );
    const first = picker.document.querySelector("#first");
    const second = picker.document.querySelector("#second");
    setRect(first, { x: 10, y: 20 });
    setRect(second, { x: 200, y: 100 });
    picker.hit(first);
    picker.invoke("mousemove");
    picker.hit(second);
    const event = picker.invoke("click", { target: first, clientX: 210, clientY: 110 });
    assert.equal(event.defaultPrevented, true);
    assert.equal(event.propagationStopped, true);
    assert.equal(picker.selections()[0].id, "#second");
    assert.deepEqual(picker.hitTests.at(-1), { x: 210, y: 110 });
    assert.equal(picker.document.querySelector("#__omni-highlight").style.left, "200px");
  });

  it("ignores missing hits, the document root, and its own overlay", (t) => {
    const picker = createPicker(t);
    for (const hit of [
      null,
      picker.document.documentElement,
      picker.document.querySelector("#__omni-design-layer"),
      picker.document.querySelector("#__omni-highlight"),
      picker.document.querySelector("#__omni-label"),
    ]) {
      picker.hit(hit);
      for (const type of ["pointerdown", "mousedown", "click"]) {
        const event = picker.invoke(type);
        assert.equal(event.defaultPrevented, false);
        assert.equal(event.propagationStopped, false);
      }
    }
    assert.deepEqual(picker.messages, []);
  });

  it("keeps the selected highlight on resize and scroll, then resumes hover after clearing", (t) => {
    const picker = createPicker(t, '<input id="target"><button id="other">Other</button>');
    const element = picker.document.querySelector("#target");
    const other = picker.document.querySelector("#other");
    const overlay = picker.document.querySelector("#__omni-highlight");
    const label = picker.document.querySelector("#__omni-label");
    setRect(element);
    setRect(other, { x: 300, y: 150 });
    picker.invoke("click");
    picker.hit(other);
    picker.invoke("mousemove");
    assert.equal(overlay.style.left, "20px");

    setRect(element, { x: 45, y: 70, width: 180, height: 36 });
    picker.window.dispatchEvent(new picker.window.Event("resize"));
    assert.equal(overlay.style.left, "45px");
    assert.equal(overlay.style.top, "70px");
    assert.equal(overlay.style.width, "180px");
    assert.equal(overlay.style.height, "36px");
    assert.equal(label.style.top, "48px");

    setRect(element, { x: 45, y: 10 });
    element.dispatchEvent(new picker.window.Event("scroll"));
    assert.equal(overlay.style.top, "10px");
    assert.equal(label.style.top, "0px");
    assert.equal(picker.messages.length, 1);

    picker.window["__omniClearDesignSelection"]();
    assert.equal(overlay.style.display, "none");
    assert.equal(label.style.display, "none");
    assert.equal(picker.window["__omniDesignMode"], true);
    picker.invoke("mousemove");
    assert.equal(overlay.style.display, "block");
    assert.equal(overlay.style.left, "300px");
    picker.invoke("click");
    assert.equal(picker.selections().at(-1).id, "#other");
  });

  it("hides the highlight if the selected page element is removed", (t) => {
    const picker = createPicker(t);
    picker.invoke("click");
    picker.document.querySelector("#target").remove();
    picker.window.dispatchEvent(new picker.window.Event("resize"));
    assert.equal(picker.document.querySelector("#__omni-highlight").style.display, "none");
    assert.equal(picker.document.querySelector("#__omni-label").style.display, "none");
    assert.equal(picker.messages.length, 1);
  });

  it("is idempotent and removes picker nodes, hooks, and listeners on disable", (t) => {
    const picker = createPicker(t);
    const { document, window } = picker;
    const layer = document.querySelector("#__omni-design-layer");
    const backdropStyle = document.querySelector("style");
    window.eval(picker.script);
    assert.equal(document.querySelectorAll("#__omni-design-layer").length, 1);
    const documentEvents = ["mousemove", "pointerdown", "mousedown", "click", "scroll"];
    for (const type of documentEvents) assert.equal(picker.documentListeners.count(type), 1);
    assert.equal(picker.windowListeners.count("resize"), 1);
    window["__omniDisableDesignMode"]();
    for (const type of documentEvents) assert.equal(picker.documentListeners.count(type), 0);
    assert.equal(picker.windowListeners.count("resize"), 0);
    assert.equal(layer.isConnected, false);
    assert.equal(backdropStyle.isConnected, false);
    assert.equal(window["__omniDesignMode"], undefined);
    assert.equal(window["__omniDisableDesignMode"], undefined);
    assert.equal(window["__omniClearDesignSelection"], undefined);

    window.eval(picker.script);
    picker.invoke("click");
    assert.equal(picker.selections().length, 1);
  });

  it("uses a noninteractive manual popover when top-layer rendering is available", (t) => {
    const shown = [];
    const picker = createPicker(t, undefined, {
      beforeEnable(window) {
        window.HTMLElement.prototype.showPopover = function () {
          shown.push(this);
        };
      },
    });
    const layer = picker.document.querySelector("#__omni-design-layer");
    assert.deepEqual(shown, [layer]);
    assert.equal(layer.getAttribute("popover"), "manual");
    assert.equal(layer.style.pointerEvents, "none");
    assert.match(
      picker.document.querySelector("style").textContent,
      /::backdrop\{display:none!important\}/,
    );
    assert.deepEqual(picker.focusCalls, []);
  });

  it("keeps the overlay usable when opening its popover fails", (t) => {
    const picker = createPicker(t, undefined, {
      beforeEnable(window) {
        window.HTMLElement.prototype.showPopover = function () {
          throw new window.DOMException("Popover unavailable", "NotSupportedError");
        };
      },
    });
    assert.equal(
      picker.document.querySelector("#__omni-design-layer").hasAttribute("popover"),
      false,
    );
    picker.invoke("click");
    assert.equal(picker.document.querySelector("#__omni-highlight").style.display, "block");
    assert.equal(picker.selections().length, 1);
  });
});

it("retains the page-hosted prompt for legacy desktop clients", (t) => {
  const picker = createPicker(t, undefined, { promptHost: "page" });
  assert.equal(buildDesignModeScript(NONCE), picker.script);
  assert.equal(
    picker.document.querySelector("#__omni-popup-input").placeholder,
    "What should change?",
  );
  assert.equal(typeof picker.window["__omniOnDesignResult"], "function");
  assert.match(picker.script, /_element_prompt_submit__/);
  assert.equal(picker.document.querySelector("#__omni-design-layer"), null);
});
