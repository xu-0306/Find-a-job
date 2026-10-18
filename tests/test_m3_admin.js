"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(root, "app", "static", "admin.js"), "utf8");
const html = fs.readFileSync(path.join(root, "app", "static", "admin.html"), "utf8");
const firstBinding = script.indexOf('\nformAction($("#schedule")');
assert.ok(firstBinding > 0, "admin function prefix should end before event bindings");
const functionPrefix = script.slice(0, firstBinding);

function response(status, payload) {
  return {
    status,
    ok: status >= 200 && status < 300,
    async json() { return payload; },
  };
}

function createHarness() {
  const state = {
    api: async () => ({items: []}),
    fetchImpl: async () => response(200, {}),
    fetches: [],
    apiCalls: [],
    messages: [],
    windowEvents: [],
    signedOut: 0,
    loginContext: 0,
    dialogQueue: [],
    dialogHook: null,
    time: 0, nextTimer: 1, timers: new Map(), draws: [], bitmaps: [],
    media: {matches: false, listeners: new Set(),
      addEventListener(type, handler) { this.listeners.add(handler); },
      removeEventListener(type, handler) { this.listeners.delete(handler); },
      change(matches) { this.matches = matches; [...this.listeners].forEach(handler => handler(this)); },
    },
  };
  const elements = new Map();
  const list = () => ({
    children: [],
    replaceChildren(...items) { this.children = items; },
    append(...items) { this.children.push(...items); },
  });
  const restoreForm = {
    elements: {
      confirm: {checked: true},
      file: {files: []},
    },
  };
  const restoreStatus = {textContent: ""};
  const restoreResult = {textContent: "", hidden: true};
  const restoreDialog = {
    returnValue: "cancel",
    closeHandlers: [],
    addEventListener(type, handler) {
      if (type === "close") this.closeHandlers.push(handler);
    },
    showModal() {
      if (state.dialogHook) state.dialogHook();
      const value = state.dialogQueue.shift() || "cancel";
      Promise.resolve().then(() => {
        this.returnValue = value;
        const handlers = this.closeHandlers.splice(0);
        handlers.forEach(handler => handler());
      });
    },
  };
  elements.set("#restore-form", restoreForm);
  elements.set("#restore-status", restoreStatus);
  elements.set("#restore-result", restoreResult);
  elements.set("#restore-confirm", restoreDialog);
  elements.set("#run-list", list());
  elements.set("#backup-list", list());

  function node(tag, text, className) {
    return {
      tag, text, textContent: text || "", className, children: [], style: {}, attributes: {}, events: {},
      append(...items) { this.children.push(...items); },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.events[name] = callback; },
      async decode() {},
      getContext() { return {clearRect() {}, drawImage: (...args) => state.draws.push({canvas: this, args})}; },
    };
  }
  function $(selector) {
    const value = elements.get(selector);
    if (!value) throw new Error(`unexpected selector ${selector}`);
    return value;
  }
  class FormDataStub {
    constructor(form) { this.form = form; }
  }
  const context = {
    console,
    URL,
    Event: function Event(type) { this.type = type; },
    Promise,
    Number,
    Object,
    String,
    Uint8Array, atob, Blob,
    async createImageBitmap() {
      const bitmap = {closed: false, close() { this.closed = true; }};
      state.bitmaps.push(bitmap); return bitmap;
    },
    FormData: FormDataStub,
    $,
    node,
    csrfToken: "",
    api: async (...args) => {
      state.apiCalls.push(args);
      return state.api(...args);
    },
    fetch: async (...args) => {
      state.fetches.push(args);
      return state.fetchImpl(...args);
    },
    clearTimeout(id) { state.timers.delete(id); },
    setTimeout(callback, delay) {
      const id = state.nextTimer++; state.timers.set(id, {at: state.time + delay, callback}); return id;
    },
    window: {
      dispatchEvent(event) { state.windowEvents.push(event.type); },
      matchMedia() { return state.media; },
    },
    message(text, failed = false) { state.messages.push({text, failed}); },
    signedOut() {
      state.signedOut += 1;
      restoreForm.elements.confirm.checked = false;
      restoreForm.elements.file.files = [];
      context.csrfToken = "";
    },
    async loginContext() {
      state.loginContext += 1;
      context.csrfToken = "renewed-token";
    },
  };
  vm.createContext(context);
  const common = fs.readFileSync(path.join(root, "app/static/common.js"), "utf8");
  vm.runInContext(common.match(/^function formatTaipeiDateTime[\s\S]*?^}/m)[0], context);
  vm.runInContext(fs.readFileSync(path.join(root, "app/static/sprite.js"), "utf8"), context);
  vm.runInContext(functionPrefix, context, {filename: "admin.js#functions"});
  function advance(ms) {
    const target = state.time + ms;
    while (true) {
      const due = [...state.timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      const [id, timer] = due; state.timers.delete(id); state.time = timer.at; timer.callback();
    }
    state.time = target;
  }
  return {context, state, elements, restoreForm, restoreDialog, restoreResult, advance};
}

function setToken(harness, value) {
  harness.context.csrfToken = value;
}

function collectText(value) {
  if (!value) return "";
  return [value.text, ...(value.children || []).map(collectText)].filter(Boolean).join(" ");
}

async function testSpriteTemplatePreviewRequiredAndSizeGuard() {
  const harness = createHarness();
  const form = {elements: {confirm: {checked: true}, file: {files: [{size: 100}]}, fps: {value: "8"}, columns: {value: "8"}, clean_edges: {checked:false}}};
  harness.elements.set("#sprite-template", form);
  harness.elements.set("button[type=submit]", {disabled: false});
  harness.elements.set("#sprite-template-preview", {replaceChildren() {}});
  harness.elements.set("#sprite-template-status", {textContent: ""});
  await assert.rejects(harness.context.submitSpriteTemplate(), /請先預覽/);
  form.elements.file.files = [{size: 5 * 1024 * 1024 + 1}];
  await assert.rejects(harness.context.previewSpriteTemplate(), /不可超過 5 MiB/);
  assert.equal(form.elements.confirm.checked, false);
  assert.equal(form.elements.confirm.disabled, true);
  assert.equal(harness.state.fetches.length, 0, "unpreviewed or oversized files must not be sent");
}

async function testSingleSearchConnectionStatus() {
  const harness = createHarness();
  const status = {textContent: ""}; harness.elements.set("#search-status", status);
  harness.state.api = async pathValue => {
    assert.equal(pathValue, "/api/admin/search-test");
    return {provider:"Tavily",passed:true,result_count:0,duration_ms:42,tested_at:"2026-10-07T00:00:00Z"};
  };
  await harness.context.testSearchConnection();
  assert.match(status.textContent, /連線測試通過 · 0 筆可用結果/);
  assert.match(status.textContent, /台灣時間/);
  assert.equal(harness.state.apiCalls.length, 1);
  harness.state.api = async () => { throw new Error("金鑰無效"); };
  await assert.rejects(harness.context.testSearchConnection(), /金鑰無效/);
  assert.match(status.textContent, /連線測試未通過：金鑰無效/);
  assert.ok(!html.includes('id="run-comparison"') && !html.includes('id="comparison-results"'));
  assert.ok(!html.includes('id="search-connection"'), "search controls belong to the existing service settings");
  assert.match(html, /value="auto">自動（使用已設定金鑰的服務）/);
}

