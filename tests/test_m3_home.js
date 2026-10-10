"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "app", "static", "home.js"), "utf8");
const homeHtml = fs.readFileSync(path.join(__dirname, "..", "app", "static", "index.html"), "utf8");

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = Object.create(null);
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.value = "";
    this._text = "";
    this.showCount = 0;
    this.destroyed = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.classList = {
      values: new Set(),
      add: (...names) => names.forEach(name => this.classList.values.add(name)),
      remove: (...names) => names.forEach(name => this.classList.values.delete(name)),
      toggle: (name, force) => { const enabled = force === undefined ? !this.classList.values.has(name) : Boolean(force); if (enabled) this.classList.values.add(name); else this.classList.values.delete(name); return enabled; },
      contains: name => this.classList.values.has(name),
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  set value(value) { this._value = value; if (this.type === "file" && value === "") this.files = []; }
  get value() { return this._value; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  append(...items) { for (const item of items.filter(Boolean)) { if (item.tagName === "FRAGMENT") this.append(...item.children); else { item.remove(); item.parentElement = this; this.children.push(item); } } }
  insertBefore(item, reference) { const index = this.children.indexOf(reference); if (index < 0) return this.append(item); item.remove(); const target = this.children.indexOf(reference); item.parentElement = this; this.children.splice(target, 0, item); return item; }
  prepend(...items) { const holder = new FakeElement(); holder.append(...items); this.children.unshift(...holder.children); for (const item of holder.children) item.parentElement = this; }
  replaceChildren(...items) { for (const child of this.children) child.parentElement = null; this._text = ""; this.children = []; this.append(...items); }
  replaceWith(item) { const parent = this.parentElement; if (!parent) return; const index = parent.children.indexOf(this); if (index >= 0) { item.parentElement = parent; parent.children[index] = item; this.parentElement = null; } }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async dispatch(type, detail = {}) {
    const event = {type, currentTarget: this, ...detail};
    await Promise.all((this.listeners[type] || []).map(listener => listener(event)));
    if (detail && "defaultPrevented" in detail) detail.defaultPrevented = event.defaultPrevented;
    return event;
  }
  click() { return this.disabled ? Promise.resolve([]) : this.dispatch("click"); }
  reset() { this.value = ""; for (const child of this.children) child.reset(); }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  scrollIntoView() { this.scrolled = true; }
  getAttribute(name) { return this[name]; }
  removeAttribute(name) { delete this[name]; }
  dispatchEvent(event) { for (const listener of this.listeners[event.type] || []) listener(event); }
  closest(selector) { return selector === ".job-card" && this.className === "job-card item" ? this : this.parentElement?.closest(selector); }
  remove() { if (!this.parentElement) return; const parent = this.parentElement; parent.children.splice(parent.children.indexOf(this), 1); this.parentElement = null; }
  contains(element) { return element === this || this.children.some(child => child.contains(element)); }
  get nextElementSibling() { return this.parentElement?.children[this.parentElement.children.indexOf(this) + 1]; }
  get previousElementSibling() { return this.parentElement?.children[this.parentElement.children.indexOf(this) - 1]; }
  showModal() { this.open = true; this.showCount++; }
  close() {
    this.open = false;
    for (const listener of this.listeners.close || []) listener({type: "close", currentTarget: this});
  }
  setAttribute(name, value) { this[name] = String(value); }
  querySelector(selector) {
    if (selector === "[data-favorite]") return find(this, item => item.dataset.favorite);
    if (selector === "[data-conversation-open]") return find(this, item => item.dataset.conversationOpen !== undefined);
    if (selector === "[data-conversation-delete]") return find(this, item => item.dataset.conversationDelete !== undefined);
    if (selector === ".conversation-title") return find(this, item => item.className === "conversation-title");
    if ([".card-feedback", ".proposal-feedback"].includes(selector)) return find(this, item => (item.className || "").split(" ").includes(selector.slice(1)));
    if (selector === ".chat-text") return find(this, item => item.className === "chat-text");
    if (selector === ".chat-bubble") return find(this, item => item.className === "chat-bubble");
    if (selector === ".chat-message-meta") return find(this, item => item.className === "chat-message-meta");
    if (selector === ".chat-message-actions") return find(this, item => item.className === "chat-message-actions");
    if (selector === ".chat-time") return find(this, item => item.className === "chat-time");
    if (selector === "input") return find(this, item => item.tagName === "INPUT");
    if (selector === "button[type=submit]") return find(this, item => item.tagName === "BUTTON" && item.type === "submit");
    return null;
  }
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) {
    const result = find(child, predicate);
    if (result) return result;
  }
  return null;
}
function all(root, predicate) {
  return [root, ...root.children.flatMap(child => all(child, predicate))].filter(predicate);
}