async function testModelConnectionHttpErrorDisplay() {
  const harness = createHarness();
  const status = {textContent: ""};
  const button = {};
  harness.elements.set("#llm-status", status);
  harness.elements.set("#llm-test", button);
  let testConnection;
  harness.context.action = (target, handler) => {
    assert.equal(target, button); testConnection = handler;
  };
  const start = script.indexOf('action($("#llm-test"),');
  const end = script.indexOf('action($("#add-default"),', start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(script.slice(start, end), harness.context);
  for (const code of [402, 503, 529]) {
    const detail = `模型服務回傳 HTTP ${code}（responses），請在後台檢查設定。`;
    harness.state.api = async target => {
      assert.equal(target, "/api/admin/llm-test");
      throw Object.assign(new Error(detail), {status: 503});
    };
    await assert.rejects(testConnection(), error => error.message === detail);
    assert.equal(status.textContent, detail, "show the provider HTTP code, independent of the app's 503 response");
  }
}

// Account management: the section between the markers is loaded with its own bindings, like the other contracts.
function accountsHarness() {
  const harness = createHarness();
  const list = {children: [], replaceChildren(...items) { this.children = items; }, append(...items) { this.children.push(...items); }};
  const dialog = {returnValue: "cancel", shown: 0, showModal() { this.shown += 1; }, closeHandlers: [],
    addEventListener(type, handler) { assert.equal(type, "close"); this.closeHandlers.push(handler); }};
  for (const [selector, value] of [["#user-list", list], ["#user-confirm", dialog], ["#user-confirm-title", {textContent: ""}],
    ["#user-confirm-text", {textContent: ""}], ["#user-confirm-ok", {textContent: ""}],
    ["#access-link", {value: "old-link", focus() { this.focused = true; }, select() { this.selected = true; }}],
    ["#copy-access", {disabled: true, events: {}, addEventListener(type, handler) { this.events[type] = handler; }}],
    ["#access-owner", {textContent: ""}], ["#access-copy-status", {textContent: ""}],
    ["#open-access", {href: "old-link", removeAttribute() { delete this.href; }}], ["#access-result", {hidden: false}],
    ["#user-form", {}]]) {
    harness.elements.set(selector, value);
  }
  harness.context.window.location = {origin: "http://localhost:8041"};
  let submitCreate;
  harness.context.formAction = (form, handler) => { submitCreate = handler; };
  const begin = script.indexOf("async function copyAccess()");
  const finish = script.indexOf("// --- end accounts ---", begin);
  assert.ok(begin >= 0 && finish > begin);
  vm.runInContext(script.slice(begin, finish), harness.context, {filename: "admin.js#accounts"});
  const bindStart = script.indexOf('$("#user-confirm").addEventListener("close",');
  const bindEnd = script.indexOf('$("#hide-access").addEventListener(', bindStart);
  assert.ok(bindStart >= 0 && bindEnd > bindStart);
  vm.runInContext(script.slice(bindStart, bindEnd), harness.context, {filename: "admin.js#account-bindings"});
  const close = async value => {
    dialog.returnValue = value;
    for (const handler of dialog.closeHandlers) await handler();
  };
  return {harness, list, dialog, close, create: () => submitCreate()};
}

function buttonLabels(row) {
  return row.children[2].children.map(button => button.textContent);
}

async function testAccessLinkOriginAndLocalWarning() {
  for (const origin of [null, "https://jobs.example.invalid", "https://203.0.113.10:8443", "https://[2001:db8::1]:9443"]) {
    const {harness, dialog, close} = accountsHarness();
    const calls = [];
    harness.state.api = async (target, options) => {
      calls.push([target, options?.method]);
      if (target === "/api/admin/users/default/access/reset") {
        return {access_path: "/access?token=synthetic-test-only", access_origin: origin};
      }
      assert.equal(target, "/api/admin/users");
      return {items: []};
    };
    harness.context.requestUserAction("link", {id: "default", username: "owner"});
    assert.equal(dialog.shown, 1);
    assert.equal(harness.elements.get("#user-confirm-title").textContent, "重設專屬連結？");
    assert.match(harness.elements.get("#user-confirm-text").textContent, /「owner」的舊連結會失效/);
    assert.equal(calls.length, 0, "nothing is sent before the administrator confirms");
    await close("confirm");
    const expected = (origin || "http://localhost:8041") + "/access?token=synthetic-test-only";
    assert.equal(harness.elements.get("#access-link").value, expected);
    assert.equal(harness.elements.get("#open-access").href, expected);
    assert.equal(harness.elements.get("#access-owner").textContent, "「owner」的專屬連結");
    assert.equal(harness.elements.get("#access-result").hidden, false);
    assert.equal(harness.state.messages.at(-1).text.includes("僅供本機使用"), !origin);
    assert.deepEqual(calls.map(call => call[0]), ["/api/admin/users/default/access/reset", "/api/admin/users"]);
  }
}

async function testAccessLinkCopyAndClear() {
  const {harness} = accountsHarness();
  const input = harness.elements.get("#access-link");
  const button = harness.elements.get("#copy-access");
  const status = harness.elements.get("#access-copy-status");
  const copied = [];
  harness.context.navigator = {clipboard: {async writeText(value) { copied.push(value); }}};
  harness.context.hideAccess();
  assert.equal(button.disabled, true);
  await button.events.click();
  assert.equal(copied.length, 0, "empty links cannot be copied");
  for (const [origin, path] of [
    [null, "/access?token=synthetic-local-copy"],
    ["https://another.example.invalid:8443", "/access?token=synthetic-reset-copy&extra=encoded%2Bvalue"],
  ]) {
    harness.context.showAccess({access_path: path, access_origin: origin}, "owner", false);
    assert.equal(button.disabled, false);
    assert.equal(status.textContent, "", "new links clear prior copy feedback");
    const message = harness.state.messages.at(-1);
    await button.events.click();
    assert.equal(copied.at(-1), (origin || "http://localhost:8041") + path, "copy uses the complete current generated URL");
    assert.match(status.textContent, /已複製完整連結/);
    assert.equal(harness.state.messages.at(-1), message, "copy feedback preserves existing sharing guidance");
    assert.equal(harness.elements.get("#access-result").hidden, false);
  }
  const current = input.value;
  for (const clipboard of [undefined, {async writeText() { throw new Error("permission denied"); }}]) {
    harness.context.navigator.clipboard = clipboard;
    input.focused = input.selected = false;
    await button.events.click();
    assert.ok(input.focused && input.selected, "failure selects the current link for manual copying");
    assert.match(status.textContent, /未能自動複製.*Ctrl\+C/);
    assert.equal(input.value, current, "failed copying must preserve the link");
    assert.equal(button.disabled, false);
  }
  for (const denied of [false, true]) {
    let finish;
    harness.context.navigator.clipboard = {writeText: () => new Promise((resolve, reject) => {
      finish = () => denied ? reject(new Error("permission denied")) : resolve();
    })};
    const pending = button.events.click();
    harness.context.hideAccess();
    input.focused = input.selected = false;
    const messages = harness.state.messages.length;
    finish(); await pending;
    assert.equal(input.value, "");
    assert.equal(status.textContent, "", "cleared links ignore late clipboard feedback");
    assert.equal(button.disabled, true);
    assert.equal(harness.elements.get("#open-access").href, undefined);
    assert.equal(harness.elements.get("#access-result").hidden, true);
    assert.equal(harness.state.messages.length, messages, "cleared links ignore late clipboard replies");
    assert.ok(!input.focused && !input.selected);
    harness.context.showAccess({access_path: "/access?token=synthetic-next-copy"}, "owner", false);
  }
  assert.equal(harness.state.fetches.length + harness.state.apiCalls.length, 0, "copy actions remain local");
  assert.match(html, /id="copy-access"[^>]*type="button"[^>]*disabled>複製連結/);
  assert.match(html, /id="access-copy-status"[^>]*role="status"[^>]*aria-live="polite"/);
}

async function testAccountListShowsOnlyApplicableActions() {
  const {harness, list} = accountsHarness();
  harness.context.renderUsers([
    {id: "a", username: "alice", has_link: true, disabled: false, devices: 2},
    {id: "b", username: "bob", has_link: false, disabled: false, devices: 0},
    {id: "c", username: "carol", has_link: true, disabled: true, devices: 0},
  ]);
  const [alice, bob, carol] = list.children;
  assert.deepEqual(buttonLabels(alice), ["重設專屬連結", "移除連結", "登出所有裝置", "停用", "刪除帳號"]);
  assert.deepEqual(buttonLabels(bob), ["建立專屬連結", "停用", "刪除帳號"]);
  assert.deepEqual(buttonLabels(carol), ["移除連結", "啟用", "刪除帳號"], "a disabled account cannot be given a link");
  assert.match(collectText(alice), /已有專屬連結・2 台裝置已登入/);
  assert.match(collectText(bob), /尚無專屬連結・0 台裝置已登入/);
  assert.match(collectText(carol), /已停用・已有專屬連結/);
  harness.context.renderUsers([]);
  assert.match(collectText(list.children[0]), /尚無使用者帳號/);
  // Link buttons follow the Funnel busy state so a link is never issued mid-change.
  vm.runInContext("sharingBusy = true", harness.context);
  harness.context.renderUsers([{id: "a", username: "alice", has_link: false, disabled: false, devices: 0}]);
  const linkButton = list.children[0].children[2].children[0];
  assert.equal(linkButton.disabled, true);
  vm.runInContext("sharingBusy = false; updateSharingButtons = function () { linkButtons.forEach(button => { button.disabled = sharingBusy; }); }", harness.context);
  vm.runInContext("updateSharingButtons()", harness.context);
  assert.equal(linkButton.disabled, false);
}

async function testAccountActionsConfirmBeforeAnythingIsSent() {
  const {harness, list, dialog, close} = accountsHarness();
  const calls = [];
  harness.state.api = async (target, options = {}) => {
    calls.push({target, method: options.method, body: options.body && JSON.parse(options.body)});
    return target === "/api/admin/users" && !options.method ? {items: []} : null;
  };
  const user = {id: "b", username: "bob", has_link: true, disabled: false, devices: 1};
  harness.context.renderUsers([user]);
  const buttons = Object.fromEntries(list.children[0].children[2].children.map(button => [button.textContent, button]));
  const expected = {
    "移除連結": ["DELETE", "/api/admin/users/b/access"],
    "登出所有裝置": ["POST", "/api/admin/users/b/sign-out"],
    "停用": ["PATCH", "/api/admin/users/b"],
    "刪除帳號": ["DELETE", "/api/admin/users/b?confirm=true"],
  };
  for (const [label, [method, target]] of Object.entries(expected)) {
    calls.length = 0;
    buttons[label].events.click();
    assert.ok(dialog.shown > 0);
    assert.equal(calls.length, 0, `${label} waits for confirmation`);
    await close("cancel");
    assert.equal(calls.length, 0, `${label} cancelled`);
    buttons[label].events.click();
    await close("confirm");
    assert.deepEqual([calls[0].method, calls[0].target], [method, target], label);
    assert.equal(calls.at(-1).target, "/api/admin/users", "the list is refreshed afterwards");
  }
  assert.deepEqual(calls.find(call => call.method === "PATCH") ?? null, null, "the last action was the delete");
  assert.match(harness.elements.get("#user-confirm-text").textContent, /所有對話、收藏、求職條件與每日職缺，無法復原/);
  // Enabling is harmless and runs at once; the disable request carries the flag.
  calls.length = 0;
  harness.context.renderUsers([{...user, disabled: true}]);
  const enable = list.children[0].children[2].children.find(button => button.textContent === "啟用");
  const shown = dialog.shown;
  enable.events.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(dialog.shown, shown, "enabling needs no confirmation");
  assert.deepEqual([calls[0].method, calls[0].target, calls[0].body], ["PATCH", "/api/admin/users/b", {disabled: false}]);
  calls.length = 0;
  harness.context.renderUsers([user]);
  list.children[0].children[2].children.find(button => button.textContent === "停用").events.click();
  await close("confirm");
  assert.deepEqual(calls[0].body, {disabled: true});
}

async function testAccountFailuresAreShownNotThrown() {
  const {harness, dialog, close} = accountsHarness();
  harness.state.api = async () => { throw Object.assign(new Error("找不到這個帳號。"), {status: 404}); };
  harness.context.requestUserAction("delete", {id: "gone", username: "gone"});
  await close("confirm");
  assert.deepEqual(harness.state.messages.at(-1), {text: "找不到這個帳號。", failed: true});
  assert.equal(dialog.shown, 1);
  // A failed request releases the lock so the next action is not swallowed.
  let sent = 0;
  harness.state.api = async target => { sent += 1; return target === "/api/admin/users" ? {items: []} : null; };
  harness.context.requestUserAction("sign-out", {id: "b", username: "bob"});
  await close("confirm");
  assert.ok(sent >= 1);
}

async function testCreatingAnAccountShowsItsLinkOnce() {
  const {harness, create} = accountsHarness();
  const input = {value: "  carol "};
  harness.elements.set("#user-form [name=username]", input);
  const calls = [];
  harness.state.api = async (target, options = {}) => {
    calls.push({target, method: options.method, body: options.body && JSON.parse(options.body)});
    if (target === "/api/admin/users" && options.method === "POST") {
      return {id: "new", username: "carol", has_link: true, disabled: false, devices: 0,
        access_path: "/access?token=synthetic-new-account", access_origin: "https://jobs.example.invalid"};
    }
    return {items: []};
  };
  await create();
  assert.deepEqual(calls[0], {target: "/api/admin/users", method: "POST", body: {username: "carol"}});
  assert.equal(harness.elements.get("#access-link").value, "https://jobs.example.invalid/access?token=synthetic-new-account");
  assert.equal(harness.elements.get("#access-owner").textContent, "「carol」的專屬連結");
  assert.equal(input.value, "");
  assert.match(harness.state.messages.at(-1).text, /已建立「carol」的專屬連結/);
  assert.equal(calls.at(-1).target, "/api/admin/users", "the list is refreshed afterwards");
  assert.ok(!html.includes('name="password"') || !/id="user-form"[\s\S]*?name="password"/.test(html), "accounts have no password field");
}

function sharingStatus(state, overrides = {}) {
  return {state, message: `狀態：${state}`, origin: null, action_url: null, can_disable: false,
    local_port: 8088, public_port: 8443, restart_required: false, ...overrides};
}

function addSharingElements(harness) {
  const link = () => ({textContent: "", href: "", hidden: true, removeAttribute(name) { delete this[name]; }});
  harness.elements.set("#sharing-status", {textContent: "", className: ""});
  harness.elements.set("#sharing-guidance", {textContent: ""});
  harness.elements.set("#sharing-ports", {textContent: ""});
  harness.elements.set("#sharing-origin-row", {hidden: true});
  harness.elements.set("#sharing-origin", link());
  harness.elements.set("#sharing-action-link", link());
  harness.elements.set("#sharing-primary", {textContent: "", disabled: false});
  harness.elements.set("#sharing-refresh", {hidden: false, disabled: false});
  harness.elements.set("#sharing-disable", {hidden: true, disabled: false});
  harness.elements.set("#access-guidance", {textContent: ""});
  harness.elements.set("#ports-save", {disabled: true});
}

async function testSharingStateActionsAndProgressiveInfo() {
  const harness = createHarness(); addSharingElements(harness);
  for (const [state, label] of [
    ["cli_missing", "安裝 Tailscale"], ["needs_login", "登入 Tailscale"],
    ["stopped", "啟動 Tailscale"], ["disabled", "啟用 Funnel"], ["configured", "再次測試"], ["conflict", "重新檢查"],
  ]) {
    harness.context.renderSharingStatus(sharingStatus(state));
    assert.equal(harness.elements.get("#sharing-primary").textContent, label, state);
    assert.equal(harness.elements.get("#sharing-disable").hidden, true, `${state} must not show disable without permission`);
    if (["cli_missing", "needs_login", "stopped", "disabled"].includes(state)) {
      assert.equal(harness.elements.get("#sharing-refresh").hidden, false, `${state} keeps a separate status check`);
    }
    assert.equal(harness.state.timers.size, 0, `${state} must not poll continuously`);
  }
  harness.context.renderSharingStatus(sharingStatus("stopped"));
  assert.equal(harness.context.primarySharingAction(), "login", "stopped means the Tailscale client needs starting, not Funnel enablement");
  assert.match(harness.elements.get("#sharing-guidance").textContent, /啟動並登入 Tailscale/);

  const authUrl = "https://login.tailscale.com/a/example?next=%2Ffunnel";
  harness.context.renderSharingStatus(sharingStatus("consent_required", {action_url: authUrl}));
  assert.equal(harness.elements.get("#sharing-primary").textContent, "已完成，重新啟用");
  assert.equal(harness.elements.get("#sharing-action-link").href, authUrl);
  assert.equal(harness.elements.get("#sharing-action-link").hidden, false);
  assert.match(harness.elements.get("#sharing-guidance").textContent, /允許 Funnel/);

  harness.context.renderSharingStatus(sharingStatus("configured", {
    origin: "https://host.tailnet.ts.net:8443", can_disable: true,
  }));
  assert.equal(harness.elements.get("#sharing-origin-row").hidden, false);
  assert.equal(harness.elements.get("#sharing-disable").hidden, false);
  assert.match(harness.elements.get("#sharing-guidance").textContent, /尚未通過/);

  harness.context.renderSharingStatus(sharingStatus("ready", {
    origin: "https://host.tailnet.ts.net:8443", can_disable: true,
  }));
  assert.equal(harness.elements.get("#sharing-primary").textContent, "重新檢查");
  assert.equal(harness.elements.get("#sharing-refresh").hidden, true, "ready exposes one recheck action");
  assert.equal(harness.elements.get("#sharing-origin").textContent, "https://host.tailnet.ts.net:8443");
  assert.equal(harness.elements.get("#sharing-guidance").textContent, "電腦和本程式須保持運行；可用手機行動網路確認外部連線。");
  assert.match(harness.elements.get("#access-guidance").textContent, /對方不需安裝 Tailscale/);
  assert.equal(harness.elements.get("#sharing-ports").textContent, "公開埠 8443 · 本機前台 proxy 8088");
  assert.equal(harness.context.safeSharingOrigin("http://host.tailnet.ts.net:8443"), null);
  assert.equal(harness.context.safeSharingOrigin("https://host.tailnet.ts.net:443"), null);
  assert.equal(harness.context.safeSharingOrigin("https://device.ts.net:8443"), "https://device.ts.net:8443");
  assert.equal(harness.context.safeSharingActionUrl("https://evil.example/login"), null);
}

async function testSharingActionsInstallationPollAndLateReply() {
  const harness = createHarness(); addSharingElements(harness); setToken(harness, "sharing-token");
  harness.state.api = async (pathValue, options) => {
    assert.equal(pathValue, "/api/admin/sharing");
    if (options) {
      assert.equal(options.method, "POST");
      assert.equal(options.body, JSON.stringify({action: "install"}));
      return sharingStatus("installing", {message: "正在安裝 Tailscale"});
    }
    return sharingStatus("installing", {message: "仍在安裝"});
  };
  await harness.context.performSharingAction("install");
  assert.equal(harness.elements.get("#sharing-primary").disabled, true);
  assert.equal(harness.elements.get("#sharing-primary").textContent, "正在安裝…");
  assert.equal(harness.state.timers.size, 1);
  assert.equal([...harness.state.timers.values()][0].at, 3000);
  await harness.context.refreshSharingStatus();
  assert.equal(harness.state.apiCalls.at(-1)[1], undefined, "installation polling uses GET status");
  assert.equal(harness.state.timers.size, 1, "install polling continues only while installation remains active");

  let release;
  harness.state.api = () => new Promise(resolve => { release = resolve; });
  const pending = harness.context.refreshSharingStatus();
  harness.context.invalidateSharing(); setToken(harness, "new-session-token");
  const afterSignOut = harness.elements.get("#sharing-status").textContent;
  release(sharingStatus("ready", {origin: "https://host.tailnet.ts.net:8443"}));
  await pending;
  assert.equal(harness.elements.get("#sharing-status").textContent, afterSignOut, "a stale sharing response must not overwrite the signed-out page");
  assert.equal(harness.state.timers.size, 0, "sign-out invalidates install polling");

  const duplicate = createHarness(); addSharingElements(duplicate); setToken(duplicate, "sharing-token");
  let finishAction;
  duplicate.state.api = () => new Promise(resolve => { finishAction = resolve; });
  const first = duplicate.context.performSharingAction("enable");
  await duplicate.context.performSharingAction("enable");
  assert.equal(duplicate.state.apiCalls.length, 1, "a pending Funnel action cannot be submitted twice");
  finishAction(sharingStatus("ready", {origin: "https://host.tailnet.ts.net:8443", can_disable: true}));
  await first;
}

async function testPortSettingsSaveAndPendingRestart() {
  const harness = createHarness(); addSharingElements(harness); setToken(harness, "ports-token");
  const fields = Object.fromEntries(["admin_port", "local_port", "public_port"].map(name => [name, {value: "", disabled: true}]));
  harness.elements.set("#ports-form", {elements: fields, reportValidity: () => true});
  harness.elements.set("#ports-current", {textContent: ""});
  harness.elements.set("#ports-status", {textContent: "", hidden: true});
  harness.elements.set("#ports-next-row", {hidden: true});
  harness.elements.set("#ports-next-admin", {href: "", textContent: "", removeAttribute(name) { delete this[name]; }});
  const current = {admin_port: 8041, local_port: 8088, public_port: 8443};
  harness.context.renderPorts({ports: current, current, editable: true, restart_required: false});
  assert.equal(fields.admin_port.disabled, false);
  fields.admin_port.value = "9041"; fields.local_port.value = "9088"; fields.public_port.value = "10000";
  let saved;
  harness.state.api = async (pathValue, options) => {
    if (pathValue === "/api/admin/ports") {
      assert.equal(options.method, "PUT"); saved = JSON.parse(options.body);
      return {ports: saved, current, editable: true, restart_required: true};
    }
    assert.equal(pathValue, "/api/admin/sharing");
    return sharingStatus("disabled", {restart_required: true});
  };
  // Access tokens are cleared by the shared helper, already tested separately.
  harness.context.hideAccess = () => {};
  await harness.context.savePorts();
  assert.deepEqual(saved, {admin_port: 9041, local_port: 9088, public_port: 10000});
  assert.match(harness.elements.get("#ports-status").textContent, /已保存，尚未生效/);
  assert.match(harness.elements.get("#ports-current").textContent, /本機後台 8041/);
  assert.equal(harness.elements.get("#ports-next-admin").href, "http://localhost:9041/admin");
  assert.equal(harness.elements.get("#sharing-primary").disabled, true, "enable waits for the new listener ports");
  for (const publicPort of [443, 10000]) {
    const origin = publicPort === 443 ? "https://device.ts.net" : "https://device.ts.net:10000";
    harness.context.renderSharingStatus(sharingStatus("ready", {public_port: publicPort, local_port: 9088, origin, can_disable: true}));
    assert.equal(harness.elements.get("#sharing-origin").href, origin);
    assert.match(harness.elements.get("#sharing-ports").textContent, /本機前台 proxy 9088/);
  }
}

async function testSharingEnableConsentAndCheckContract() {
  const harness = createHarness(); addSharingElements(harness); setToken(harness, "sharing-token");
  const calls = [];
  harness.state.api = async (pathValue, options) => {
    calls.push({pathValue, options});
    const action = JSON.parse(options.body).action;
    if (action === "enable") return calls.length === 1
      ? sharingStatus("consent_required", {action_url: "https://login.tailscale.com/a/approve"})
      : sharingStatus("configured", {origin: "https://host.tailnet.ts.net:8443", can_disable: true});
    return sharingStatus("ready", {origin: "https://host.tailnet.ts.net:8443", can_disable: true});
  };
  await harness.context.performSharingAction("enable");
  assert.equal(calls[0].pathValue, "/api/admin/sharing");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.body, JSON.stringify({action: "enable"}));
  assert.equal(harness.elements.get("#sharing-action-link").href, "https://login.tailscale.com/a/approve");
  assert.equal(harness.elements.get("#sharing-primary").textContent, "已完成，重新啟用");
  await harness.context.performSharingAction("enable");
  assert.equal(vm.runInContext("sharingSnapshot.state", harness.context), "configured");
  assert.match(harness.elements.get("#sharing-guidance").textContent, /尚未通過/);
  assert.equal(harness.elements.get("#sharing-disable").hidden, false, "can_disable controls the stop action");
  await harness.context.performSharingAction("check");
  assert.equal(calls[2].options.body, JSON.stringify({action: "check"}));
  assert.equal(vm.runInContext("sharingSnapshot.state", harness.context), "ready");
  assert.match(harness.elements.get("#access-guidance").textContent, /建立專屬連結/);

  harness.state.api = async (pathValue, options) => {
    const action = JSON.parse(options.body).action;
    if (action === "disable") return sharingStatus("disabled");
    assert.equal(action, "check");
    return sharingStatus("ready", {origin: "https://host.tailnet.ts.net:8443", can_disable: true});
  };
  await harness.context.performSharingAction("disable");
  assert.equal(harness.elements.get("#sharing-disable").hidden, true);
  const disabledState = vm.runInContext("sharingSnapshot.state", harness.context);
  assert.equal(disabledState, "disabled", "only an explicit disable action stops Funnel");

  const login = createHarness(); addSharingElements(login); setToken(login, "login-token");
  login.context.renderSharingStatus(sharingStatus("needs_login"));
  assert.equal(login.elements.get("#sharing-primary").textContent, "登入 Tailscale");
  let loginAction;
  login.state.api = async (pathValue, options) => {
    loginAction = JSON.parse(options.body).action;
    return sharingStatus("needs_login", {action_url: "https://login.tailscale.com/a/login"});
  };
  await login.context.performSharingAction(login.context.primarySharingAction());
  assert.equal(loginAction, "login");
  assert.equal(login.elements.get("#sharing-action-link").textContent, "開啟 Tailscale 官方登入頁面");
  assert.equal(login.elements.get("#sharing-primary").textContent, "登入 Tailscale");
  assert.equal(login.elements.get("#sharing-refresh").hidden, false, "the separate check remains available during login");
}