class Storage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(String(key), String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class FakeClock {
  constructor() { this.nextId = 1; this.timers = new Map(); this.scheduled = []; }
  setTimeout = (callback, delay) => { const id = this.nextId++; this.timers.set(id, {callback, delay}); this.scheduled.push(delay); return id; };
  clearTimeout = id => { this.timers.delete(id); };
  async advance(delay) {
    const due = [...this.timers.entries()].filter(([, timer]) => timer.delay <= delay);
    for (const [id, timer] of due) {
      this.timers.delete(id);
      await timer.callback();
    }
  }
}

class FakeDataTransfer {
  constructor() {
    this._files = [];
    this.items = {add: file => this._files.push(file)};
    this.types = ["Files"];
    this.dropEffect = "";
  }
  get files() { return this._files; }
}

function keyForTest() {
  const bytes = Uint8Array.from({length: 65}, (_, index) => index + 1);
  return {bytes, text: Buffer.from(bytes).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")};
}

 function createContext({storage = new Storage(), push = false, permission = "default", existing = null, pendingPost = false,
  pendingUnsubscribe = false,
  recentRun = {id: "run-1", status: "success", new_count: 2, initial_count: 0}, pollRun = null, intersection = false, panelWidth = 1600} = {}) {
  const elements = new Map();
  const ids = ["review-dialog", "user-panel", "logout", "access-needed", "preference-list", "proposal-list",
    "favorite-list", "criteria-fields", "editor-title", "cancel-edit", "chat-list", "daily-list", "daily-sources", "preferences-status", "message-edit-banner", "message-edit-status", "cancel-message-edit",
    "daily-date", "daily-status", "chat-form", "chat-status", "cancel-chat", "refresh-messages", "more-messages", "preferences-feedback", "daily-sources",
    "review-result", "more-favorites", "more-daily", "refresh-daily", "criteria-form", "message", "enable-push",
    "disable-push", "push-status", "push-summary", "companion", "encourage-dialog", "encourage-companion", "encourage-text", "theme", "daily",
    "header-companion", "status-companion", "quick-options", "toggle-quick-options", "chat-count", "resume-file", "resume-selection", "resume-name", "remove-resume", "chat-transcript", "latest-message", "chat", "chat-drop-overlay",
    "new-conversation", "open-conversation-history", "active-conversation-title", "conversation-history-dialog", "conversation-search-form", "conversation-query",
    "conversation-history-status", "conversation-list", "more-conversations", "close-conversation-history",
    "rename-conversation-dialog", "rename-conversation-form", "rename-conversation-input", "rename-conversation-status", "cancel-rename-conversation", "save-conversation-name",
    "delete-conversation-dialog", "delete-conversation-name", "delete-conversation-status", "cancel-delete-conversation", "confirm-delete-conversation",
    "search-results-panel", "search-results-title", "search-results-summary", "search-results-counts", "search-results-controls", "search-results-details", "search-results-actions", "search-results-list",
    "show-chat-view", "show-results-view", "results-count-badge", "preferences", "favorites",
    "open-result-options", "result-options-dialog", "close-result-options", "chat-menu", "chat-menu-toggle", "chat-menu-actions", "panel-resizer"];
  for (const id of ids) {
    const element = new FakeElement(["chat-form", "criteria-form", "conversation-search-form", "rename-conversation-form"].includes(id) ? "form" : "div");
    element.id = id;
    const classes = new Set();
    element.classList = {
      add(...names) { names.forEach(name => classes.add(name)); },
      remove(...names) { names.forEach(name => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) { const add = force === undefined ? !classes.has(name) : Boolean(force); if (add) classes.add(name); else classes.delete(name); return add; },
    };
    elements.set(id, element);
  }
  const input = new FakeElement("input"); input.name = "text"; input.value = "";
  const submit = new FakeElement("button"); submit.type = "submit";
  elements.get("chat-form").append(input, submit);
  const fileInput = elements.get("resume-file"); fileInput.type = "file"; fileInput.files = [];
  elements.get("chat-form").append(fileInput);
  elements.get("chat").append(elements.get("chat-drop-overlay"));
  // Splitter fakes: the chat width follows --chat-w (px) once set; otherwise 440px stands in for the CSS default.
  const splitPanel = elements.get("user-panel"); splitPanel.clientWidth = panelWidth;
  splitPanel.style.setProperty = (name, value) => { splitPanel.style[name] = String(value); };
  splitPanel.style.removeProperty = name => { delete splitPanel.style[name]; };
  elements.get("chat").getBoundingClientRect = () => ({width: splitPanel.style["--chat-w"] ? parseFloat(splitPanel.style["--chat-w"]) : 440, left: 0});
  elements.get("panel-resizer").setPointerCapture = () => {};
  const quickCard = new FakeElement("button"); quickCard.dataset.chatDraft = "請找生態調查員的工作。";
  elements.get("quick-options").hidden = true;
  elements.get("daily-date").value = "2026-10-05";
  elements.get("daily").hidden = true;
  elements.get("search-results-panel").hidden = true;
  const navButtons = ["chat", "daily", "preferences", "favorites"].map(page => { const button = new FakeElement("button"); button.dataset.page = page; button["aria-pressed"] = String(page === "chat"); return button; });

  const documentListeners = Object.create(null);
  const document = {
    documentElement: {dataset: {}},
    addEventListener(type, listener) { (documentListeners[type] ||= []).push(listener); },
    createElement: tag => new FakeElement(tag),
    createElementNS: (_namespace, tag) => new FakeElement(tag),
    createDocumentFragment: () => new FakeElement("fragment"),
    querySelector(selector) {
      if (selector.startsWith("#")) {
        const simple = selector.slice(1);
        if (simple.includes(" ")) {
          if (simple === "criteria-fields input") return elements.get("criteria-fields").querySelector("input");
          if (simple === "chat-form [name=text]") return input;
          if (simple === "chat-form button[type=submit]") return submit;
        }
        return elements.get(simple) || null;
      }
      if (selector === "[name=text]") return input;
      return null;
    },
    querySelectorAll: selector => selector === "[data-chat-draft]" ? [quickCard] : selector === "[data-page]" ? navButtons : selector === ".job-card" ? [...elements.values()].flatMap(root => all(root, item => item.className === "job-card item")) : [],
  };
  const listeners = Object.create(null);
  const clock = new FakeClock();
  const key = keyForTest();
  const state = {
    elements, navButtons, input, submit, clock, documentListeners, apiCalls: [], fetchCalls: [], frames: new Map(), resizeObservers: [], permissionCalls: 0, getSubscriptionCalls: 0, subscribeCalls: [],
    deleteCalls: [], play: [], popupPlay: [], thinking: [], reset: 0, destroyed: 0, controllers: [],
    pendingPost, resolvePost: null, pendingUnsubscribe, resolveUnsubscribe: null, portraits: [], quickCard, fileInput,
    conversations: [{id: "conversation-1", title: "合成對話", created_at: "2026-10-05T00:00:00Z", updated_at: "2026-10-05T00:00:00Z", message_count: 0}],
    conversationHistories: new Map(), conversationText: new Map(), conversationRequests: [], messageRequests: [], chatPayloads: [], history: [], chatSnapshots: new Map(), chatSnapshotRequests: [], chatCancelRequests: [],
    bootstrap: {
      csrf_token: "csrf-1", account: {username: "alice"}, local_date: "2026-10-05", quotes: ["<私人語錄>"], sprites: [], recent_run: recentRun,
      push_configured: push, push_public_key: push ? key.text : null, push_subscription_id: null,
    },
  };
  const sub = existing || null;
  const pushManager = {
    async getSubscription() { state.getSubscriptionCalls++; return state.subscription || sub; },
    async subscribe(options) {
      state.subscribeCalls.push(options);
      const newSub = createSubscription(key.bytes, state);
      state.subscription = newSub;
      return newSub;
    },
  };
  state.subscription = sub;
  const registration = {pushManager};
  const navigator = {
    userAgent: "Mozilla/5.0",
    standalone: false,
    serviceWorker: {ready: Promise.resolve(registration), register: async () => registration},
  };
  const notification = {
    permission,
    async requestPermission() { state.permissionCalls++; notification.permission = "granted"; return "granted"; },
  };
  const companion = {
    renderStatic(element, sprites, portraitState, size) { state.portraits.push({element, sprites, portraitState, size}); },
    create(element) {
      const popup = element === elements.get("encourage-companion");
      const controller = {
        setThinking(value) { state.thinking.push(Boolean(value)); },
        play(value) { (popup ? state.popupPlay : state.play).push(value); },
        reset() { state.reset++; },
        destroy() { state.destroyed++; },
      };
      state.controllers.push({controller, popup});
      state.controller = controller;
      return controller;
    },
  };
  const intersectionState = {observers: []};
  const IntersectionObserver = intersection ? class {
    constructor(callback) { this.callback = callback; this.targets = []; intersectionState.observers.push(this); }
    observe(target) { this.targets.push(target); }
    disconnect() { this.targets = []; }
    emit(targets = this.targets) { this.callback(targets.map(target => ({target, isIntersecting: true}))); }
  } : undefined;
  const window = {
    isSecureContext: true, PushManager: function PushManager() {}, Notification: notification, localStorage: storage,
    sessionStorage: storage, IntersectionObserver, ResizeObserver: class { constructor(callback) { this.callback = callback; state.resizeObservers.push(this); } observe() { this.callback([]); } },
    CompanionSprite: companion, matchMedia: () => ({matches: false}), navigator, location: {search: ""},
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
    dispatchEvent(event) { for (const listener of listeners[event.type] || []) listener(event); },
    atob(value) { return Buffer.from(value, "base64").toString("binary"); },
    reload() {},
  };
  const context = {
    window, document, navigator, location: window.location, ResizeObserver: window.ResizeObserver, localStorage: storage, URLSearchParams, URL, Promise, Date,
    console, Event, AbortController, FormData, DataTransfer: FakeDataTransfer, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    crypto: {randomUUID: () => "request-1"}, csrfToken: "", regionCatalog: [],
    requestAnimationFrame(callback) { const id = (state.nextFrameId = (state.nextFrameId || 0) + 1); state.frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { state.frames.delete(id); },
    $: (selector, parent = document) => parent.querySelector(selector),
    node(tag, text, className) { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element; },
    criteriaFields: () => { const fields = document.createElement("div"); fields.append(document.createElement("input")); return fields; },
    readCriteria: () => ({keyword: "x", area_codes: [], min_monthly_salary: null}),
    describe: () => "條件",
    message(text) { state.message = text; },
    action(button, handler) { button.addEventListener("click", async event => { button.disabled = true; try { await handler(event); } finally { button.disabled = false; } }); },
    formAction(form, handler) { form.addEventListener("submit", async event => { event.preventDefault(); await handler(event); }); },
    ChatSSE: {read: async (_reader, handler) => {
      await handler("tool_status", {name: "reply", phase: "started"});
      await handler("tool_status", {name: "search_jobs", id: "tool-1", phase: "started"});
      await handler("text_delta", {text: "回覆"});
      await handler("tool_status", {name: "search_jobs", id: "tool-1", phase: "completed"});
      await handler("done", {request_id: state.chatRequestId, status: "completed"});
    }},
  };
  context.fetch = async (path, options = {}) => {
    state.fetchCalls.push({path, options});
    if (path === "/api/chat") {
      const payload = options.body instanceof FormData
        ? {text: options.body.get("text"), request_id: options.body.get("request_id"), conversation_id: options.body.get("conversation_id"), edit_message_id: options.body.get("edit_message_id"), edit_request_id: options.body.get("edit_request_id"), resume: options.body.get("resume")}
        : JSON.parse(options.body);
      state.chatRequestId = payload.request_id; state.chatPayloads.push(payload);
      return {ok: true, status: 200, headers: {get: () => "text/event-stream"}, body: {getReader: () => ({})}};
    }
    return {ok: true, status: 204, headers: {get: () => ""}, json: async () => null};
  };
  context.api = async (path, options = {}) => {
    state.apiCalls.push({path, options});
    if (path === "/api/bootstrap") return pollRun ? (state.bootstraps ||= [state.bootstrap, {...state.bootstrap, recent_run: pollRun}]).shift() || state.bootstrap : state.bootstrap;
    if (path === "/api/areas") return {items: []};
    if (path === "/api/preferences") return {paused: false, user_has_chosen: true, effective: [], proposals: []};
    if (path.startsWith("/api/conversations")) {
      const url = new URL(path, "https://fixture.invalid");
      const id = url.pathname.split("/").at(-1);
      const method = options.method || "GET";
      state.conversationRequests.push({path, options});
      if (method === "GET") {
        const query = (url.searchParams.get("q") || "").toLocaleLowerCase();
        const matched = state.conversations.filter(item => !query || `${item.title} ${state.conversationText.get(String(item.id)) || ""}`.toLocaleLowerCase().includes(query));
        const offset = Number(url.searchParams.get("cursor")) || 0;
        const limit = Number(url.searchParams.get("limit")) || 20;
        return {items: matched.slice(offset, offset + limit), next_cursor: offset + limit < matched.length ? String(offset + limit) : null};
      }
      if (method === "POST") {
        const payload = JSON.parse(options.body || "{}");
        const conversation = {id: `conversation-${state.conversations.length + 1}`, title: payload.title || "新對話", created_at: new Date().toISOString(), updated_at: new Date().toISOString(), message_count: 0};
        state.conversations.unshift(conversation); return conversation;
      }
      const conversation = state.conversations.find(item => String(item.id) === String(id));
      if (method === "PATCH") {
        if (!conversation) throw new Error("找不到對話");
        Object.assign(conversation, JSON.parse(options.body || "{}")); return {...conversation};
      }
      if (method === "DELETE") {
        if (state.conversationDeleteError) throw state.conversationDeleteError;
        state.conversations = state.conversations.filter(item => String(item.id) !== String(id));
        state.conversationHistories.delete(String(id)); state.conversationText.delete(String(id)); return null;
      }
    }
    if (path.startsWith("/api/messages")) {
      const url = new URL(path, "https://fixture.invalid");
      const id = url.searchParams.get("conversation_id");
      state.messageRequests.push({id, cursor: url.searchParams.get("cursor"), path});
      return {items: state.conversationHistories.get(String(id)) || state.history || [], next_cursor: null};
    }
    if (path.startsWith("/api/chat/") && !path.endsWith("/cancel")) {
      const id = decodeURIComponent(path.split("/").at(-1));
      state.chatSnapshotRequests.push(id);
      if (state.chatSnapshots.has(id)) return state.chatSnapshots.get(id);
      const error = new Error("找不到回覆"); error.status = 404; throw error;
    }
    if (path.startsWith("/api/chat/") && path.endsWith("/cancel")) {
      state.chatCancelRequests.push(path);
      const id = decodeURIComponent(path.split("/").at(-2));
      const snapshot = state.chatSnapshots.get(id);
      if (snapshot) {
        const items = snapshot.items.map(item => item.role === "assistant" ? {...item, content: {...item.content, status: "interrupted"}} : item);
        state.chatSnapshots.set(id, {...snapshot, items});
      }
      return {status: "interrupted"};
    }
    if (path.startsWith("/api/favorites")) return {items: [], next_cursor: null};
    if (path === "/api/push-subscriptions" && options.method === "POST") {
      if (state.pendingPost) return await new Promise(resolve => { state.resolvePost = () => resolve({id: "server-sub", enabled: true}); });
      state.postPayload = JSON.parse(options.body);
      return {id: "server-sub", enabled: true};
    }
    if (path.startsWith("/api/push-subscriptions/") && options.method === "DELETE") { state.deleteCalls.push(path); return null; }
    return {};
  };
  vm.createContext(context);
  const common = fs.readFileSync(path.join(__dirname, "..", "app", "static", "common.js"), "utf8");
  vm.runInContext(common.match(/^function formatTaipeiDateTime[\s\S]*?^}/m)[0], context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "app", "static", "cards.js"), "utf8"), context, {filename: "cards.js"});
  context.JobCards = window.JobCards;
  vm.runInContext(source, context, {filename: "home.js"});
  state.intersection = intersectionState;
  return {context, state, elements, window, storage, key, registration};
}

function createSubscription(bytes, state) {
  return {
    options: {applicationServerKey: bytes.buffer.slice(0)},
    toJSON: () => ({endpoint: "https://push.example/sub", keys: {p256dh: "p", auth: "a"}}),
    async unsubscribe() {
      state.unsubscribed = true;
      if (state.pendingUnsubscribe) return await new Promise(resolve => { state.resolveUnsubscribe = () => resolve(true); });
      return true;
    },
  };
}

async function settle() {
  for (let index = 0; index < 8; index++) {
    await Promise.resolve();
    await new Promise(resolve => setImmediate(resolve));
  }
}
async function finishWithClock(home, promise, maxSteps = 20) {
  let finished = false;
  promise.then(() => { finished = true; }, () => { finished = true; });
  for (let step = 0; step < maxSteps && !finished; step++) {
    await settle();
    if (finished) break;
    const delays = [...home.state.clock.timers.values()].map(timer => timer.delay);
    if (!delays.length) break;
    await home.state.clock.advance(Math.min(...delays));
  }
  await settle();
  return promise;
}

async function testCompanionLifecycle() {
  const storage = new Storage();
  const home = createContext({storage, recentRun: {id: "run-1", status: "success", new_count: 2, initial_count: 0}, pollRun: {id: "run-1", status: "success", new_count: 2, initial_count: 0}});
  await settle();
  assert.equal(home.elements.get("logout").textContent, "登出（alice）", "the signed-in account name is shown");
  assert.equal(home.elements.get("encourage-dialog").showCount, 1);
  assert.equal(home.elements.get("encourage-text").textContent, "<私人語錄>");
  assert.deepEqual(home.state.play, ["encourage", "mail"], "encourage precedes the recent run mail");
  assert.deepEqual(home.state.popupPlay, ["encourage"], "popup has its own short-lived encourage controller");
  home.context.maybePlayMail({id: "baseline-1", trigger: "baseline", new_count: 4});
  home.context.maybePlayMail({id: "missing-count", status: "success"});
  home.context.maybePlayMail({id: "failed-run", status: "failed", new_count: 4});
  assert.deepEqual(home.state.play, ["encourage", "mail"], "baseline, missing count and failed runs never play mail");
  assert.equal(home.state.permissionCalls, 0, "bootstrap never asks for notification permission");

  home.context.updateToolThinking({name: "search_jobs", phase: "started"});
  const destroyedBeforeClose = home.state.destroyed;
  home.elements.get("encourage-dialog").close();
  assert.equal(home.state.destroyed, destroyedBeforeClose + 1, "closing the dialog destroys the popup controller");
  assert.equal(home.state.thinking.at(-1), true, "closing encouragement reapplies active tool thinking");
  home.context.updateToolThinking({name: "search_jobs", phase: "completed"});

  await home.state.clock.advance(30000); await settle();
  assert.deepEqual(home.state.play, ["encourage", "mail"], "polling the same run does not replay mail or encouragement");
  assert.equal(home.elements.get("encourage-dialog").showCount, 1);

  home.state.input.value = "找工作";
  await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.deepEqual(home.state.thinking.slice(-2), [true, false], "only tool started/completed drives thinking");
  assert.ok(home.state.reset > 0, "chat completion clears transient sprite timers");

  const favoriteJob = {id: "job-1", is_favorite: false};
  const favoriteCard = home.context.jobCard(favoriteJob); home.elements.get("favorite-list").append(favoriteCard);
  await home.context.toggleFavorite(favoriteJob, all(favoriteCard, item => item.dataset.favorite)[0]);
  assert.equal(home.state.play.at(-1), "happy", "successful favorite plays happy");
  const favoriteNote = all(favoriteCard, item => item.className === "card-feedback")[0];
  const noteTimers = delay => [...home.state.clock.timers.entries()].filter(([, timer]) => timer.delay === delay);
  const fireNoteTimer = delay => { const [id, timer] = noteTimers(delay)[0]; home.state.clock.clearTimeout(id); timer.callback(); };
  assert.equal(favoriteNote.textContent, "已收藏。");
  assert.equal(noteTimers(2500).length, 1);
  fireNoteTimer(2500);
  assert.equal(favoriteNote.textContent, "", "success feedback leaves by itself instead of keeping card space");
  const healthyApi = home.context.api;
  await home.context.toggleFavorite(favoriteJob, all(favoriteCard, item => item.dataset.favorite)[0]);
  assert.equal(favoriteNote.textContent, "已取消收藏。");
  home.context.api = async () => { throw new Error("合成失敗"); };
  await home.context.toggleFavorite(favoriteJob, all(favoriteCard, item => item.dataset.favorite)[0]);
  assert.match(favoriteNote.textContent, /收藏未能更新：合成失敗/);
  assert.equal(noteTimers(2500).length, 0, "a new message replaces the previous message's timer");
  assert.equal(noteTimers(6000).length, 1, "errors stay longer than confirmations");
  fireNoteTimer(6000);
  assert.equal(favoriteNote.textContent, "");
  home.context.api = healthyApi;

  const second = createContext({storage, recentRun: {id: "run-2", status: "success", new_count: 0, initial_count: 1}});
  await settle();
  assert.equal(second.elements.get("encourage-dialog").showCount, 0, "same device shows encouragement once per local date");

  home.context.clearCompanion();
  home.context.setupCompanion({local_date: "2026-10-06", quotes: ["隔日語錄"], sprites: []});
  home.context.maybeShowEncouragement({local_date: "2026-10-06", quotes: ["隔日語錄"], sprites: []});
  assert.equal(home.elements.get("encourage-dialog").hidden, false, "a later bootstrap unhides the dialog after cleanup");
}

async function testPushPermissionRebindDisableAndLateLogout() {
  const home = createContext({push: true, permission: "default"});
  await settle();
  assert.equal(home.state.permissionCalls, 0);
  assert.equal(home.state.subscribeCalls.length, 0);
  await home.elements.get("enable-push").click();
  assert.equal(home.state.permissionCalls, 1, "permission is requested only after explicit click");
  assert.equal(home.state.subscribeCalls.length, 1);
  assert.deepEqual(home.state.postPayload, {endpoint: "https://push.example/sub", keys: {p256dh: "p", auth: "a"}});
  await home.elements.get("disable-push").click();
  assert.equal(home.state.unsubscribed, true);
  assert.equal(home.state.deleteCalls.length, 1);

  const lateDisable = createContext({push: true, permission: "default", pendingUnsubscribe: true});
  await settle();
  await lateDisable.elements.get("enable-push").click();
  const disabling = lateDisable.elements.get("disable-push").click();
  await settle();
  assert.equal(typeof lateDisable.state.resolveUnsubscribe, "function");
  lateDisable.context.signedOut();
  lateDisable.state.resolveUnsubscribe();
  await disabling; await settle();
  assert.equal(lateDisable.state.deleteCalls.length, 0, "logout during unsubscribe prevents a late DELETE");

  const key = keyForTest();
  const existingState = {};
  const existing = createSubscription(key.bytes, existingState);
  const rebound = createContext({push: true, permission: "granted", existing});
  await settle();
  assert.equal(rebound.state.permissionCalls, 0, "existing granted permission is rebound without requesting again");
  assert.deepEqual(rebound.state.postPayload, {endpoint: "https://push.example/sub", keys: {p256dh: "p", auth: "a"}});

  const late = createContext({push: true, permission: "default", pendingPost: true});
  await settle();
  const click = late.elements.get("enable-push").click();
  await settle();
  late.context.signedOut();
  late.state.resolvePost();
  await click; await settle();
  assert.equal(late.state.destroyed, 2, "logout destroys both companion controllers");
  assert.equal(late.elements.get("push-status").textContent, "", "late push response cannot update signed-out UI");
}

async function testChatDeliveryFeedback() {
  const reading = createContext(); await settle();
  reading.state.input.value = "請查看我的公開作品集";
  reading.context.ChatSSE.read = async (_reader, handler) => {
    await handler("tool_status", {name: "read_web_page", phase: "started"});
    assert.match(reading.elements.get("chat-status").textContent, /正在讀取公開網頁/);
    await handler("tool_status", {name: "read_web_page", phase: "completed"});
    await handler("done", {request_id: reading.state.chatRequestId, status: "completed"});
  };
  await reading.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(reading.elements.get("chat-status").dataset.state, "complete");
  for (const [code, statusCode] of [["test_required", 503], ["not_configured", 503], ["invalid_request", 501]]) {
    const home = createContext(); await settle();
    home.state.input.value = "想找新的工作";
    let calls = 0;
    home.context.fetch = async () => { calls++; return {ok: false, status: statusCode, json: async () => ({error: {code, message: "service unavailable"}})}; };
    await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
    const status = home.elements.get("chat-status");
    assert.equal(calls, 1, "definite configuration, validation and 501 errors are not retried");
    assert.equal(status.dataset.state, "error");
    assert.equal(status["aria-live"], "assertive");
    assert.match(status.textContent, /訊息尚未送出/);
    assert.equal(home.state.input.value, "想找新的工作");
    assert.equal(home.state.input.readOnly, false);
    assert.equal(home.state.submit.disabled, false);
    assert.equal(home.elements.get("chat-list").children.length, 0, "a rejected send must not create successful-looking messages");
  }

  const duplicate = createContext(); await settle();
  duplicate.state.input.value = "已可能送達的訊息";
  duplicate.state.chatSnapshots.set("request-1", {request_id: "request-1", conversation_id: "conversation-1", items: [
    {id: 51, request_id: "request-1", role: "user", content: {text: "已可能送達的訊息", status: "completed", attachments: []}, created_at: "2026-10-08T00:00:00Z"},
    {id: 52, request_id: "request-1", role: "assistant", content: {text: "伺服器已保存", status: "completed", events: []}, created_at: "2026-10-08T00:00:01Z"},
  ]});
  let duplicateCalls = 0;
  duplicate.context.fetch = async (_path, options) => {
    duplicateCalls++;
    const payload = JSON.parse(options.body);
    duplicate.state.chatPayloads.push(payload); duplicate.state.chatRequestId = payload.request_id;
    return {ok: false, status: 409, json: async () => ({error: {code: "duplicate_request", message: "duplicate"}})};
  };
  await duplicate.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(duplicateCalls, 1, "duplicate id is recovered from its snapshot rather than posted again");
  assert.equal(duplicate.state.chatSnapshotRequests.length, 1);
  assert.equal(duplicate.elements.get("chat-list").children.length, 2);
  assert.match(duplicate.elements.get("chat-list").textContent, /伺服器已保存/);
  assert.equal(duplicate.state.input.value, "");

  const home = createContext(); await settle();
  home.state.input.value = "幫我看看職缺";
  const successfulFetch = home.context.fetch;
  const attempts = [];
  home.context.fetch = async (path, options) => {
    attempts.push(JSON.parse(options.body).request_id);
    if (attempts.length <= 2) return {ok: false, status: 503, json: async () => ({error: {code: "temporary", message: "temporarily unavailable"}})};
    return successfulFetch(path, options);
  };
  const sending = home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  await finishWithClock(home, sending);
  assert.equal(attempts.length, 3);
  assert.equal(new Set(attempts).size, 1, "pre-accept transient retry reuses one idempotency key");
  await sending;
  assert.match(home.elements.get("chat-status").textContent, /回覆已完成/);
  assert.equal(home.elements.get("chat-status").dataset.state, "complete");
  assert.equal(home.state.submit.textContent, "送出");
  assert.equal(home.state.input.readOnly, false);

  const exhausted = createContext(); await settle();
  exhausted.state.input.value = "保留這段文字";
  const retryIds = [];
  exhausted.context.fetch = async (_path, options) => { retryIds.push(JSON.parse(options.body).request_id); throw new TypeError("Failed to fetch"); };
  const exhaustSending = exhausted.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  await finishWithClock(exhausted, exhaustSending);
  assert.equal(retryIds.length, 6, "five 5-second retries follow the first request");
  assert.equal(new Set(retryIds).size, 1);
  assert.deepEqual(exhausted.state.clock.scheduled.filter(delay => delay === 5000).length, 10, "five POST retries and five spaced snapshot retries use 5 seconds");
  assert.equal(exhausted.state.input.value, "保留這段文字");
  assert.equal(exhausted.state.submit.disabled, true, "uncertain delivery remains recoverable and cancellable");
  assert.equal(exhausted.elements.get("cancel-chat").hidden, false);
  assert.equal(exhausted.state.chatSnapshotRequests.length, 6, "an immediate snapshot check is followed by five retries");

  const cancelBackoff = createContext(); await settle();
  cancelBackoff.state.input.value = "等待網路重試";
  cancelBackoff.context.fetch = async () => { throw new TypeError("offline"); };
  const backoffSend = cancelBackoff.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  await settle();
  assert(cancelBackoff.state.clock.scheduled.includes(5000), "transient transport failure waits before retrying");
  await cancelBackoff.elements.get("cancel-chat").click();
  await backoffSend;
  assert.equal(cancelBackoff.state.clock.timers.size, 1, "cancel clears the retry timer and listener (only the unrelated 30-second poll remains)");
  assert.equal(cancelBackoff.state.submit.disabled, false);
  assert.equal(cancelBackoff.state.input.value, "等待網路重試");

  const running = createContext(); await settle();
  running.state.input.value = "仍在處理";
  running.state.chatSnapshots.set("request-1", {request_id: "request-1", conversation_id: "conversation-1", items: [
    {id: 81, request_id: "request-1", role: "user", content: {text: "仍在處理", status: "completed", attachments: []}},
    {id: 82, request_id: "request-1", role: "assistant", content: {text: "目前內容", status: "running", events: []}},
  ]});
  running.context.ChatSSE.read = async () => {};
  const runningSend = running.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  await finishWithClock(running, runningSend);
  assert.equal(running.state.chatSnapshotRequests.length, 6, "running state is checked immediately and up to five times at 5-second intervals");
  assert.equal(running.state.submit.disabled, true, "a still-running snapshot keeps sending blocked");
  assert.equal(running.elements.get("refresh-messages").disabled, false, "manual snapshot refresh stays available");
  await running.elements.get("cancel-chat").click();
  assert.equal(running.state.chatCancelRequests.length, 1);
  assert.equal(vm.runInContext("activeChat", running.context), null, "accepted cancel plus terminal snapshot unlocks the composer");
  assert.equal(running.state.submit.disabled, false);
  assert.match(running.elements.get("chat-status").textContent, /已停止回覆/);

  const recovered = createContext(); await settle();
  recovered.state.input.value = "恢復保存的回覆";
  recovered.state.chatSnapshots.set("request-1", {request_id: "request-1", conversation_id: "conversation-1", items: [
    {id: 61, request_id: "request-1", role: "user", content: {text: "恢復保存的回覆", status: "completed", attachments: []}},
    {id: 62, request_id: "request-1", role: "assistant", content: {text: "快照內容", status: "completed", events: []}},
  ]});
  recovered.context.ChatSSE.read = async () => { throw new Error("stream ended"); };
  await recovered.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(recovered.state.fetchCalls.length, 1, "accepted stream interruption never issues a second POST");
  assert.equal(recovered.state.chatSnapshotRequests.length, 1);
  assert.match(recovered.elements.get("chat-list").textContent, /快照內容/);
  assert.equal(recovered.state.submit.disabled, false);

  const interrupted = createContext(); await settle();
  interrupted.state.input.value = "已接受的訊息";
  interrupted.state.chatSnapshots.set("request-1", {request_id: "request-1", conversation_id: "conversation-1", items: [
    {id: 71, request_id: "request-1", role: "user", content: {text: "已接受的訊息", status: "completed", attachments: []}},
    {id: 72, request_id: "request-1", role: "assistant", content: {text: "部分回覆", status: "interrupted", events: []}},
  ]});
  interrupted.context.ChatSSE.read = async () => {};
  await interrupted.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(interrupted.state.input.value, "", "accepted draft must not be restored and resent after interruption");
  assert.match(interrupted.elements.get("chat-status").textContent, /鉛筆編輯後送出/);
  assert.equal(interrupted.state.submit.disabled, false);
  const interruptedUser = interrupted.elements.get("chat-list").children[0];
  const editButton = all(interruptedUser, item => item.className === "chat-message-edit")[0];
  assert.ok(editButton);
  assert.equal(all(interruptedUser, item => item.tagName === "BUTTON").length, 1, "interrupted snapshots offer only edit");
  interrupted.context.crypto.randomUUID = () => "request-2";
  interrupted.context.ChatSSE.read = async (_reader, handler) => handler("done", {request_id: interrupted.state.chatRequestId, status: "completed"});
  await editButton.click();
  interrupted.state.input.value = "修訂後重新送出";
  await interrupted.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(interrupted.state.chatPayloads.at(-1).edit_message_id, "71", "edit uses the stable user message id");
  assert.equal(interrupted.state.chatPayloads.at(-1).edit_request_id, "request-1", "edit uses the prior request as optimistic version");
  assert.equal(interrupted.elements.get("chat-list").children.length, 2, "edit replaces the failed pair instead of appending a duplicate");
  assert.equal(interrupted.elements.get("chat-list").children[0].dataset.messageId, "71");

  const unsupported = createContext(); await settle();
  unsupported.state.input.value = "未送出的文字";
  unsupported.context.crypto.randomUUID = () => { throw new Error("unsupported"); };
  await unsupported.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.match(unsupported.elements.get("chat-status").textContent, /訊息尚未送出/);
  assert.equal(unsupported.state.input.value, "未送出的文字");
  assert.equal(unsupported.state.fetchCalls.length, 0);
}

async function testEditRegenerationInPlace() {
  const home = createContext(); await settle();
  const list = home.elements.get("chat-list");
  const originalUser = home.context.chatMessage("user", "原本問題", "completed", [], [{filename: "履歷.pdf"}], null, "old-request", 101);
  const originalAssistant = home.context.chatMessage("assistant", "舊回覆", "completed", [], [], null, "old-request", 102);
  const laterUser = home.context.chatMessage("user", "後續問題", "completed", [], [], null, "later-request", 103);
  const laterAssistant = home.context.chatMessage("assistant", "後續回答", "completed", [], [], null, "later-request", 104);
  list.replaceChildren(originalUser, originalAssistant, laterUser, laterAssistant);
  const editButton = all(originalUser, item => item.className === "chat-message-edit")[0];
  assert.equal(editButton.disabled, false);
  assert.equal(editButton["aria-label"], "編輯這則訊息");
  assert.equal(editButton.title, "編輯這則訊息");
  assert.match(home.elements.get("chat-list").textContent, /後續回答/);

  home.state.input.value = "先前的私人草稿";
  await editButton.click();
  assert.equal(home.state.input.value, "先前的私人草稿", "a different draft is never silently overwritten");
  assert.match(home.elements.get("chat-status").textContent, /先送出或清除/);
  home.state.input.value = "";
  await editButton.click();
  assert.equal(home.state.input.value, "原本問題");
  assert.equal(home.elements.get("message-edit-banner").hidden, false);
  assert.match(home.elements.get("message-edit-status").textContent, /後面的對話會保留/);
  assert.match(home.elements.get("message-edit-status").textContent, /原檔未保存，請重新附加原附件或替換成新附件後送出/);
  await home.elements.get("cancel-message-edit").click();
  assert.equal(home.elements.get("message-edit-banner").hidden, true);
  assert.equal(home.state.input.value, "原本問題", "cancel leaves edited text as a draft");

  await editButton.click();
  const pdf = new File(["replacement resume"], "新版履歷.pdf", {type: "application/pdf"});
  home.state.fileInput.files = [pdf];
  await home.state.fileInput.dispatch("change");
  home.state.input.value = "改寫後的問題";
  home.state.chatSnapshots.set("request-1", {request_id: "request-1", conversation_id: "conversation-1", items: [
    {id: 101, request_id: "request-1", role: "user", content: {text: "改寫後的問題", status: "completed", attachments: [{filename: "新版履歷.pdf"}]}, created_at: "2026-10-08T00:00:00Z"},
    {id: 102, request_id: "request-1", role: "assistant", content: {text: "重新生成的回答", status: "completed", events: []}, created_at: "2026-10-08T00:00:01Z"},
  ]});
  home.context.ChatSSE.read = async (_reader, handler) => {
    await handler("retry", {attempt: 1, max_attempts: 5, message: "模型暫時失敗，正在重試。"});
    await handler("text_delta", {text: "失敗片段"});
    await handler("text_reset", {text: "重新生成的回答"});
    await handler("done", {request_id: home.state.chatRequestId, status: "completed"});
  };
  await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  const payload = home.state.chatPayloads.at(-1);
  assert.equal(payload.edit_message_id, "101");
  assert.equal(payload.edit_request_id, "old-request");
  assert.equal(payload.resume, pdf, "editing reuses the existing multipart attachment field");
  assert.equal(payload.conversation_id, "conversation-1");
  assert.equal(home.elements.get("message-edit-banner").hidden, true);
  assert.equal(home.state.fileInput.files.length, 0);
  assert.equal(list.children.length, 4, "edit replaces exactly its user and assistant rows");
  const editedUser = list.children[0]; const editedAssistant = list.children[1];
  assert.equal(editedUser.dataset.messageId, "101", "the original user's stable message id remains");
  assert.equal(editedUser.dataset.requestId, "request-1");
  assert.match(editedUser.textContent, /改寫後的問題/);
  assert.equal(editedAssistant.dataset.requestId, "request-1");
  assert.equal(editedAssistant.dataset.messageId, "102", "the completed snapshot restores the stable assistant message id");
  assert.equal(all(editedAssistant, item => item.className === "chat-time").length, 1, "the snapshot restores the original message timestamp");
  assert.match(editedAssistant.textContent, /重新生成的回答/);
  assert.doesNotMatch(editedAssistant.textContent, /失敗片段/);
  assert.doesNotMatch(editedAssistant.textContent, /回覆正在進行/);
  assert.equal(list.children[2], laterUser, "later user messages keep their original order and DOM identity");
  assert.equal(list.children[3], laterAssistant, "later replies keep their original order and DOM identity");
  assert.match(home.elements.get("chat-status").textContent, /回覆已完成/);

  const snapshotUnavailable = createContext(); await settle();
  const unavailableUser = snapshotUnavailable.context.chatMessage("user", "原文", "completed", [], [], null, "old-request", 151);
  const unavailableReply = snapshotUnavailable.context.chatMessage("assistant", "舊答覆", "completed", [], [], null, "old-request", 152);
  snapshotUnavailable.elements.get("chat-list").replaceChildren(unavailableUser, unavailableReply);
  await all(unavailableUser, item => item.className === "chat-message-edit")[0].click();
  snapshotUnavailable.state.input.value = "成功的新文字";
  snapshotUnavailable.context.ChatSSE.read = async (_reader, handler) => {
    await handler("text_delta", {text: "成功的新回覆"});
    await handler("done", {request_id: snapshotUnavailable.state.chatRequestId, status: "completed"});
  };
  await snapshotUnavailable.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(snapshotUnavailable.state.chatSnapshotRequests.length, 1, "edit completion requests only that pair's snapshot");
  assert.equal(snapshotUnavailable.elements.get("chat-status").dataset.state, "complete", "a failed metadata refresh does not turn successful generation into an error");
  assert.match(snapshotUnavailable.elements.get("chat-list").children[0].textContent, /成功的新文字/);
  assert.match(snapshotUnavailable.elements.get("chat-list").children[1].textContent, /成功的新回覆/);

  const missingAttachment = createContext(); await settle();
  const oldUser = missingAttachment.context.chatMessage("user", "原訊息", "completed", [], [{filename: "原履歷.pdf"}], null, "request-old", 201);
  const oldReply = missingAttachment.context.chatMessage("assistant", "原回答", "completed", [], [], null, "request-old", 202);
  missingAttachment.elements.get("chat-list").replaceChildren(oldUser, oldReply);
  await all(oldUser, item => item.className === "chat-message-edit")[0].click();
  missingAttachment.context.fetch = async () => ({ok: false, status: 422, json: async () => ({error: {code: "attachment_required", message: "請重新附加原附件"}})});
  await missingAttachment.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(missingAttachment.elements.get("message-edit-banner").hidden, false, "definite edit rejection preserves edit mode");
  assert.equal(missingAttachment.state.input.value, "原訊息");
  assert.equal(missingAttachment.elements.get("chat-list").children[0], oldUser, "422 leaves the stored original message untouched");
  assert.match(missingAttachment.elements.get("chat-status").textContent, /尚未送出/);

  const snapshotEdit = createContext(); await settle();
  const userBeforeSnapshot = snapshotEdit.context.chatMessage("user", "原本", "completed", [], [], null, "request-old", 301);
  const replyBeforeSnapshot = snapshotEdit.context.chatMessage("assistant", "舊答案", "completed", [], [], null, "request-old", 302);
  const later = snapshotEdit.context.chatMessage("user", "後來的訊息", "completed", [], [], null, "later", 303);
  snapshotEdit.elements.get("chat-list").replaceChildren(userBeforeSnapshot, replyBeforeSnapshot, later);
  snapshotEdit.state.chatSnapshots.set("request-1", {request_id: "request-1", conversation_id: "conversation-1", items: [
    {id: 301, request_id: "request-1", role: "user", content: {text: "修訂內容", status: "completed", attachments: []}},
    {id: 302, request_id: "request-1", role: "assistant", content: {text: "從快照恢復的答案", status: "completed", events: []}},
  ]});
  await all(userBeforeSnapshot, item => item.className === "chat-message-edit")[0].click();
  snapshotEdit.state.input.value = "修訂內容";
  snapshotEdit.context.ChatSSE.read = async () => { throw new Error("connection closed"); };
  await snapshotEdit.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(snapshotEdit.state.fetchCalls.length, 1, "accepted edit recovers without another POST");
  assert.equal(snapshotEdit.state.chatPayloads.at(-1).edit_message_id, "301");
  assert.equal(snapshotEdit.state.chatPayloads.at(-1).edit_request_id, "request-old", "JSON edit sends the optimistic version too");
  assert.equal(snapshotEdit.elements.get("chat-list").children.length, 3);
  assert.match(snapshotEdit.elements.get("chat-list").children[1].textContent, /從快照恢復的答案/);
  assert.equal(snapshotEdit.elements.get("chat-list").children[2], later, "snapshot replacement leaves later messages in place");

  const stale = createContext(); await settle();
  const staleUser = stale.context.chatMessage("user", "原版本", "completed", [], [], null, "old-request", 401);
  const staleReply = stale.context.chatMessage("assistant", "原回覆", "completed", [], [], null, "old-request", 402);
  stale.elements.get("chat-list").replaceChildren(staleUser, staleReply);
  await all(staleUser, item => item.className === "chat-message-edit")[0].click();
  stale.state.input.value = "較新的修訂稿";
  let staleCalls = 0;
  stale.context.fetch = async () => { staleCalls++; return {ok: false, status: 409, json: async () => ({error: {code: "message_changed", message: "原訊息已被其他裝置修改"}})}; };
  await stale.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(staleCalls, 1, "version conflict never retries over another edit");
  assert.equal(stale.elements.get("chat-list").children[0], staleUser);
  assert.equal(stale.elements.get("chat-list").children[1], staleReply);
  assert.equal(stale.state.input.value, "較新的修訂稿");
  assert.equal(stale.elements.get("message-edit-banner").hidden, false);
}

async function testDraftShortcutsPortraitsAndResume() {
  const home = createContext(); await settle();
  home.state.input.scrollHeight = 96;
  home.state.input.value = "多行草稿";
  await home.state.input.dispatch("input");
  assert.equal(home.state.input.style.height, "96px", "typing uses the browser-measured content height");
  home.state.input.scrollHeight = 44;
  home.state.input.value = "";
  await home.state.input.dispatch("input");
  assert.equal(home.state.input.style.height, "44px", "deleting content shrinks the input again");
  const apiCount = home.state.apiCalls.length;
  home.state.input.value = "我已有草稿";
  await home.state.quickCard.click();
  assert.equal(home.state.input.value, "我已有草稿\n請找生態調查員的工作。");
  assert.equal(home.state.apiCalls.length, apiCount);
  assert.equal(home.state.fetchCalls.length, 0, "shortcut never sends or searches");
  await home.elements.get("toggle-quick-options").click();
  assert.equal(home.elements.get("quick-options").hidden, false);
  assert.equal(home.elements.get("toggle-quick-options")["aria-expanded"], "true");
  await home.elements.get("toggle-quick-options").click();
  assert.equal(home.elements.get("quick-options").hidden, true);
  const controllers = home.state.controllers.length;
  const unusual = [{state: "mail", frame_width: 128, frame_height: 64, frame_count: 4, fps: 8}];
  home.context.setupCompanion({sprites: unusual});
  const afterSetup = home.state.controllers.length;
  for (let i = 0; i < 20; i++) home.context.chatMessage("assistant", "任意訊息");
  assert.equal(afterSetup, controllers + 1);
  assert.equal(home.state.controllers.length, afterSetup, "history portraits create no animation controllers");
  assert.equal(home.state.portraits.at(-1).sprites[0], unusual[0]);
  home.state.fileInput.files = [new File(["synthetic PDF"], "履歷.pdf", {type: "application/pdf"})];
  await home.state.fileInput.dispatch("change");
  assert.equal(home.elements.get("resume-selection").hidden, false);
  const file = home.state.fileInput.files[0];
  home.context.fetch = async (_path, options) => {
    assert(options.body instanceof FormData);
    assert.equal(options.body.get("resume"), file);
    assert.equal(options.headers["X-CSRF-Token"], "csrf-1");
    assert.equal(options.headers["Content-Type"], undefined, "browser supplies multipart boundary");
    return {ok: false, status: 422, json: async () => ({error: {code: "invalid_resume", message: "PDF 格式不符"}})};
  };
  const draft = home.state.input.value;
  await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(home.state.input.value, draft);
  assert.equal(home.state.fileInput.files[0], file, "rejected upload keeps the attachment");
  assert.match(home.elements.get("chat-status").textContent, /尚未送出/);
  home.context.fetch = async (_path, options) => {
    home.state.chatRequestId = options.body.get("request_id");
    return {ok: true, headers: {get: () => "text/event-stream"}, body: {getReader: () => ({})}};
  };
  await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(home.state.fileInput.files.length, 0);
  assert.equal(home.state.input.value, "");
  assert.equal(home.elements.get("resume-selection").hidden, true);
  let lastValidBoundary = null;
  for (const size of [5 * 1024 * 1024 + 1, 20 * 1024 * 1024, 20 * 1024 * 1024 + 1]) {
    const candidate = {name: "boundary.pdf", size};
    home.state.fileInput.files = [candidate];
    await home.state.fileInput.dispatch("change");
    const allowed = size <= 20 * 1024 * 1024;
    if (allowed) lastValidBoundary = candidate;
    assert.equal(home.state.fileInput.files.length, 1, "invalid replacement keeps the last valid file");
    assert.equal(home.state.fileInput.files[0], lastValidBoundary);
    assert.equal(home.elements.get("resume-selection").hidden, false);
    if (!allowed) assert.match(home.elements.get("chat-status").textContent, /20 MiB/);
  }
  const message = home.context.chatMessage("user", "安全文字", "completed", [], [{filename: "<unsafe>.pdf", pages: 2}]);
  assert.match(message.textContent, /<unsafe>\.pdf · 2 頁/);
  home.state.fileInput.files = [new File(["x"], "other.exe")];
  await home.state.fileInput.dispatch("change");
  assert.equal(home.state.fileInput.files[0], lastValidBoundary, "unsupported replacement keeps the existing attachment");
  const late = createContext(); await settle();
  late.state.input.value = "未接受前的私人草稿";
  late.state.fileInput.files = [file];
  let resolveUpload;
  let cancelled = false;
  late.context.fetch = async () => await new Promise(resolve => { resolveUpload = resolve; });
  const sending = late.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  await settle(); late.context.signedOut();
  resolveUpload({ok: true, headers: {get: () => "text/event-stream"}, body: {cancel: async () => { cancelled = true; }}});
  await sending;
  assert.equal(cancelled, true, "late accepted upload stream is closed after logout");
  assert.equal(late.elements.get("chat-list").textContent, "");
  assert.equal(late.state.fileInput.files.length, 0);
}

async function testAttachmentDropSelectionAndBoundaries() {
  const home = createContext(); await settle();
  const panel = home.elements.get("chat");
  const overlay = home.elements.get("chat-drop-overlay");
  const makeDrag = files => ({dataTransfer: {types: ["Files"], files, dropEffect: ""}, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }});
  const oldFile = new File(["old document"], "existing.md", {type: "text/markdown"});
  home.state.input.value = "草稿不要改動";
  home.state.fileInput.files = [oldFile]; await home.state.fileInput.dispatch("change");

  const nested = new FakeElement("div"); panel.append(nested);
  const enter = makeDrag([new File(["image"], "picture.png", {type: "image/png"})]);
  await panel.dispatch("dragenter", enter);
  assert.equal(enter.defaultPrevented, true);
  assert.equal(overlay.hidden, false);
  const nestedEnter = makeDrag(enter.dataTransfer.files); await panel.dispatch("dragenter", nestedEnter);
  const nestedLeave = makeDrag(enter.dataTransfer.files); nestedLeave.relatedTarget = nested;
  await panel.dispatch("dragleave", nestedLeave);
  assert.equal(overlay.hidden, false, "moving over nested chat content keeps the drop overlay active");
  const leave = makeDrag(enter.dataTransfer.files); leave.relatedTarget = new FakeElement("div");
  await panel.dispatch("dragleave", leave);
  assert.equal(overlay.hidden, true, "leaving the chat panel clears the overlay");

  const image = new File(["synthetic image"], "portrait.WEBP", {type: "image/webp"});
  const enterImage = makeDrag([image]); await panel.dispatch("dragenter", enterImage);
  const overImage = makeDrag([image]); await panel.dispatch("dragover", overImage);
  assert.equal(overImage.defaultPrevented, true); assert.equal(overImage.dataTransfer.dropEffect, "copy");
  const dropImage = makeDrag([image]); await panel.dispatch("drop", dropImage);
  assert.equal(dropImage.defaultPrevented, true);
  assert.equal(home.state.fileInput.files[0], image, "valid image drop replaces the native input selection");
  assert.equal(home.state.input.value, "草稿不要改動", "valid drop keeps a nonempty draft");
  assert.equal(overlay.hidden, true);
  assert.equal(home.state.fetchCalls.some(item => item.path === "/api/chat"), false, "dropping a file never sends it automatically");

  for (const files of [
    [new File(["bad"], "unsupported.exe")],
    [{name: "large.txt", size: 20 * 1024 * 1024 + 1}],
    [new File(["image"], "one.png"), new File(["text"], "two.md")],
  ]) {
    const event = makeDrag(files); await panel.dispatch("drop", event);
    assert.equal(event.defaultPrevented, true);
    assert.equal(home.state.fileInput.files[0], image, "invalid, oversized and multiple drops preserve the selected attachment");
    assert.equal(home.state.input.value, "草稿不要改動", "invalid drops preserve the draft");
    assert.match(home.elements.get("chat-status").textContent, /保留/);
  }
  home.state.fileInput.files = [new File(["bad"], "unsupported.exe")];
  await home.state.fileInput.dispatch("change");
  assert.equal(home.state.fileInput.files[0], image, "invalid picker selection rolls back to the prior valid attachment");

  home.state.input.value = "";
  const pdf = new File(["resume"], "resume.pdf", {type: "application/pdf"});
  home.state.fileInput.files = [pdf]; await home.state.fileInput.dispatch("change");
  assert.match(home.state.input.value, /這份履歷/, "blank drafts get the existing PDF-specific prompt");
  home.state.input.value = "";
  const imagePrompt = new File(["image"], "portrait.jpg", {type: "image/jpeg"});
  await panel.dispatch("drop", makeDrag([imagePrompt]));
  assert.match(home.state.input.value, /這張圖片/, "blank drafts get an image-aware prompt");
  home.state.input.value = "";
  const markdown = new File(["# Notes"], "notes.markdown", {type: "text/markdown"});
  await panel.dispatch("drop", makeDrag([markdown]));
  assert.equal(home.state.fileInput.files[0], markdown);
  assert.match(home.state.input.value, /這份文件/);
  assert.equal(home.state.fetchCalls.some(item => item.path === "/api/chat"), false);

  const textDrag = {dataTransfer: {types: ["text/uri-list"], files: []}, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }};
  await panel.dispatch("dragover", textDrag);
  assert.equal(textDrag.defaultPrevented, false, "URL/text dragging retains native browser behavior");
  const windowTextDrag = Object.assign({dataTransfer: {types: ["text/uri-list"], files: []}, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }}, {type: "dragover"});
  home.window.dispatchEvent(windowTextDrag);
  assert.equal(windowTextDrag.defaultPrevented, false, "global file-navigation guard leaves URL/text drags untouched");
  const outsideFileDrag = Object.assign(makeDrag([image]), {type: "dragover"}); home.window.dispatchEvent(outsideFileDrag);
  assert.equal(outsideFileDrag.defaultPrevented, true, "file dragover is prevented outside chat so a later drop cannot navigate");
  const pageFileDrag = Object.assign(makeDrag([image]), {type: "drop"}); home.window.dispatchEvent(pageFileDrag);
  assert.equal(pageFileDrag.defaultPrevented, true, "file drop outside chat cannot navigate the browser away");
  assert.equal(home.state.fileInput.files[0], markdown, "outside file drops do not attach to the chat");

  await panel.dispatch("dragenter", makeDrag([image]));
  home.window.dispatchEvent({type: "blur"});
  assert.equal(overlay.hidden, true, "window blur clears drag feedback");
  await panel.dispatch("dragenter", makeDrag([image]));
  home.context.signedOut();
  assert.equal(overlay.hidden, true, "logout clears drag feedback");
  assert.equal(home.state.fileInput.files.length, 0);

  const upload = createContext(); await settle();
  const droppedImage = new File(["image bytes"], "portfolio.png", {type: "image/png"});
  upload.state.input.value = "請看這張作品圖片";
  await upload.elements.get("chat").dispatch("drop", makeDrag([droppedImage]));
  assert.equal(upload.state.fetchCalls.some(item => item.path === "/api/chat"), false, "image drop waits for the user's explicit send");
  await upload.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(upload.state.chatPayloads.at(-1).resume, droppedImage, "dropped images reuse the existing multipart resume field");
  assert.equal(upload.state.chatPayloads.at(-1).conversation_id, "conversation-1");

  const busy = createContext(); await settle();
  const inFlight = new File(["in-flight"], "resume.pdf", {type: "application/pdf"});
  busy.state.input.value = "正在送出的訊息"; busy.state.fileInput.files = [inFlight]; await busy.state.fileInput.dispatch("change");
  const originalFetch = busy.context.fetch; let releaseFetch;
  busy.context.fetch = async (...args) => await new Promise(resolve => { releaseFetch = () => resolve(originalFetch(...args)); });
  const sending = busy.elements.get("chat-form").dispatch("submit", {preventDefault() {}}); await settle();
  const replacement = new File(["replacement"], "new.png", {type: "image/png"});
  const busyEnter = makeDrag([replacement]); await busy.elements.get("chat").dispatch("dragenter", busyEnter);
  assert.match(busy.elements.get("chat-drop-overlay").textContent, /回覆中/);
  const busyOver = makeDrag([replacement]); await busy.elements.get("chat").dispatch("dragover", busyOver);
  assert.equal(busyOver.dataTransfer.dropEffect, "none");
  const busyDrop = makeDrag([replacement]); await busy.elements.get("chat").dispatch("drop", busyDrop);
  assert.equal(busy.elements.get("resume-file").disabled, true);
  assert.equal(busy.state.fileInput.files[0], inFlight, "reply-in-progress rejects drops without replacing the submitted file");
  assert.match(busy.elements.get("chat-status").textContent, /回覆進行中/);
  releaseFetch(); await sending;
}