async function testRunNotificationSummary() {
  const harness = createHarness();
  harness.state.api = async () => ({items: [{
    local_date: "2026-10-05", status: "success", started_at: "09:00", trigger: "daily",
    new_count: 2, initial_count: 1, source_results: [],
    notifications: {accepted: 2, expired: 1, failed: 3, unknown: 4, blocked: 5},
  }]});
  setToken(harness, "stable-token");
  await harness.context.loadRuns();
  const text = collectText(harness.elements.get("#run-list"));
  assert.match(text, /accepted 2/);
  assert.match(text, /expired 1/);
  assert.match(text, /failed 3/);
  assert.match(text, /unknown 4/);
  assert.match(text, /blocked 5/);
  assert.match(text, /accepted 僅代表伺服器接受，不代表裝置已顯示通知/);
}

async function testBackupHrefAndCreatedAt() {
  const harness = createHarness();
  harness.state.api = async pathValue => {
    assert.equal(pathValue, "/api/admin/backups");
    return {items: [{id: "abc123", size: 12, created_at: "2026-10-05T12:00:00+08:00"}]};
  };
  await harness.context.loadBackups();
  const backup = harness.elements.get("#backup-list").children[0];
  const link = backup.children.find(item => item && item.tag === "a");
  assert.ok(link, "backup list should render a download link");
  assert.equal(link.href, "/api/admin/backups/abc123");
  assert.ok(link.href.startsWith("/api/admin/backups/"));
  assert.ok(!link.href.includes("http"));
  assert.match(collectText(backup), /2026\/10\/5.*12:00/);
}