async function testLinkedRecommendations() {
  const home = createContext(); await settle();
  const jobs = [
    {id: "unselected", title: "合成餐飲候選", url: "https://www.104.com.tw/job/food"},
    {id: "other-source", title: "Archivist", url: "https://www.518.com.tw/job-library.html"},
    {id: "selected", title: "生態調查員", url: "https://www.104.com.tw/job/ecology"}
  ];
  const event = {event: "jobs", data: {items: jobs, source_results: [{source: "104", status: "partial", returned_count: 3}], filter_warnings: ["合成來源限制"]}};
  const text = "參考 [生態職缺](https://www.104.com.tw/job/ecology) 與 https://www.518.com.tw/job-library.html。\n<script>alert(1)</script> javascript:alert(1)";
  const history = home.context.chatMessage("assistant", text, "completed", [event, event]);
  const all = (root, predicate) => [root, ...root.children.flatMap(child => all(child, predicate))].filter(predicate);
  const cards = all(history, item => item.className === "job-card item");
  assert.deepEqual(cards.map(card => card.dataset.jobId), ["selected", "other-source"], "follow reply citation order, not search order; deduplicate across searches");
  assert.deepEqual(cards.map(card => card.children[0].textContent), ["職缺 #3", "職缺 #2"]);
  assert.equal(all(history, item => item.tagName === "DETAILS" && item.className === "chat-search-log").every(item => !item.open), true);
  assert.equal(all(cards[0], item => item.tagName === "DIV" && item.className === "job-description" && item.hidden).length, 1, "details start collapsed behind 更多");
  assert.equal(all(cards[0], item => item.tagName === "BUTTON" && item.textContent === "更多").length, 1, "details use one inline expand control");
  assert.equal(all(cards[0], item => item.tagName === "BUTTON").every(item => !item.disabled), true, "favorite and review remain usable");
  assert.equal(all(history, item => item.tagName === "SCRIPT").length, 0);
  const links = all(history, item => item.className === "chat-link");
  assert.equal(links[0].textContent, "生態職缺");
  assert.equal(links[1].href, jobs[1].url, "Chinese punctuation is outside the URL");
  assert.equal(links.every(link => link.rel === "noopener noreferrer" && link.target === "_blank"), true);
  const unsafe = home.context.chatMessage("assistant", "[bad](https://user:pass@example.invalid/) https://example.invalid/a(b). https://example.invalid/a). https://example.invalid/?x=1&y=2");
  const safe = all(unsafe, item => item.className === "chat-link");
  assert.equal(safe.length, 3);
  assert.equal(safe[0].href, "https://example.invalid/a(b)");
  assert.equal(safe[1].href, "https://example.invalid/a");
  assert.equal(safe[2].href, "https://example.invalid/?x=1&y=2");
  const noCitations = home.context.chatMessage("assistant", "沒有合適職缺。", "completed", [event]);
  assert.equal(all(noCitations, item => item.className === "job-card item").length, 0);
  home.context.ChatSSE.read = async (_reader, handler) => {
    await handler("jobs", event.data);
    await handler("text_delta", {text: "[生態職缺](https://www.104.com.tw/jo"});
    await handler("text_delta", {text: "b/ecology)"});
    await handler("done", {request_id: home.state.chatRequestId, status: "completed"});
    const streamed = home.elements.get("chat-list").children.at(-1);
    assert.equal(all(streamed, item => item.className === "chat-link")[0].href, jobs[2].url);
    assert.deepEqual(all(streamed, item => item.className === "job-card item").map(card => card.dataset.jobId), ["selected"]);
  };
  home.state.input.value = "請找工作";
  await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(home.elements.get("chat-status").dataset.state, "complete", home.elements.get("chat-status").textContent);
  home.state.history = [{role: "assistant", content: {text, status: "completed", events: [event]}}];
  await home.context.loadMessages();
  assert.deepEqual(all(home.elements.get("chat-list"), item => item.className === "job-card item").map(card => card.dataset.jobId), ["selected", "other-source"]);
  const reversed = home.context.chatMessage("assistant", "https://www.518.com.tw/job-library.html https://www.104.com.tw/job/ecology https://www.518.com.tw/job-library.html", "completed", [event]);
  assert.deepEqual(all(reversed, item => item.className === "job-card item").map(card => card.dataset.jobId), ["other-source", "selected"], "order changes with the reply and repeated citations create no duplicates");
}