function extractFunction(source, name) {
  const match = source.match(new RegExp(`^(?:async )?function ${name}\\b[\\s\\S]*?^}`, "m"));
  assert.ok(match, `${name} should be extractable from its source`);
  return match[0];
}

async function testSourceSwitchesLoadToggleAndSave() {
  for (const [source, enabled] of [["518", true], ["1111", false]]) {
    assert.match(html, new RegExp(`<button[^>]*name="source_${source}_enabled"[^>]*type="button"[^>]*role="switch"[^>]*aria-checked="${enabled}"[^>]*aria-labelledby="source-${source}-label"[^>]*aria-describedby="source-${source}-help"`));
    assert.doesNotMatch(html, new RegExp(`<input[^>]*name="source_${source}_enabled"`));
  }
  assert.match(html, /1111（實驗功能）/);
  assert.match(html, /可能需要人機驗證而暫時查不到/);
  assert.match(html, /name="admin_local_only" type="checkbox"/);
  const harness = createHarness();
  const common = fs.readFileSync(path.join(root, "app", "static", "common.js"), "utf8");
  vm.runInContext(common.match(/^const secretKeys = .*$/m)[0] + "\n" + extractFunction(common, "formAction"), harness.context);
  vm.runInContext(extractFunction(script, "setSourceSwitch") + "\n" + extractFunction(script, "renderLLMSettings") + "\n" + extractFunction(script, "loadSettings"), harness.context);
  harness.context.loadModelProfiles = async () => {};
  const servicesSave = script.match(/^formAction\(\$\("#services"\), async \(\) => \{[\s\S]*?^\}\);/m);
  assert.ok(servicesSave, "services save handler should be extractable");
  const names = ["llm_base_url", "llm_model", "llm_protocol", "llm_context_window", "vapid_subject", "search_provider", "admin_local_only",
    "source_518_enabled", "source_1111_enabled", "llm_api_key", "brave_api_key", "tavily_api_key", "clear_llm_api_key", "clear_brave_api_key", "clear_tavily_api_key"];
  const elements = Object.fromEntries(names.map(name => [name, {value: "", checked: false}]));
  for (const name of ["source_518_enabled", "source_1111_enabled"]) {
    elements[name] = {attributes: {}, events: {}, textContent: "",
      setAttribute(key, value) { this.attributes[key] = value; },
      getAttribute(key) { return this.attributes[key]; },
      addEventListener(type, handler) { this.events[type] = handler; }};
  }
  const listeners = {};
  const form = {elements, addEventListener(type, handler) { listeners[type] = handler; }, reset() { listeners.reset(); }};
  harness.elements.set("#services", form);
  for (const selector of ["#llm-status", "#search-test", "#search-status", "#search-engine-name",
    ...["llm_api_key", "brave_api_key", "tavily_api_key"].map(key => `[data-status=${key}]`)]) {
    harness.elements.set(selector, {textContent: "", disabled: false});
  }
  harness.elements.set("button[type=submit]", {disabled: false});
  const bindStart = script.indexOf('for (const name of ["source_518_enabled", "source_1111_enabled"])');
  const bindEnd = script.indexOf('formAction($("#services"),', bindStart);
  assert.ok(bindStart >= 0 && bindEnd > bindStart);
  vm.runInContext(script.slice(bindStart, bindEnd) + servicesSave[0], harness.context);
  let stored = {}, saved = null;
  const settings = () => ({
    llm_base_url: "", llm_model: "", llm_protocol: "auto", vapid_subject: "", search_provider: "",
    admin_local_only: true, ...stored, connection_test_status: "pending", active_search_provider: null,
    llm_api_key: {configured: false, mask: ""}, brave_api_key: {configured: false, mask: ""}, tavily_api_key: {configured: false, mask: ""},
  });
  harness.state.api = async (pathValue, options) => {
    assert.equal(pathValue, "/api/admin/settings");
    if (options?.method === "PUT") {
      saved = JSON.parse(options.body);
      stored = {source_518_enabled: saved.source_518_enabled, source_1111_enabled: saved.source_1111_enabled};
    }
    return settings();
  };
  const assertSwitch = (name, enabled) => {
    assert.equal(elements[name].getAttribute("aria-checked"), String(enabled));
    assert.equal(elements[name].textContent, enabled ? "開啟" : "關閉");
  };
  await harness.context.loadSettings();
  assertSwitch("source_518_enabled", true);
  assertSwitch("source_1111_enabled", false);
  for (const name of ["source_518_enabled", "source_1111_enabled"]) {
    for (const enabled of [false, true]) {
      stored[name] = enabled;
      await harness.context.loadSettings();
      assertSwitch(name, enabled);
      const callsBefore = harness.state.apiCalls.length;
      const other = name === "source_518_enabled" ? "source_1111_enabled" : "source_518_enabled";
      const otherState = elements[other].getAttribute("aria-checked");
      elements[name].events.click();
      assertSwitch(name, !enabled);
      assert.equal(elements[other].getAttribute("aria-checked"), otherState, "source switches remain independent");
      assert.equal(harness.state.apiCalls.length, callsBefore, "toggling waits for the Save button");
      await listeners.submit({preventDefault() {}});
      assert.equal(saved[name], !enabled, "save sends the switch state as a boolean");
      assertSwitch(name, !enabled);
      await harness.context.loadSettings();
      assertSwitch(name, !enabled);
    }
  }
  // The context window is sent as a number (blank means the 0 default) and rendered back from settings.
  elements.llm_context_window.value = "500000";
  await listeners.submit({preventDefault() {}});
  assert.equal(saved.llm_context_window, 500000);
  elements.llm_context_window.value = "";
  await listeners.submit({preventDefault() {}});
  assert.equal(saved.llm_context_window, 0);
  stored.llm_context_window = 256000; await harness.context.loadSettings();
  assert.equal(elements.llm_context_window.value, 256000);
  delete stored.llm_context_window;
  form.reset();
  assertSwitch("source_518_enabled", true);
  assertSwitch("source_1111_enabled", false);
}