async function testChatScroll() {
  const home = createContext(); await settle();
  const viewport = home.elements.get("chat-transcript");
  const latest = home.elements.get("latest-message");
  viewport.clientHeight = 300; viewport.scrollHeight = 1200; viewport.scrollTop = 200;
  home.state.history = [{role: "assistant", content: {text: "舊回覆", status: "completed", events: []}}];
  home.elements.get("chat-list").append(new FakeElement());
  await viewport.dispatch("scroll");
  assert.equal(latest.hidden, false);
  await home.context.loadMessages();
  assert.equal(viewport.scrollTop, 200, "history refresh preserves a reader's position");
  await latest.click();
  assert.equal(latest.hidden, true);
  assert.equal(viewport.scrollTop, 1200);
  // The fake DOM models the browser's scroll clamping and growth explicitly.
  viewport.scrollTop = 900;
  const list = home.elements.get("chat-list");
  const prepend = list.prepend.bind(list);
  list.prepend = fragment => { prepend(fragment); viewport.scrollHeight += 600; };
  await home.context.loadMessages(true);
  assert.equal(viewport.scrollTop, 1500, "loading older messages keeps the same content in view");
  home.context.ChatSSE.read = async (_reader, handler) => {
    viewport.scrollTop = viewport.scrollHeight - viewport.clientHeight;
    const text = home.context.renderChatText;
    home.context.renderChatText = (...args) => { const result = text(...args); viewport.scrollHeight += 200; return result; };
    await handler("text_delta", {text: "新回覆"});
    assert.equal(viewport.scrollTop, viewport.scrollHeight, "stream follows while at the bottom");
    viewport.scrollTop = 100;
    await handler("text_delta", {text: "繼續回覆"});
    assert.equal(viewport.scrollTop, 100, "stream does not pull a reader away from older messages");
    assert.equal(latest.hidden, false);
    await handler("done", {request_id: home.state.chatRequestId, status: "completed"});
    assert.equal(viewport.scrollTop, 100);
  };
  home.state.input.value = "繼續對話";
  await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(viewport.scrollTop, 100);
}

async function testFavoriteKeepsHistoryAndPaging() {
  const home = createContext(); await settle();
  const job = {id: "same/id", title: "合成職缺", url: "https://example.invalid/job", is_favorite: false};
  const event = {event: "jobs", data: {items: [job]}};
  home.state.history = Array.from({length: 20}, () => ({role: "assistant", content: {text: job.url, events: [event]}}));
  const baseApi = home.context.api;
  home.context.api = async (path, options) => path.startsWith("/api/messages?") && !path.includes("cursor=") ? {items: home.state.history, next_cursor: "older"} : path.includes("cursor=older") ? {items: home.state.history.slice(0, 6), next_cursor: "oldest"} : baseApi(path, options);
  await home.context.loadMessages(); await home.context.loadMessages(true);
  const list = home.elements.get("chat-list");
  assert.equal(list.children.length, 26);
  const firstMessage = list.children[0];
  const pendingMessage = home.context.chatMessage("assistant", "");
  home.context.renderChatEvent(pendingMessage, "jobs", {items: [{...job}]}); list.append(pendingMessage);
  const buttons = all(list, item => item.dataset.favorite);
  const details = all(firstMessage, item => item.className === "job-description")[0]; details.hidden = false;
  home.elements.get("chat-transcript").scrollTop = 145;
  let resolveFavorite; const requests = [];
  home.context.api = async (path, options) => { requests.push({path, options}); return new Promise(resolve => { resolveFavorite = resolve; }); };
  const favorite = home.context.toggleFavorite(job, buttons[0]);
  await home.context.toggleFavorite(job, buttons[1]);
  assert.equal(requests.length, 1); assert(buttons.every(button => button.disabled));
  resolveFavorite(null); await favorite;
  assert.equal(list.children.length, 27); assert.equal(list.children[0], firstMessage); assert.equal(details.hidden, false);
  assert.equal(home.elements.get("chat-transcript").scrollTop, 145);
  assert(buttons.every(button => button.getAttribute("aria-pressed") === "true" && !button.disabled));
  home.context.renderChatRecommendations(pendingMessage, job.url);
  assert.equal(all(pendingMessage, item => item.dataset.favorite)[0].getAttribute("aria-pressed"), "true", "unfinished stream candidate updates too");
  const cancel = home.context.toggleFavorite(job, buttons[1]); resolveFavorite(null); await cancel;
  assert.equal(requests[1].options.method, "DELETE");
  assert.equal(vm.runInContext("messageCursor", home.context), "oldest");
  home.context.api = async () => { throw new Error("合成失敗"); };
  await home.context.toggleFavorite(job, buttons[0]);
  assert.equal(buttons[0].getAttribute("aria-pressed"), "false");
  assert.match(firstMessage.textContent, /收藏未能更新/);
  const favorites = home.elements.get("favorite-list");
  const card = home.context.jobCard({...job, is_favorite: true}, true); favorites.append(card);
  home.elements.get("more-favorites").hidden = false;
  vm.runInContext('favoriteCursor = "next-favorites"; dailyCursor = "next-daily";', home.context);
  home.context.api = async () => null;
  await home.context.toggleFavorite(job, all(card, item => item.dataset.favorite)[0]);
  assert.match(favorites.textContent, /載入更多/);
  assert.equal(vm.runInContext("favoriteCursor", home.context), "next-favorites");
  assert.equal(vm.runInContext("dailyCursor", home.context), "next-daily");
  let resolveLate;
  home.context.api = () => new Promise(resolve => { resolveLate = resolve; });
  const late = home.context.toggleFavorite(job, buttons[0]); home.context.signedOut(); resolveLate(null); await late;
  assert.equal(list.children.length, 0);
}

async function testReviewLoadingRetryAndLateResults() {
  const home = createContext(); await settle();
  let resolve, reject; let count = 0;
  home.context.api = () => { count++; return new Promise((yes, no) => { resolve = yes; reject = no; }); };
  const dialog = home.elements.get("review-dialog"), result = home.elements.get("review-result");
  const query = home.context.showReviews({company_name: "合成公司"});
  assert.equal(dialog.open, true); assert.match(result.textContent, /正在查詢/);
  await home.context.showReviews({company_name: "另一公司"}); assert.equal(count, 1);
  reject(new Error("合成503")); await query;
  assert.match(result.textContent, /合成503/); assert.equal(result.getAttribute("aria-busy"), "false");
  const retry = all(result, item => item.tagName === "BUTTON")[0].click();
  resolve({company_name: "合成公司", evidence: [], unavailable_sources: ["合成來源"], choices: [{source: "GoodJob", company_name: "合成公司", company_id: "different"}]}); await retry;
  assert.match(result.textContent, /暫時無法查詢的來源/); assert.match(result.textContent, /different/);
  const late = home.context.showReviews({company_name: "合成公司"}); dialog.close();
  resolve({company_name: "晚回公司"}); await late;
  assert.equal(dialog.open, false); assert.doesNotMatch(result.textContent, /晚回公司/);
  const logout = home.context.showReviews({company_name: "合成公司"}); home.context.signedOut();
  reject(new Error("晚回錯誤")); await logout;
  assert.equal(result.textContent, ""); assert.equal(dialog.open, false);
}

async function testProposalFocusAndConfirmationBoundary() {
  const home = createContext(); await settle();
  const data = {paused: false, user_has_chosen: true, effective: [{id: "pref", revision: 1, criteria: {}}], proposals: []};
  const calls = [];
  home.context.api = async (path, options) => {
    calls.push({path, options});
    if (path === "/api/preferences") return data;
    if (path === "/api/preference-proposals") {
      const proposal = {id: `new-${calls.length}`, operation: JSON.parse(options.body).operation, before: {}, after: {}};
      data.proposals.push(proposal); return proposal;
    }
    if (path.endsWith("/cancel")) { data.proposals = []; return {}; }
    if (path.endsWith("/confirm")) { const error = new Error("提案已過期"); error.status = 409; throw error; }
    throw new Error("unexpected request");
  };
  await home.elements.get("criteria-form").dispatch("submit", {preventDefault() {}});
  const list = home.elements.get("proposal-list"); const first = list.children[0];
  assert.equal(list.children.length, 1, home.elements.get("preferences-feedback").textContent);
  assert.equal(first.dataset.proposalId, data.proposals[0].id); assert.equal(first.children[0].focused, true);
  assert.equal(first.children[0].scrolled, true); assert.match(first.textContent, /尚未生效/);
  assert.equal(calls.some(call => call.path.endsWith("/confirm")), false);
  await all(first, item => item.tagName === "BUTTON")[0].click();
  assert.match(first.textContent, /重新檢視/);
  await all(first, item => item.tagName === "BUTTON")[1].click();
  assert.match(home.elements.get("preferences-feedback").textContent, /原條件保持不變/);
  assert.equal(data.effective.length, 1);
  await home.context.loadPreferences();
  await all(home.elements.get("preference-list"), item => item.tagName === "BUTTON")[0].click();
  await home.elements.get("criteria-form").dispatch("submit", {preventDefault() {}});
  assert.equal(data.proposals[0].operation, "update");
  assert.equal(list.children[0].children[0].focused, true);
  await all(list.children[0], item => item.tagName === "BUTTON")[1].click();
  const remove = all(home.elements.get("preference-list"), item => item.tagName === "BUTTON")[1]; await remove.click();
  assert.equal(data.proposals[0].operation, "delete");
  assert.equal(list.children[0].children[0].focused, true);
}