async function testVapidSubjectPorts() {
  const harness = createHarness();
  const valid = [
    "https://203.0.113.10:8443/contact",
    "https://[2001:db8::1]:9443/contact",
    "https://example.test:1/contact",
    "https://example.test:65535/contact",
    "mailto:admin@example.test",
  ];
  for (const value of valid) assert.equal(harness.context.validVapidSubject(value), true, value);
  const invalid = [
    "https://example.test:0/contact",
    "https://example.test:65536/contact",
    "https://example.test:99999/contact",
    "https://user:password@example.test:8443/contact",
    "https://example.test:8443/contact#secret",
    "https://example.test:8443/contact\u0000",
    "mailto:user@example.test#fragment",
  ];
  for (const value of invalid) assert.equal(harness.context.validVapidSubject(value), false, value);
}

async function testRestoreGuardsAndSuccess() {
  const harness = createHarness();
  const file = {name: "backup.zip", size: 10};
  harness.restoreForm.elements.file.files = [file];

  harness.restoreForm.elements.confirm.checked = false;
  await assert.rejects(harness.context.submitRestore(), /請先勾選/);
  assert.equal(harness.state.fetches.length, 0, "unconfirmed restore must not POST");

  harness.restoreForm.elements.confirm.checked = true;
  harness.restoreForm.elements.file.files = [{name: "large.zip", size: 128 * 1024 * 1024 + 1}];
  await assert.rejects(harness.context.submitRestore(), /128 MiB/);
  assert.equal(harness.state.fetches.length, 0, "oversized restore must not POST");

  harness.restoreForm.elements.file.files = [file];
  harness.state.dialogQueue.push("cancel");
  setToken(harness, "cancel-token");
  await harness.context.submitRestore();
  assert.equal(harness.state.fetches.length, 0, "cancelled restore must not POST");

  harness.restoreForm.elements.confirm.checked = true;
  harness.restoreForm.elements.file.files = [file];
  harness.state.dialogQueue.push("confirm");
  harness.state.dialogHook = () => {
    setToken(harness, "new-session-token");
    harness.context.signedOut();
    harness.state.dialogHook = null;
  };
  await assert.rejects(harness.context.submitRestore(), /登入狀態或還原檔案已變更/);
  assert.equal(harness.state.fetches.length, 0, "session reset during confirmation must not POST");

  harness.restoreForm.elements.confirm.checked = true;
  harness.restoreForm.elements.file.files = [file];
  harness.state.dialogQueue.push("confirm");
  setToken(harness, "restore-token");
  harness.state.fetchImpl = async (pathValue, options) => {
    assert.equal(pathValue, "/api/admin/restore");
    assert.equal(options.method, "POST");
    assert.equal(options.headers["X-CSRF-Token"], "restore-token");
    assert.equal(options.body.form, harness.restoreForm);
    return response(200, {restored: true, pre_restore_backup_id: "before123", login_required: true, resubscribe_required: true});
  };
  await harness.context.submitRestore();
  assert.equal(harness.state.fetches.length, 1);
  assert.equal(harness.state.signedOut, 2, "restore success should sign out once in addition to reset simulation");
  assert.equal(harness.state.loginContext, 1);
  assert.equal(harness.restoreResult.hidden, false);
  assert.match(harness.restoreResult.textContent, /before123/);
}