async function testConversationLifecycleAndGuards() {
  const home = createContext();
  const makeConversation = (id, title, day) => ({id, title, created_at: `2026-10-${String(day).padStart(2, "0")}T00:00:00Z`, updated_at: `2026-10-${String(day).padStart(2, "0")}T00:00:00Z`, message_count: 1});
  home.state.conversations = Array.from({length: 22}, (_, index) => makeConversation(`conversation-${index + 1}`, `對話 ${index + 1}`, 8 - index % 8));
  home.state.conversationText.set("conversation-2", "只在正文出現的 searchable bodyword");
  home.state.conversationText.set("conversation-3", "另一段私人內容");
  await settle();
  assert.equal(vm.runInContext("selectedConversationId", home.context), "conversation-1");
  assert.equal(home.state.messageRequests[0].id, "conversation-1", "startup loads messages only after selecting the newest conversation");

  await home.elements.get("open-conversation-history").click(); await settle();
  const list = home.elements.get("conversation-list");
  assert.equal(list.children.filter(item => item.dataset.conversationId).length, 20);
  assert.equal(home.elements.get("more-conversations").hidden, false);
  await home.elements.get("more-conversations").click();
  assert.equal(list.children.filter(item => item.dataset.conversationId).length, 22);
  assert(home.state.conversationRequests.some(item => item.path.includes("cursor=20")), "history uses the returned cursor for the next page");

  home.elements.get("conversation-query").value = "bodyword";
  await home.elements.get("conversation-search-form").dispatch("submit", {preventDefault() {}});
  assert.equal(list.children.filter(item => item.dataset.conversationId).length, 1, "search includes conversation message text");
  assert(home.state.conversationRequests.at(-1).path.includes("q=bodyword"));
  const second = home.state.conversations.find(item => item.id === "conversation-2");
  home.context.showRenameConversation(second);
  const renameInput = home.elements.get("rename-conversation-input");
  renameInput.value = "   ";
  await home.elements.get("rename-conversation-form").dispatch("submit", {preventDefault() {}});
  assert.match(home.elements.get("rename-conversation-status").textContent, /1～80/);
  assert.equal(home.state.conversationRequests.filter(item => item.options.method === "PATCH").length, 0);
  renameInput.value = "改好的對話名稱";
  await home.elements.get("rename-conversation-form").dispatch("submit", {preventDefault() {}});
  assert.equal(second.title, "改好的對話名稱");
  assert.equal(JSON.parse(home.state.conversationRequests.find(item => item.options.method === "PATCH").options.body).title, "改好的對話名稱");
  home.elements.get("conversation-query").value = "改好的對話名稱";
  await home.elements.get("conversation-search-form").dispatch("submit", {preventDefault() {}});
  const secondCard = list.children.find(item => item.dataset.conversationId === "conversation-2");
  await secondCard.querySelector("[data-conversation-open]").click();
  assert.equal(vm.runInContext("selectedConversationId", home.context), "conversation-2");
  assert.equal(home.state.messageRequests.at(-1).id, "conversation-2", "opening a result requests only that conversation's messages");

  const resume = new File(["synthetic PDF"], "resume.pdf", {type: "application/pdf"});
  home.state.input.value = "草稿要保留"; home.state.fileInput.files = [resume];
  await home.context.openConversation(home.state.conversations.find(item => item.id === "conversation-3"));
  assert.equal(home.state.input.value, "草稿要保留");
  assert.equal(home.state.fileInput.files[0], resume, "switching conversations preserves the unsent draft and attachment");
  await home.elements.get("open-conversation-history").click(); await settle();
  await home.elements.get("new-conversation").click(); await settle();
  const selected = vm.runInContext("selectedConversationId", home.context);
  assert.notEqual(selected, "conversation-3");
  assert.equal(home.state.conversationRequests.filter(item => item.options.method === "POST").length, 1);
  assert.deepEqual(JSON.parse(home.state.conversationRequests.find(item => item.options.method === "POST").options.body), {title: "新對話"});
  assert.equal(home.state.input.value, "草稿要保留", "creating a conversation keeps the text draft");
  assert.equal(home.state.fileInput.files[0], resume, "creating a conversation keeps the PDF attachment");

  await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(home.state.chatPayloads.at(-1).conversation_id, selected, "JSON chat carries the selected conversation ID");
  assert.equal(home.state.chatPayloads.at(-1).edit_message_id, null, "normal multipart sends omit edit-version fields");
  home.state.input.value = "附上履歷繼續聊"; home.state.fileInput.files = [resume];
  await home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(home.state.chatPayloads.at(-1).conversation_id, selected, "PDF chat carries the selected conversation ID");
  assert.equal(home.state.chatPayloads.at(-1).resume, resume);
  assert.equal(home.state.chatPayloads.at(-1).edit_message_id, null, "normal multipart sends omit edit-version fields");
  await home.elements.get("refresh-messages").click();
  assert.equal(home.state.messageRequests.at(-1).id, selected, "refresh reloads the selected conversation");

  home.state.input.value = "送出中";
  const originalFetch = home.context.fetch;
  let releaseFetch;
  home.context.fetch = async (...args) => await new Promise(resolve => { releaseFetch = () => resolve(originalFetch(...args)); });
  const sending = home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  await settle();
  assert.equal(home.elements.get("new-conversation").disabled, true, "new conversation is locked while the reply is active");
  const prior = selected;
  assert.equal(await home.context.openConversation(home.state.conversations[0]), false, "switching is rejected while a reply is active");
  assert.equal(await home.context.createConversation(), false, "creating is rejected while a reply is active");
  assert.equal(vm.runInContext("selectedConversationId", home.context), prior);
  home.context.showDeleteConversation(home.state.conversations.find(item => item.id === selected));
  await home.elements.get("confirm-delete-conversation").click();
  assert.match(home.elements.get("delete-conversation-status").textContent, /正在回覆/);
  assert.equal(home.state.conversationRequests.some(item => item.options.method === "DELETE"), false, "active conversation cannot be deleted");
  releaseFetch(); await sending;

  home.context.showDeleteConversation(second);
  home.state.conversationDeleteError = new Error("刪除失敗，請稍後再試");
  await home.elements.get("confirm-delete-conversation").click();
  assert.equal(home.elements.get("delete-conversation-dialog").open, true, "failed deletion keeps the confirmation open");
  assert.match(home.elements.get("delete-conversation-status").textContent, /刪除失敗/);
  await home.elements.get("cancel-delete-conversation").click();
  assert(home.state.conversations.some(item => item.id === "conversation-2"), "cancel never deletes the conversation");
  home.state.conversationDeleteError = null;
  home.context.showDeleteConversation(second);
  await home.elements.get("confirm-delete-conversation").click();
  assert.equal(home.state.conversations.some(item => item.id === "conversation-2"), false);
  assert.equal(vm.runInContext("selectedConversationId", home.context), selected, "deleting a nonselected result keeps the open conversation");
  assert.equal(home.state.deleteCalls.length, 0, "conversation deletes do not use the push-subscription route");

  home.context.showDeleteConversation(home.state.conversations.find(item => item.id === selected));
  await home.elements.get("confirm-delete-conversation").click();
  assert.equal(vm.runInContext("selectedConversationId", home.context), "conversation-1", "deleting the selected conversation opens the next available conversation");
  assert.equal(home.state.messageRequests.at(-1).id, "conversation-1");

  const empty = createContext(); empty.state.conversations = []; await settle();
  assert.equal(vm.runInContext("selectedConversationId", empty.context), null);
  assert.equal(empty.state.conversationRequests.some(item => item.options.method === "POST"), false, "startup never creates an empty conversation implicitly");
  assert.equal(empty.elements.get("chat-form").querySelector("button[type=submit]").disabled, true);
  empty.state.input.value = "尚未建立對話";
  await empty.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  assert.equal(empty.state.fetchCalls.some(item => item.path === "/api/chat"), false, "sending without a selected conversation is blocked with a local message");
  await empty.elements.get("open-conversation-history").click();
  await empty.elements.get("new-conversation").click(); await settle();
  assert(vm.runInContext("selectedConversationId", empty.context));

  const creatingApi = empty.context.api;
  let releaseCreate;
  empty.context.api = async (path, options) => options?.method === "POST" && path === "/api/conversations"
    ? await new Promise(resolve => { releaseCreate = () => resolve(creatingApi(path, options)); }) : creatingApi(path, options);
  const creating = empty.context.createConversation(); await settle();
  assert.equal(empty.elements.get("new-conversation").disabled, true);
  assert.equal(empty.elements.get("chat-form").querySelector("button[type=submit]").disabled, true);
  assert.equal(await empty.context.createConversation(), false, "a pending creation cannot create duplicates or send into the old conversation");
  releaseCreate(); await creating;

  const late = createContext(); await settle();
  const realApi = late.context.api;
  let resolveList;
  late.context.api = async (path, options) => path.startsWith("/api/conversations?")
    ? await new Promise(resolve => { resolveList = resolve; }) : realApi(path, options);
  const pendingList = late.context.loadConversations(); await settle();
  late.context.signedOut();
  resolveList({items: [{id: "private-late", title: "晚回私人標題"}], next_cursor: null});
  await pendingList;
  assert.equal(late.elements.get("conversation-list").children.length, 0, "a late conversation list cannot restore private data after logout");
  assert.equal(late.elements.get("conversation-history-status").getAttribute("aria-busy"), "false");
  assert.equal(vm.runInContext("selectedConversationId", late.context), null);
}

async function testPushSummaryStates() {
  const home = createContext({push:true}); await settle();
  for (const [permission, summary] of [["default", "尚未開啟"], ["denied", "已拒絕"], ["granted", "待綁定"]]) {
    home.window.Notification.permission = permission; home.context.updatePushUi();
    assert.match(home.elements.get("push-summary").textContent, new RegExp(summary));
  }
  vm.runInContext("pushKeyMismatch = true", home.context); home.context.updatePushUi();
  assert.match(home.elements.get("push-summary").textContent, /重新綁定/);
  home.context.updatePushBootstrap({push_configured:false});
  assert.match(home.elements.get("push-summary").textContent, /尚未設定/);
}

async function testEnterToSendAndShiftEnterNewline() {
  const home = createContext(); await settle();
  let submitted = 0;
  home.elements.get("chat-form").requestSubmit = button => {
    assert.equal(button, home.state.submit); submitted++;
    return home.elements.get("chat-form").dispatch("submit", {preventDefault() {}});
  };
  const key = async detail => {
    let prevented = false;
    await home.state.input.dispatch("keydown", {key:"Enter", preventDefault() { prevented = true; }, ...detail});
    return prevented;
  };
  home.state.input.value = "合成鍵盤訊息";
  for (const detail of [{shiftKey:true}, {isComposing:true}, {keyCode:229}, {key:"a"}]) {
    assert.equal(await key(detail), false);
    assert.equal(submitted, 0, "newline, composition and other keys never submit");
  }
  assert.equal(await key({repeat:true}), true);
  assert.equal(submitted, 0, "held Enter cannot submit repeatedly");
  home.state.submit.disabled = true;
  assert.equal(await key({}), true); assert.equal(submitted, 0);
  home.state.submit.disabled = false;
  vm.runInContext("switchingConversation = true", home.context);
  assert.equal(await key({}), true); assert.equal(submitted, 0);
  vm.runInContext("switchingConversation = false; activeChat = {}", home.context);
  assert.equal(await key({}), true); assert.equal(submitted, 0);
  vm.runInContext("activeChat = null", home.context);
  assert.equal(await key({}), true);
  assert.equal(submitted, 1);
  assert.equal(home.state.chatPayloads.at(-1).text, "合成鍵盤訊息");
  assert.equal(home.state.chatPayloads.at(-1).edit_message_id, undefined, "normal JSON sends omit edit-version fields");
  assert.equal(home.state.input.value, "", "Enter uses the existing successful submission path");
}

function testEditControlsAndCacheVersion() {
  assert.match(homeHtml, /id="message-edit-banner"[^>]*hidden/);
  assert.match(homeHtml, /id="message-edit-status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(homeHtml, /id="cancel-message-edit"[^>]*>取消編輯/);
  const styleVersion = homeHtml.match(/style\.css\?v=(\d+)/)?.[1];
  assert.ok(styleVersion, "the stylesheet link carries a cache version");
  const sw = fs.readFileSync(path.join(__dirname, "..", "app", "static", "sw.js"), "utf8");
  assert.match(sw, new RegExp(`"job-companion-shell-v${styleVersion}"`), "the service-worker shell cache moves with the stylesheet version");
}

function testUserMessageActionFooter() {
  const home = createContext();
  const user = home.context.chatMessage("user", "簡短訊息", "completed", [], [], "2026-10-05T15:00:00Z", "request-1", 101);
  const bubble = user.querySelector(".chat-bubble");
  const footer = user.querySelector(".chat-message-meta");
  assert.ok(bubble && footer);
  assert.equal(user.children[0], bubble, "the user bubble remains its own grid item");
  assert.equal(user.children[1], footer, "time and message actions share one sibling footer");
  assert.equal(bubble.querySelector(".chat-message-actions"), null, "action controls do not affect bubble sizing");
  assert.equal(bubble.querySelector(".chat-time"), null, "user timestamps are outside the bubble");
  assert.equal(footer.children[0].className, "chat-time");
  assert.equal(footer.children[0].dateTime, "2026-10-05T15:00:00.000Z");
  const actionRow = footer.querySelector(".chat-message-actions");
  const actions = actionRow.children;
  assert.deepEqual(actions.map(button => button.className), ["chat-message-edit"]);
  assert.equal(actions[0].textContent, "", "edit is an icon-only control");
  assert.equal(actions[0].getAttribute("aria-label"), "編輯這則訊息");
  assert.equal(actions[0].title, "編輯這則訊息");
  assert.equal(actions[0].children[0].getAttribute("aria-hidden"), "true");
  assert.equal(actions[0].children[0].children[0].getAttribute("href"), "/static/icons.svg#edit");

  const assistant = home.context.chatMessage("assistant", "回答", "completed", [], [], "2026-10-05T15:01:00Z", "request-1", 102);
  assert.equal(assistant.querySelector(".chat-message-meta"), null, "assistant keeps its existing metadata layout");
  assert.equal(assistant.querySelector(".chat-bubble").querySelector(".chat-time").dateTime, "2026-10-05T15:01:00.000Z");
}

async function testSearchResultsPanelAndAutomaticReviews() {
  const home = createContext({intersection: true}); await settle();
  const jobs = [
    {id: "s1", source: "104", title: "後端工程師", company_name: "同公司", salary_text: "月薪 50,000 元", url: "https://jobs.example/s1"},
    {id: "s2", source: "104", title: "平台工程師", company_name: "同公司", salary_text: "面議", url: "https://jobs.example/s2"},
    {id: "s3", source: "518", title: "資料工程師", company_name: "另一公司", salary_text: "月薪 45,000 元", url: "https://jobs.example/s3"},
    {id: "s4", source: "518", title: "測試工程師", company_name: "第三公司", salary_text: "", url: "https://jobs.example/s4"},
  ];
  const message = home.context.chatMessage("assistant", "", "running");
  home.context.renderChatEvent(message, "jobs", {
    search_id: "search-1", criteria: {keyword: "Python", area_codes: ["TPE"]}, sources: ["104", "518"],
    can_continue: true, total_count: 4, new_count: 3, duplicate_count: 1, items: jobs,
    source_results: [{source: "104", status: "success", returned_count: 2}, {source: "518", status: "partial", returned_count: 2}],
  });
  const panel = home.elements.get("search-results-panel");
  const list = home.elements.get("search-results-list");
  assert.equal(panel.hidden, false);
  home.context.renderToolProgress(message, {call_id: "detail", name: "get_job_details", phase: "started"});
  home.context.renderChatEvent(message, "job_detail", {job_id: "s1", identity_verified: false, detail: {summary: "保留在收合進度中的全文"}});
  home.context.renderToolProgress(message, {call_id: "detail", name: "get_job_details", phase: "completed"});
  assert.match(message.toolProgressElement.textContent, /保留在收合進度中的全文/);
  assert.equal(home.elements.get("search-results-summary").textContent, "4 筆 · 新 3", "header summary is the short one-liner built from the server counts");
  assert.equal(home.elements.get("search-results-counts").textContent, "共 4 筆 · 本輪新增 3 · 重複 1 · 還可以繼續找", "the filter dialog keeps the full counts and the can-continue hint");
  assert.equal(all(list, item => item.className === "job-card item").length, 4, "all candidates are browseable in the results panel");
  assert.match(home.elements.get("search-results-details").textContent, /104.*已完成/);
  assert.equal(home.state.intersection.observers.length, 1);
  home.state.intersection.observers[0].emit(); await settle();
  const reviewCalls = home.state.apiCalls.filter(call => call.path === "/api/reviews");
  assert.equal(reviewCalls.length, 3, "same company is reviewed once while visible");
  assert.equal(JSON.parse(reviewCalls[0].options.body).scope, "basic");
  assert.equal(JSON.parse(reviewCalls[0].options.body).job_id, "s1");

  home.context.renderChatEvent(message, "recommendations", {items: [{job_id: "s3", reasons: ["技能符合"], concerns: [], unknowns: []}]});
  const featured = all(list, item => item.className === "job-featured");
  assert.equal(featured.length, 1, "recommendation events pin a candidate");
  assert.match(list.textContent, /適合技能符合/, "fit tags render as labelled rows");

  const salaryControl = all(home.elements.get("search-results-controls"), item => item.tagName === "INPUT")[0];
  salaryControl.checked = true; await salaryControl.dispatch("change");
  assert.equal(all(list, item => item.className === "job-card item").length, 2, "salary filtering stays local to the panel");
  assert.equal(home.state.fetchCalls.some(call => call.path === "/api/chat"), false);

  const target = all(list, item => item.dataset.jobId === "s1")[0];
  await target.notInterestedElement.click();
  assert.ok(home.state.apiCalls.some(call => call.path === "/api/searches/search-1" && JSON.parse(call.options.body).dismissed === true));
  const restored = all(list, item => item.dataset.jobId === "s1")[0];
  assert.equal(restored.descriptionElement.hidden, false, "a dismissed card opens 更多 so the 5-second 復原 stays visible");
  assert.equal(restored.notInterestedElement.textContent, "復原");
  await restored.notInterestedElement.click();
  assert.ok(home.state.apiCalls.some(call => call.path === "/api/searches/search-1" && JSON.parse(call.options.body).dismissed === false));

  // A single assistant reply may contain more than one backend search batch.
  // The default panel keeps every unique candidate reachable while the batch
  // selector preserves each batch's continuation and dismiss context.
  home.context.renderChatEvent(message, "jobs", {
    search_id: "search-2", criteria: {keyword: "Python", area_codes: ["TPE"]}, sources: ["104"],
    can_continue: true, new_ids: ["s5", "s6"], total_count: 2,
    items: [
      {id: "s5", source: "104", title: "資料平台工程師", company_name: "第四公司", salary_text: "月薪 60,000 元", url: "https://jobs.example/s5"},
      {id: "s6", source: "104", title: "雲端工程師", company_name: "第五公司", salary_text: "月薪 65,000 元", url: "https://jobs.example/s6"},
    ],
  });
  assert.equal(panel.dataset.searchId, "__all__");
  assert.equal(all(list, item => item.className === "job-card item").length, 6, "all search batches remain in the aggregate result panel");
  assert.equal(home.elements.get("search-results-summary").textContent, "6 筆 · 新 2");
  assert.match(home.elements.get("search-results-counts").textContent, /^共 6 筆 · 本輪新增 2/);
  const batch = all(home.elements.get("search-results-controls"), item => item.tagName === "SELECT").find(item => item.getAttribute("aria-label") === "搜尋批次");
  assert.ok(batch && batch.children.length === 3, "aggregate and both search batches are selectable");
  batch.value = "search-1"; await batch.dispatch("change");
  assert.equal(panel.dataset.searchId, "search-1");
  assert.equal(all(list, item => item.className === "job-card item").length, 4, "batch selection keeps the original search context");
  batch.value = ""; await batch.dispatch("change");
  assert.equal(panel.dataset.searchId, "__all__");
}

async function testContinueSearchSitsAtListBottom() {
  const home = createContext(); await settle();
  const list = home.elements.get("search-results-list");
  const message = home.context.chatMessage("assistant", "", "running");
  const job = id => ({id, source: "104", title: `T${id}`, company_name: `C${id}`, url: `https://jobs.example/${id}`});
  home.context.renderChatEvent(message, "jobs", {search_id: "c1", criteria: {keyword: "x"}, can_continue: true, total_count: 2, items: [job("c1a"), job("c1b")]});
  const footer = list.children.at(-1);
  assert.equal(footer.className, "continue-footer", "the footer is the last child, after the last card");
  const button = all(footer, item => item.tagName === "BUTTON")[0];
  assert.equal(button.textContent, "繼續找"); assert.equal(button.id, "continue-search");
  assert.match(footer.textContent, /已看 2 筆/, "the seen count is the server total");
  assert.equal(all(home.elements.get("search-results-actions"), item => item.textContent === "繼續找").length, 0, "the filter dialog no longer holds the button");
  assert.deepEqual(all(home.elements.get("search-results-actions"), item => item.tagName === "BUTTON").map(item => item.textContent), ["存成每日提醒"]);

  // Filtering down to nothing keeps the button at the bottom.
  vm.runInContext("hideDismissed = true; newOnly = true", home.context); home.context.renderSearchResultsPanel();
  assert.match(list.textContent, /目前篩選沒有職缺/); assert.equal(list.children.at(-1).className, "continue-footer");
  vm.runInContext("newOnly = false", home.context); home.context.renderSearchResultsPanel();

  // Busy: disabled with progress text, and a stray click sends nothing.
  home.elements.set("continue-search", all(list.children.at(-1), item => item.tagName === "BUTTON")[0]);
  vm.runInContext("activeChat = {}", home.context); home.context.updateConversationControls();
  const busy = home.elements.get("continue-search");
  assert.equal(busy.disabled, true); assert.equal(busy.textContent, "搜尋中…");
  await busy.click();
  assert.equal(vm.runInContext("pendingChatAction", home.context), null, "a busy click does not queue a second continuation");
  vm.runInContext("activeChat = null", home.context); home.context.updateConversationControls();
  assert.equal(busy.disabled, false); assert.equal(busy.textContent, "繼續找");

  // Click reuses the existing continue_search flow.
  let submitted = 0; home.elements.get("chat-form").requestSubmit = () => { submitted++; };
  await busy.click();
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext("pendingChatAction", home.context))), {type: "continue_search", search_id: "c1"});
  assert.equal(submitted, 1);

  // Two batches: the button acts on the batch that can still continue.
  vm.runInContext("pendingChatAction = null", home.context);
  home.context.renderChatEvent(message, "jobs", {search_id: "c2", criteria: {keyword: "x"}, can_continue: false, total_count: 1, items: [job("c2a")]});
  home.context.renderChatEvent(message, "jobs", {search_id: "c1", can_continue: true, items: []});
  await all(list.children.at(-1), item => item.tagName === "BUTTON")[0].click();
  assert.equal(vm.runInContext("pendingChatAction.search_id", home.context), "c1");

  // Ended search: no button, only the hint.
  home.context.renderChatEvent(message, "jobs", {search_id: "c1", can_continue: false, items: []});
  const ended = list.children.at(-1);
  assert.equal(all(ended, item => item.tagName === "BUTTON").length, 0);
  assert.match(ended.textContent, /已沒有更多結果/);
}