async function testMultipartCsrf401AndLateResponse() {
  const harness = createHarness();
  setToken(harness, "csrf-token");
  let release;
  harness.state.fetchImpl = () => new Promise(resolve => { release = resolve; });
  const pending = harness.context.multipartApi("/api/admin/restore", {body: true});
  assert.equal(harness.state.fetches[0][1].headers["X-CSRF-Token"], "csrf-token");
  assert.equal("Content-Type" in harness.state.fetches[0][1].headers, false);
  setToken(harness, "changed-token");
  release(response(200, {ok: true}));
  await assert.rejects(pending, /登入狀態已變更/);

  setToken(harness, "csrf-token");
  harness.state.fetchImpl = async () => response(401, {error: {message: "session expired"}});
  await assert.rejects(harness.context.multipartApi("/api/admin/restore", {body: true}), error => error.status === 401);
  assert.ok(harness.state.windowEvents.includes("session-expired"));
}

async function testHtmlCopy() {
  assert.ok(html.includes("五狀態素材驗證與預覽"));
  assert.ok(html.includes("reduced-motion"));
  assert.ok(html.includes("備份含 API 秘密與素材，請妥善保存"));
  assert.ok(!html.includes("角色演出將接續實作"));
  assert.ok(!html.includes("建立時間未知"));
  assert.match(html, /<option value="8" selected>/);
  assert.match(html, /name="frame_count"[^>]*value="8"/);
  assert.ok(!html.includes("模板為 4 欄×5 列"));
  assert.match(html, /id="sharing-status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(html, /id="sharing-primary"/);
  assert.match(html, /id="sharing-action-link"/);
  assert.match(html, /id="sharing-refresh"/);
  assert.match(html, /id="sharing-disable"/);
  assert.match(html, /id="sharing-disable-confirm"/);
  assert.match(html, /id="ports-card" class="card stack"/);
  assert.match(html, /name="admin_port" type="number" min="1" max="65535"/);
  assert.match(html, /name="local_port" type="number" min="1" max="65535"/);
  assert.match(html, /name="public_port"/);
  assert.match(html, /保存後重新執行 start.cmd/);
  assert.match(html, /登入 Tailscale → 開啟外部連線 → 建立專屬連結。/);
  assert.match(html, /id="user-list"/);
  assert.match(html, /id="user-form"/);
  assert.match(html, /id="user-confirm"/);
  assert.match(html, /id="access-owner"/);
  assert.ok(!html.includes("reset-access") && !html.includes("access-confirm"));
}

async function testSpritePresetSelection() {
  const harness = createHarness();
  const preset = {selectedOptions: []};
  const description = {value: "原有角色"};
  harness.elements.set("#sprite-prompt-preset", preset);
  harness.elements.set("#sprite-prompt-character", description);
  harness.elements.set("#sprite-template", {elements: {columns: {value: "8"}}});
  harness.elements.set("#sprite-prompt-status", {});
  const list = {children: [], replaceChildren() { this.children = []; }, append(card) { this.children.push(card); }};
  harness.elements.set("#sprite-prompt-list", list);
  harness.context.action = (button, handler) => { button.events.click = handler; };
  function assertRenderedEightFrames(identity) {
    const card = list.children[0];
    const rendered = card.children[1].children[0].value;
    assert.equal(list.children.length, 1);
    assert.match(card.children[0].textContent, /8 欄×5 列/);
    assert.ok(rendered.includes(identity));
    assert.match(rendered, /8 欄×5 列，共 40 格/);
    assert.match(rendered, /512×320 px/);
    assert.match(rendered, /每列必須畫滿 8 格/);
    assert.doesNotMatch(rendered, /4 欄×5 列|共 20 格|256×320 px/);
  }
  const presets = [...html.matchAll(/<option value="(?:cat|rabbit|bear|fox)" data-description="([^"]+)"/g)];
  assert.equal(presets.length, 4);
  for (const [, identity] of presets) {
    preset.selectedOptions = [{dataset: {description: identity}}];
    harness.context.applySpritePreset();
    assert.equal(description.value, identity);
    assertRenderedEightFrames(identity);
  }
  description.value = "自行修改的小恐龍";
  preset.selectedOptions = [{dataset: {}}];
  harness.context.applySpritePreset();
  assert.equal(description.value, "自行修改的小恐龍", "custom selection must preserve the current description");
  assertRenderedEightFrames(description.value);
}

async function testSpritePromptsAndCopyFallback() {
  const harness = createHarness();
  const character = "  深藍色小貓，金色眼睛，戴星星圍巾。  ";
  const prompt = harness.context.spritePrompt(character);
  assert.ok(prompt.includes(character.trim()), "custom identity should appear in the complete prompt");
  assert.ok(!prompt.includes("橘色虎斑小貓"), "the example character must not override a custom identity");
  for (const state of ["idle", "thinking", "happy", "mail", "encourage"]) assert.ok(prompt.includes(state));
  assert.match(prompt, /8 欄×5 列，共 40 格/);
  assert.match(prompt, /64×64 px/);
  assert.match(prompt, /512×320 px/);
  for (const columns of [4,8,12]) {
    const variant = harness.context.spritePrompt(character, columns);
    assert.ok(variant.includes(`${columns} 欄×5 列，共 ${columns * 5} 格`));
    assert.ok(variant.includes(`${columns * 64}×320 px`));
    assert.ok(variant.includes(`每列必須畫滿 ${columns} 格`));
  }
  assert.match(prompt, /透明背景/);
  assert.match(prompt, /自然的動物體態/);
  assert.match(prompt, /連續動作的 8 個時間點/);
  assert.match(prompt, /嘴輕叼/);
  assert.match(prompt, /3～5 px/);
  assert.match(prompt, /身體重心（軀幹）都畫在該格水平正中央/);
  assert.match(prompt, /同一列 8 格不得朝左或右逐格漂移/);
  assert.ok(harness.context.spritePrompt(character, 12).includes("同一列 12 格不得朝左或右逐格漂移"));
  assert.doesNotMatch(prompt, /不要把腳底或輪廓鎖死/);
  assert.doesNotMatch(prompt, /一手托下巴|雙手捧|雙手舉高|放鬆站立/);
  for (const option of html.matchAll(/<option value="(?:cat|rabbit|bear|fox)" data-description="([^"]+)"/g)) {
    assert.doesNotMatch(option[1], /兩腳站立|外套|背心|吊帶褲/);
  }
  assert.ok(!prompt.includes("undefined"));
  assert.match(harness.context.spritePrompt(" \n "), /請依附上的角色參考圖/);
  const status = {textContent: ""}; harness.elements.set("#sprite-prompt-status", status);
  let copied, focused = false, selected = false;
  const textarea = {value: prompt, focus() { focused = true; }, select() { selected = true; }};
  harness.context.navigator = {clipboard: {async writeText(value) { copied = value; }}};
  await harness.context.copySpritePrompt(textarea, "角色模板");
  assert.equal(copied, textarea.value, "copy must contain the complete displayed prompt");
  assert.match(status.textContent, /已複製「角色模板」/);
  harness.context.navigator.clipboard.writeText = async () => { throw new Error("permission denied"); };
  await harness.context.copySpritePrompt(textarea, "思考");
  assert.ok(focused && selected);
  assert.match(status.textContent, /未能自動複製/);
  focused = selected = false; delete harness.context.navigator.clipboard;
  await harness.context.copySpritePrompt(textarea, "思考");
  assert.ok(focused && selected, "missing clipboard API should also permit manual copy");
  assert.equal(harness.state.fetches.length + harness.state.apiCalls.length, 0, "prompt and copy actions must remain local");
}