async function testBottomNavStateAndFilterDialog() {
  const home = createContext(); await settle();
  const [chat, daily, preferences, favorites] = home.state.navButtons;
  home.context.showPage("favorites");
  assert.deepEqual(home.state.navButtons.map(button => button.getAttribute("aria-pressed")), ["false", "false", "false", "true"]);
  assert.equal(favorites.getAttribute("aria-current"), "page", "the active tab exposes aria-current");
  for (const other of [chat, daily, preferences]) assert.equal(other.getAttribute("aria-current"), undefined, "only one tab is current");
  assert.equal(home.elements.get("favorites").hidden, false);
  assert.equal(home.elements.get("chat").hidden, true);
  home.context.showPage("chat");
  assert.equal(chat.getAttribute("aria-current"), "page");
  assert.equal(favorites.getAttribute("aria-current"), undefined);
  assert.match(homeHtml, /data-page="chat" aria-pressed="true" aria-current="page"/, "initial markup highlights the chat tab");
  assert.equal((homeHtml.match(/data-page="/g) || []).length, 4, "one set of tab buttons serves top and bottom navigation");

  const opener = home.elements.get("open-result-options"); const dialog = home.elements.get("result-options-dialog");
  await opener.click();
  assert.equal(dialog.open, true); assert.equal(dialog.showCount, 1, "filter button opens the native modal dialog");
  await home.elements.get("close-result-options").click();
  assert.equal(dialog.open, false);
  assert.equal(opener.focused, true, "focus returns to the filter button");
  assert.match(homeHtml, /<dialog id="result-options-dialog"[^>]*>[\s\S]*id="search-results-controls"[\s\S]*<\/dialog>/, "sort and filter controls live in the dialog");
  assert.doesNotMatch(homeHtml, /<details class="result-options/);
  await opener.click();
  const message = home.context.chatMessage("assistant", "", "running");
  home.context.renderChatEvent(message, "jobs", {search_id: "s-dialog", criteria: {keyword: "x"}, items: [{id: "d1", source: "104", title: "T", url: "https://jobs.example/d1"}]});
  vm.runInContext('prefillDailyReminder(searchStates.get("s-dialog"))', home.context);
  assert.equal(dialog.open, false, "choosing an action from the dialog closes it");
}

async function testBasicReviewIdentityAndReset() {
  const home = createContext({intersection: true}); await settle();
  const baseApi = home.context.api;
  const requests = [];
  home.context.api = async (path, options = {}) => {
    if (path === "/api/reviews") {
      let resolve;
      const promise = new Promise(yes => { resolve = yes; });
      requests.push({options, resolve});
      return promise;
    }
    return baseApi(path, options);
  };
  const message = home.context.chatMessage("assistant", "", "running");
  home.context.renderChatEvent(message, "jobs", {items: [
    {id: "review-a", source: "104", company_name: "同名公司", company_source_id: "company-1", url: "https://jobs.example/review-a"},
    {id: "review-b", source: "104", company_name: "同名公司", company_source_id: "company-1", url: "https://jobs.example/review-b"},
    {id: "review-c", source: "104", company_name: "同名公司", company_source_id: "company-2", url: "https://jobs.example/review-c"},
    {id: "review-d", source: "104", company_name: "同名公司", url: "https://jobs.example/review-d"},
  ]});
  home.state.intersection.observers.at(-1).emit(); await settle();
  assert.equal(requests.length, 2, "visible reviews keep the two-company concurrency limit");
  assert.deepEqual(requests.map(request => JSON.parse(request.options.body).job_id), ["review-a", "review-c"], "same verified company id deduplicates, while another id remains distinct");
  requests[0].resolve({status: "empty"}); requests[1].resolve({status: "empty"}); await settle();
  assert.equal(requests.length, 3, "a name-only card does not reuse an id-bearing company's request");
  assert.equal(JSON.parse(requests[2].options.body).job_id, "review-d");
  requests[2].resolve({status: "empty"}); await settle();

  const switched = createContext({intersection: true}); await settle();
  const switchedBaseApi = switched.context.api;
  const switchedRequests = [];
  switched.context.api = async (path, options = {}) => {
    if (path === "/api/reviews") {
      let resolve;
      const promise = new Promise(yes => { resolve = yes; });
      switchedRequests.push({options, resolve});
      return promise;
    }
    return switchedBaseApi(path, options);
  };
  const oldMessage = switched.context.chatMessage("assistant", "", "running");
  switched.context.renderChatEvent(oldMessage, "jobs", {items: [{id: "old-review", source: "104", company_name: "會重用公司", company_source_id: "reuse-1", url: "https://jobs.example/old-review"}]});
  switched.state.intersection.observers.at(-1).emit(); await settle();
  assert.equal(switchedRequests.length, 1);
  await switched.context.openConversation({id: "conversation-2", title: "第二段對話"});
  assert.equal(switchedRequests[0].options.signal.aborted, true, "opening another conversation aborts old review requests");
  const freshMessage = switched.context.chatMessage("assistant", "", "running");
  switched.context.renderChatEvent(freshMessage, "jobs", {items: [{id: "new-review", source: "104", company_name: "會重用公司", company_source_id: "reuse-1", url: "https://jobs.example/new-review"}]});
  switched.state.intersection.observers.at(-1).emit(); await settle();
  assert.equal(switchedRequests.length, 2, "the new conversation can review a company still pending in the old one");
  switchedRequests[0].resolve({status: "empty"}); await settle();
  assert.equal(vm.runInContext("basicReviewActive", switched.context), 1, "a late old promise cannot consume the new active slot");
  assert.equal(vm.runInContext("basicReviewPending.size", switched.context), 1, "a late old promise cannot delete the new pending request");
  switched.context.signedOut();
  assert.equal(switchedRequests[1].options.signal.aborted, true, "logout aborts active review requests");
  assert.equal(vm.runInContext("basicReviewActive", switched.context), 0);
  assert.equal(vm.runInContext("basicReviewPending.size", switched.context), 0);
  switchedRequests[1].resolve({status: "empty"}); await settle();
}

async function testScrolledAwayQueuedReviewIsSkipped() {
  const home = createContext({intersection: true}); await settle();
  const baseApi = home.context.api;
  const requests = [];
  home.context.api = async (path, options = {}) => {
    if (path === "/api/reviews") {
      let resolve;
      const promise = new Promise(yes => { resolve = yes; });
      requests.push({options, resolve});
      return promise;
    }
    return baseApi(path, options);
  };
  const message = home.context.chatMessage("assistant", "", "running");
  home.context.renderChatEvent(message, "jobs", {items: [
    {id: "skip-a", source: "104", company_name: "甲公司", company_source_id: "a", url: "https://jobs.example/skip-a"},
    {id: "skip-b", source: "104", company_name: "乙公司", company_source_id: "b", url: "https://jobs.example/skip-b"},
    {id: "skip-c", source: "104", company_name: "丙公司", company_source_id: "c", url: "https://jobs.example/skip-c"},
  ]});
  const observer = home.state.intersection.observers.at(-1);
  observer.emit(); await settle();
  assert.equal(requests.length, 2, "third company waits in the queue");
  const away = observer.targets.filter(target => target.dataset.jobId === "skip-c");
  observer.callback(away.map(target => ({target, isIntersecting: false})));
  requests[0].resolve({status: "empty"}); requests[1].resolve({status: "empty"}); await settle();
  assert.equal(requests.length, 2, "a company scrolled away while queued is not reviewed");
  observer.callback(away.map(target => ({target, isIntersecting: true}))); await settle();
  assert.equal(requests.length, 3, "re-entering the viewport queues it again");
  assert.equal(JSON.parse(requests[2].options.body).job_id, "skip-c");
  requests[2].resolve({status: "empty"}); await settle();
}

function dispatchDocument(home, type, event) {
  for (const listener of home.state.documentListeners[type] || []) listener(event);
}

async function testChatHeadingMenuOpensClosesAndRunsItems() {
  const home = createContext(); await settle();
  const host = home.elements.get("chat-menu"); const toggle = home.elements.get("chat-menu-toggle");
  const panel = home.elements.get("chat-menu-actions"); const transcript = home.elements.get("chat-transcript");
  // The fake DOM is flat by default; nest the menu the way index.html does so contains() sees the toggle inside it.
  host.append(toggle, panel);
  panel.append(home.elements.get("open-conversation-history"), home.elements.get("refresh-messages"));
  toggle.click();
  assert.equal(toggle.getAttribute("aria-expanded"), "true"); assert.equal(host.classList.contains("menu-open"), true);
  dispatchDocument(home, "click", {target: transcript});
  assert.equal(toggle.getAttribute("aria-expanded"), "false", "a click outside the menu closes it");
  assert.equal(host.classList.contains("menu-open"), false);
  toggle.click();
  dispatchDocument(home, "click", {target: toggle});
  assert.equal(toggle.getAttribute("aria-expanded"), "true", "a click on the toggle itself does not close it");
  toggle.focused = false;
  dispatchDocument(home, "keydown", {key: "Escape"});
  assert.equal(toggle.getAttribute("aria-expanded"), "false", "Escape closes the menu");
  assert.equal(toggle.focused, true, "Escape returns focus to the toggle");
  toggle.click();
  const history = home.elements.get("open-conversation-history");
  await history.click(); await settle();
  panel.dispatch("click", {target: history});
  assert.equal(toggle.getAttribute("aria-expanded"), "false", "choosing an item closes the menu");
  assert.equal(home.elements.get("conversation-history-dialog").open, true, "the item's own handler still opens the history dialog");
  toggle.click(); toggle.focused = false;
  const refresh = home.elements.get("refresh-messages");
  const before = home.state.messageRequests.length;
  await refresh.click(); await settle();
  assert.equal(home.state.messageRequests.length > before, true, "the reload item still reloads messages");
  panel.dispatch("click", {target: refresh});
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(toggle.focused, true, "the reload item leaves focus on the toggle, not on a hidden button");
}

function testChatHeadingMarkupMovesLatestButtonOutOfHeading() {
  const heading = homeHtml.slice(homeHtml.indexOf('<div class="chat-heading">'), homeHtml.indexOf('<div class="transcript-wrap">'));
  assert.doesNotMatch(heading, /latest-message/, "the latest-reply button is no longer in the heading row");
  assert.match(heading.slice(heading.indexOf('id="chat-menu-actions"')), /id="open-conversation-history"[\s\S]*id="refresh-messages"/, "both history actions live in the menu panel");
  const wrap = homeHtml.slice(homeHtml.indexOf('<div class="transcript-wrap">'), homeHtml.indexOf('<div class="chat-status-panel">'));
  assert.match(wrap, /id="chat-transcript"[\s\S]*id="latest-message"/, "the latest-reply button floats inside the transcript wrapper");
}

function testSearchTitleIsNotRepeatedWhenUnlabelled() {
  const home = createContext();
  const message = home.context.chatMessage("assistant", "", "running");
  home.context.renderChatEvent(message, "jobs", {search_id: "s-plain", criteria: {}, items: [{id: "p1", source: "104", title: "T", url: "https://jobs.example/p1"}]});
  vm.runInContext("renderSearchResultsPanel()", home.context);
  assert.equal(home.elements.get("search-results-title").textContent, "本次搜尋", "an unlabelled search shows the fallback once");
  const labelled = createContext();
  const labelledMessage = labelled.context.chatMessage("assistant", "", "running");
  labelled.context.renderChatEvent(labelledMessage, "jobs", {search_id: "s-kw", criteria: {keyword: "後端"}, items: [{id: "k1", source: "104", title: "T2", url: "https://jobs.example/k1"}]});
  vm.runInContext("renderSearchResultsPanel()", labelled.context);
  assert.equal(labelled.elements.get("search-results-title").textContent, "本次搜尋 · 後端", "a labelled search keeps the prefix once");
  // Whether a label exists comes from the criteria, not from comparing display text: a keyword that happens to
  // equal the fallback wording is still shown, and an unlabelled batch adds nothing after its number.
  const batches = createContext();
  const batchMessage = batches.context.chatMessage("assistant", "", "running");
  batches.context.renderChatEvent(batchMessage, "jobs", {search_id: "s-none", criteria: {}, items: [{id: "n1", source: "104", title: "T", url: "https://jobs.example/n1"}]});
  batches.context.renderChatEvent(batchMessage, "jobs", {search_id: "s-same", criteria: {keyword: "本次搜尋"}, items: [{id: "m1", source: "104", title: "T3", url: "https://jobs.example/m1"}]});
  vm.runInContext("renderSearchResultsPanel()", batches.context);
  const select = all(batches.elements.get("search-results-controls"), item => item.tagName === "SELECT")[0];
  assert.deepEqual(select.children.map(option => option.textContent), ["全部搜尋結果", "第1批", "第2批 · 本次搜尋"]);
  select.value = "s-same"; vm.runInContext("resultSearchId = 's-same'; renderSearchResultsPanel()", batches.context);
  assert.equal(batches.elements.get("search-results-title").textContent, "本次搜尋 · 本次搜尋", "a keyword equal to the fallback wording is data, not a missing label");
}

const CHAT_WIDTH_KEY = "job-companion-chat-width";

class ThrowingStorage {
  getItem() { throw new Error("storage blocked"); }
  setItem() { throw new Error("storage blocked"); }
  removeItem() { throw new Error("storage blocked"); }
}

async function pressKey(element, key, shiftKey = false) {
  let prevented = false;
  await element.dispatch("keydown", {key, shiftKey, preventDefault() { prevented = true; }});
  return prevented;
}

async function pointerEvent(element, type, pointerId, clientX) {
  let prevented = false;
  await element.dispatch(type, {pointerId, clientX, button: 0, preventDefault() { prevented = true; }});
  return prevented;
}

function fireResize(home) {
  for (const observer of home.state.resizeObservers) observer.callback([]);
}

function flushFrames(home) {
  const pending = [...home.state.frames.values()];
  home.state.frames.clear();
  for (const callback of pending) callback();
}

function chatWidthState(home) {
  const splitter = home.elements.get("panel-resizer");
  return {
    applied: home.elements.get("user-panel").style["--chat-w"] ?? null,
    min: splitter["aria-valuemin"], max: splitter["aria-valuemax"], now: splitter["aria-valuenow"],
  };
}

async function testChatWidthClampsAndReclampsOnResize() {
  const storage = new Storage(); storage.setItem(CHAT_WIDTH_KEY, "900");
  const home = createContext({storage, panelWidth: 1600}); await settle();
  const run = expression => vm.runInContext(expression, home.context);
  assert.equal(run("clampChatWidth(100, 1600)"), 360, "widths below the 360px chat minimum clamp up");
  assert.equal(run("clampChatWidth(5000, 1600)"), 1186, "the maximum leaves 400px for results and the 14px splitter");
  assert.equal(run("clampChatWidth(700, 1600)"), 700, "in-range widths are kept as they are");
  assert.equal(run("chatWidthBounds(900).max"), 486);
  assert.equal(run("chatWidthBounds(700).max"), 360, "a too-narrow panel never pushes the minimum out of range");
  assert.deepEqual(chatWidthState(home), {applied: "900px", min: "360", max: "1186", now: "900"}, "the stored width is restored inside the current range");
  const panel = home.elements.get("user-panel");
  panel.clientWidth = 1000; fireResize(home);
  assert.deepEqual(chatWidthState(home), {applied: "586px", min: "360", max: "586", now: "586"}, "a narrower window re-clamps the applied width to panel minus 414px");
  assert.equal(storage.getItem(CHAT_WIDTH_KEY), "900", "re-clamping does not overwrite the stored preference");
  panel.clientWidth = 700; fireResize(home);
  assert.equal(chatWidthState(home).applied, "360px", "a very narrow window settles at the chat minimum");
  panel.clientWidth = 1600; fireResize(home);
  assert.equal(chatWidthState(home).applied, "900px", "widening the window brings the preferred width back");
}

async function testChatWidthKeyboardAdjustsAndResetRestoresDefault() {
  const home = createContext({panelWidth: 1600}); await settle();
  const splitter = home.elements.get("panel-resizer");
  assert.equal(chatWidthState(home).applied, null, "without a stored choice the CSS default applies");
  assert.equal(await pressKey(splitter, "ArrowRight"), true, "handled arrow keys prevent page scrolling");
  assert.equal(chatWidthState(home).applied, "456px", "ArrowRight widens the chat column by 16px");
  assert.equal(home.storage.getItem(CHAT_WIDTH_KEY), "456", "the keyboard choice is stored");
  await pressKey(splitter, "ArrowLeft", true);
  assert.equal(chatWidthState(home).applied, "392px", "Shift makes the arrow step 64px");
  await pressKey(splitter, "Home");
  assert.deepEqual([chatWidthState(home).applied, chatWidthState(home).now], ["360px", "360"], "Home goes to the minimum");
  await pressKey(splitter, "End");
  assert.deepEqual([chatWidthState(home).applied, chatWidthState(home).now], ["1186px", "1186"], "End goes to the maximum");
  await pressKey(splitter, "ArrowRight");
  assert.equal(chatWidthState(home).applied, "1186px", "steps past the maximum stay clamped");
  assert.equal(await pressKey(splitter, "a"), false, "other keys are left to the browser");
  await pressKey(splitter, "Enter");
  assert.deepEqual(chatWidthState(home), {applied: null, min: "360", max: "1186", now: "440"}, "Enter restores the CSS default width");
  assert.equal(home.storage.getItem(CHAT_WIDTH_KEY), null, "restoring the default clears the stored choice");
  await pressKey(splitter, "ArrowRight");
  await splitter.dispatch("dblclick");
  assert.equal(chatWidthState(home).applied, null, "double-clicking the splitter restores the default width");
  assert.equal(home.storage.getItem(CHAT_WIDTH_KEY), null);
}

async function testChatWidthDragFollowsFramesAndStoresOnRelease() {
  const home = createContext({panelWidth: 1600}); await settle();
  const splitter = home.elements.get("panel-resizer");
  assert.equal(await pointerEvent(splitter, "pointerdown", 7, 500), true, "pressing the splitter prevents text selection");
  await pointerEvent(splitter, "pointermove", 7, 560);
  assert.equal(chatWidthState(home).applied, null, "a move is applied on the next animation frame, not synchronously");
  await pointerEvent(splitter, "pointermove", 7, 620);
  assert.equal(home.state.frames.size, 1, "several moves before a frame share one scheduled update");
  flushFrames(home);
  assert.equal(chatWidthState(home).applied, "560px", "the chat width is the start width plus the pointer delta");
  await pointerEvent(splitter, "pointermove", 8, 900);
  assert.equal(home.state.frames.size, 0, "moves from another pointer are ignored");
  await pointerEvent(splitter, "pointerup", 7, 620);
  assert.equal(home.storage.getItem(CHAT_WIDTH_KEY), "560", "releasing the pointer stores the dragged width");
  await pointerEvent(splitter, "pointermove", 7, 900);
  assert.equal(home.state.frames.size, 0, "moves after release are ignored");
  await pointerEvent(splitter, "pointerdown", 9, 500);
  await pointerEvent(splitter, "pointermove", 9, 5000);
  flushFrames(home);
  await pointerEvent(splitter, "pointerup", 9, 5000);
  assert.equal(chatWidthState(home).applied, "1186px", "dragging past the maximum stops at the maximum");
  assert.equal(home.storage.getItem(CHAT_WIDTH_KEY), "1186");
  await pressKey(splitter, "Enter");
  await pointerEvent(splitter, "pointerdown", 10, 500);
  await pointerEvent(splitter, "pointerup", 10, 500);
  assert.equal(home.storage.getItem(CHAT_WIDTH_KEY), null, "a plain click without movement stores nothing");
  assert.equal(chatWidthState(home).applied, null);
}

async function testChatWidthSurvivesStorageErrors() {
  const home = createContext({storage: new ThrowingStorage(), panelWidth: 1600}); await settle();
  assert.equal(vm.runInContext("chatWidthPreference", home.context), null, "an unreadable stored value falls back to the default");
  const splitter = home.elements.get("panel-resizer");
  assert.equal(await pressKey(splitter, "ArrowRight"), true);
  assert.equal(chatWidthState(home).applied, "456px", "resizing still works when storage throws");
  await pressKey(splitter, "Enter");
  assert.equal(chatWidthState(home).applied, null, "restoring the default still works when storage throws");
  const junk = new Storage(); junk.setItem(CHAT_WIDTH_KEY, "abc");
  const malformed = createContext({storage: junk, panelWidth: 1600}); await settle();
  assert.equal(chatWidthState(malformed).applied, null, "non-numeric stored values are ignored");
}

function testSplitterMarkup() {
  const tag = homeHtml.match(/<div id="panel-resizer"[^>]*><\/div>/)?.[0] || "";
  for (const attribute of ['class="panel-resizer"', 'role="separator"', 'aria-orientation="vertical"', 'aria-label="調整聊天與結果區寬度"', 'tabindex="0"']) {
    assert.ok(tag.includes(attribute), `the splitter carries ${attribute}`);
  }
  assert.ok(homeHtml.indexOf('id="chat"') < homeHtml.indexOf('id="panel-resizer"') && homeHtml.indexOf('id="panel-resizer"') < homeHtml.indexOf('id="search-results-panel"'), "the splitter sits between the chat and results columns");
}

(async () => {
  await testChatDeliveryFeedback();
  await testEditRegenerationInPlace();
  await testCompanionLifecycle();
  await testPushPermissionRebindDisableAndLateLogout();
  await testDraftShortcutsPortraitsAndResume();
  await testAttachmentDropSelectionAndBoundaries();
  await testLinkedRecommendations();
  await testChatScroll();
  await testFavoriteKeepsHistoryAndPaging();
  await testReviewLoadingRetryAndLateResults();
  await testScrolledAwayQueuedReviewIsSkipped();
  await testProposalFocusAndConfirmationBoundary();
  await testConversationLifecycleAndGuards();
  await testPushSummaryStates();
  await testEnterToSendAndShiftEnterNewline();
  testEditControlsAndCacheVersion();
  testUserMessageActionFooter();
  await testSearchResultsPanelAndAutomaticReviews();
  await testBasicReviewIdentityAndReset();
  await testContinueSearchSitsAtListBottom();
  await testBottomNavStateAndFilterDialog();
  await testChatHeadingMenuOpensClosesAndRunsItems();
  testChatHeadingMarkupMovesLatestButtonOutOfHeading();
  testSearchTitleIsNotRepeatedWhenUnlabelled();
  testSplitterMarkup();
  await testChatWidthClampsAndReclampsOnResize();
  await testChatWidthKeyboardAdjustsAndResetRestoresDefault();
  await testChatWidthDragFollowsFramesAndStoresOnRelease();
  await testChatWidthSurvivesStorageErrors();
  console.log("test_m3_home: 29 checks passed");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