async function testSpriteAnimationPreview() {
  const harness = createHarness();
  const form = {elements: {confirm: {}, file: {files: [{size: 100}]}, fps: {value: "8", reportValidity: () => true}, columns: {value:"8"}, clean_edges:{checked:false}}};
  const preview = {children: [], replaceChildren(...items) { this.children = items; }};
  harness.elements.set("#sprite-template", form);
  harness.elements.set("button[type=submit]", {});
  harness.elements.set("#sprite-template-preview", preview);
  harness.elements.set("#sprite-template-status", {});
  const items = ["idle", "thinking", "happy", "mail", "encourage"].map(state => ({state, png_base64: "AA==", frame_width:64, frame_height:64, frame_count:8, fps:8}));
  harness.state.fetchImpl = async () => response(200, {items});
  setToken(harness, "stable-token");
  await harness.context.previewSpriteTemplate();
  assert.equal(preview.children.length, 5);
  assert.equal(preview.children[0].children[1].width, 512);
  assert.equal(harness.state.timers.size, 0, "previews await explicit playback");
  const box = preview.children[0].children[2];
  const [canvas, button] = box.children;
  const frame = () => harness.state.draws.filter(draw => draw.canvas === canvas).at(-1).args[1];
  button.events.click();
  harness.advance(1999); assert.equal(frame(), 0);
  harness.advance(1); assert.equal(frame(), 64, "preview shares the frontend idle rest");
  assert.equal(button.textContent, "暫停動畫");
  button.events.click();
  harness.advance(5000); assert.equal(frame(), 64, "pause preserves the displayed frame");
  button.events.click(); harness.state.media.change(true);
  assert.equal(harness.state.timers.size, 0);
  assert.equal(button.disabled, true);
  assert.equal(frame(), 0, "reduced motion displays the first frame");
  harness.state.media.change(false);
  assert.equal(button.disabled, false);
  assert.equal(harness.state.timers.size, 0, "motion preference changes do not autoplay previews");
  form.elements.columns.value = "12";
  form.elements.confirm.checked = true;
  await assert.rejects(harness.context.submitSpriteTemplate(), /請先預覽/);
  form.elements.columns.value = "8";
  form.elements.clean_edges.checked = true;
  await assert.rejects(harness.context.submitSpriteTemplate(), /請先預覽/);
  form.elements.clean_edges.checked = false;
  button.events.click();
  harness.context.clearSpriteTemplatePreview();
  assert.equal(preview.children.length, 0);
  assert.equal(harness.state.timers.size, 0);
  assert.equal(harness.state.media.listeners.size, 0);
  assert.ok(harness.state.bitmaps.every(bitmap => bitmap.closed));

  let resolve;
  harness.state.fetchImpl = () => new Promise(done => { resolve = done; });
  const pending = harness.context.previewSpriteTemplate();
  harness.context.clearSpriteTemplatePreview();
  resolve(response(200, {items}));
  await assert.rejects(pending, /已變更/);
  assert.equal(preview.children.length, 0, "cleared previews cannot reappear from late responses");
  assert.equal(harness.state.media.listeners.size, 0);
  assert.ok(harness.state.bitmaps.every(bitmap => bitmap.closed));

  const saved = {children: [], replaceChildren(...items) { this.children = items; }, append(item) { this.children.push(item); }};
  harness.elements.set("#sprite-list", saved);
  harness.state.api = async () => ({items: [{state: "happy", frame_width: 32, frame_height: 48, frame_count: 3, fps: 10}]});
  await harness.context.loadSprites();
  const [savedCanvas, savedButton] = saved.children[0].children[2].children;
  savedButton.events.click(); harness.advance(100);
  assert.equal(harness.state.draws.filter(draw => draw.canvas === savedCanvas).at(-1).args[1], 32);
  await harness.context.loadSprites();
  assert.equal(harness.state.timers.size, 0, "refresh destroys prior saved previews");
  harness.context.clearSpriteAnimations(vm.runInContext("spriteListAnimations", harness.context));
  assert.equal(harness.state.media.listeners.size, 0);
}

(async () => {
  await testAccessLinkOriginAndLocalWarning();
  await testAccessLinkCopyAndClear();
  await testAccountListShowsOnlyApplicableActions();
  await testAccountActionsConfirmBeforeAnythingIsSent();
  await testAccountFailuresAreShownNotThrown();
  await testCreatingAnAccountShowsItsLinkOnce();
  await testSharingStateActionsAndProgressiveInfo();
  await testSharingActionsInstallationPollAndLateReply();
  await testSharingEnableConsentAndCheckContract();
  await testPortSettingsSaveAndPendingRestart();
  await testModelConnectionHttpErrorDisplay();
  await testRunNotificationSummary();
  await testSpriteTemplatePreviewRequiredAndSizeGuard();
  await testSingleSearchConnectionStatus();
  await testSourceSwitchesLoadToggleAndSave();
  await testBackupHrefAndCreatedAt();
  await testVapidSubjectPorts();
  await testRestoreGuardsAndSuccess();
  await testMultipartCsrf401AndLateResponse();
  await testHtmlCopy();
  await testSpritePresetSelection();
  await testSpritePromptsAndCopyFallback();
  await testSpriteAnimationPreview();
  console.log("M3 admin VM contract checks passed");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
