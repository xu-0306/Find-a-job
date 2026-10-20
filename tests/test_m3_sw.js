const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const swSource = fs.readFileSync(path.join(root, "app", "static", "sw.js"), "utf8");
const ORIGIN = "https://job.example";

function response(body, status = 200) {
  return {body, status, ok: status >= 200 && status < 300};
}

function createWorker({fetchImpl = async request => response(`network:${request.url}`), cacheNames = []} = {}) {
  const listeners = new Map();
  const entries = new Map();
  const deleted = [];
  const opened = new Set();
  const networkCalls = [];
  const notifications = [];
  const clients = [];
  const windows = {claimed: false};
  const cacheObject = name => ({
    addAll: async urls => {
      for (const item of urls) entries.set(new URL(item, ORIGIN).href, response(`cached:${item}`));
    },
    match: async request => entries.get(typeof request === "string" ? new URL(request, ORIGIN).href : request.url),
  });
  const caches = {
    open: async name => { opened.add(name); return cacheObject(name); },
    match: async request => entries.get(typeof request === "string" ? new URL(request, ORIGIN).href : request.url),
    keys: async () => [...cacheNames, ...opened],
    delete: async name => { deleted.push(name); return true; },
  };
  const fetchFn = async request => {
    networkCalls.push(request.url);
    return fetchImpl(request);
  };
  const self = {
    location: {origin: ORIGIN},
    registration: {showNotification: async (title, options) => { notifications.push({title, options}); }},
    clients: {
      matchAll: async () => clients,
      claim: async () => { windows.claimed = true; },
      openWindow: async url => { windows.opened = url; return null; },
    },
    skipWaiting: async () => { windows.skipped = true; },
    addEventListener: (type, handler) => listeners.set(type, handler),
  };
  const context = {self, caches, fetch: fetchFn, URL, URLSearchParams, Promise, console};
  vm.createContext(context);
  vm.runInContext(swSource, context, {filename: "sw.js"});
  return {listeners, entries, opened, deleted, networkCalls, notifications, clients, windows,
    dispatch(type, detail = {}) {
      const waits = [];
      const event = {...detail, waitUntil: promise => waits.push(Promise.resolve(promise))};
      const handler = listeners.get(type);
      assert.ok(handler, `missing ${type} listener`);
      handler(event);
      return Promise.all(waits);
    },
    dispatchFetch(request) {
      let responsePromise;
      const event = {request, respondWith: promise => {responsePromise = Promise.resolve(promise);}};
      listeners.get("fetch")(event);
      return responsePromise ? responsePromise.then(value => ({intercepted: true, value})) : Promise.resolve({intercepted: false});
    },
  };
}

const request = (url, options = {}) => ({
  url: new URL(url, ORIGIN).href,
  method: options.method || "GET",
  mode: options.mode || "cors",
});

async function testInstallActivateAndCacheBoundary() {
  const worker = createWorker({cacheNames: ["job-companion-shell-old", "other-app-cache"]});
  await worker.dispatch("install");
  assert.ok([...worker.opened].some(name => name.startsWith("job-companion-shell-v") && name !== "job-companion-shell-old"),"new icon/CSS markup installs into a fresh shell cache");
  assert.ok(worker.entries.has(`${ORIGIN}/static/offline.html`));
  assert.ok(worker.entries.has(`${ORIGIN}/static/icon.svg`));
  assert.ok(worker.entries.has(`${ORIGIN}/static/style.css`));
  assert.equal(worker.entries.has(`${ORIGIN}/`), false, "authenticated root must not be precached");
  await worker.dispatch("activate");
  assert.deepEqual(worker.deleted, ["job-companion-shell-old"]);
  assert.equal(worker.windows.claimed, true);

  const cached = await worker.dispatchFetch(request("/static/offline.html"));
  assert.equal(cached.intercepted, true);
  assert.match(cached.value.body, /cached:\/static\/offline\.html/);
  const queried = await worker.dispatchFetch(request("/static/offline.html?x=1"));
  assert.equal(queried.intercepted, false, "query variants are dynamic and uncached");
  const freshStyle = await worker.dispatchFetch(request("/static/style.css"));
  assert.equal(freshStyle.value.body, `network:${ORIGIN}/static/style.css`, "online CSS must not use a stale precache");
  const versionedStyle = await worker.dispatchFetch(request("/static/style.css?v=next"));
  assert.equal(versionedStyle.intercepted, false, "stylesheet revision bypasses old workers too");
  const offlineWorker = createWorker({fetchImpl: async () => { throw new Error("offline"); }});
  await offlineWorker.dispatch("install");
  const offlineStyle = await offlineWorker.dispatchFetch(request("/static/style.css"));
  assert.match(offlineStyle.value.body, /cached:\/static\/style\.css/, "offline shell keeps its stylesheet");
}

async function testSensitiveAndOriginRequestsBypassWorker() {
  const worker = createWorker();
  for (const item of [
    request("/api/jobs?endpoint=https%3A%2F%2Fsecret.invalid"),
    request("/admin", {mode: "navigate"}),
    request("/access?token=private", {mode: "navigate"}),
    request("/?token=private", {mode: "navigate"}),
    request("/?page=daily&token=private", {mode: "navigate"}),
    request("https://job.example.attacker/?page=daily", {mode: "navigate"}),
    request("/", {method: "POST", mode: "navigate"}),
  ]) {
    const result = await worker.dispatchFetch(item);
    assert.equal(result.intercepted, false, `must bypass ${item.url}`);
  }
  assert.equal(worker.networkCalls.length, 0, "bypassed requests stay with the browser network stack");
}

async function testNavigationFallbackWithoutDynamicCache() {
  const worker = createWorker({fetchImpl: async () => { throw new Error("offline"); }});
  await worker.dispatch("install");
  const home = await worker.dispatchFetch(request("/", {mode: "navigate"}));
  assert.equal(home.intercepted, true);
  assert.match(home.value.body, /cached:\/static\/offline\.html/);
  const daily = await worker.dispatchFetch(request("/?page=daily", {mode: "navigate"}));
  assert.equal(daily.intercepted, true);
  assert.match(daily.value.body, /cached:\/static\/offline\.html/);
  assert.equal(worker.entries.has(`${ORIGIN}/?page=daily`), false, "navigation responses are never cached");
}

async function testPushPayloadAndClickSanitization() {
  const worker = createWorker();
  await worker.dispatch("push", {data: {text: () => JSON.stringify({
    title: "今日新增", body: "有 3 筆新職缺", url: "/?page=daily", endpoint: "do-not-display",
  })}});
  assert.equal(worker.notifications.length, 1);
  assert.equal(worker.notifications[0].title, "今日新增");
  assert.equal(worker.notifications[0].options.data.url, "/?page=daily");
  assert.equal("endpoint" in worker.notifications[0].options, false);

  await worker.dispatch("push", {data: {text: () => "{bad json"}});
  assert.equal(worker.notifications[1].title, "找工作小幫手");
  assert.equal(worker.notifications[1].options.data.url, "/?page=daily");

  await worker.dispatch("push", {data: {text: () => JSON.stringify({
    title: "外站", body: "不應外連", url: "https://job.example.attacker/?page=daily",
  })}});
  assert.equal(worker.notifications[2].options.data.url, "/?page=daily", "same-host prefix attack must fall back");

  const sameOriginWindow = {
    url: `${ORIGIN}/`,
    navigate: async url => { sameOriginWindow.navigated = url; },
    focus: async () => { sameOriginWindow.focused = true; },
  };
  worker.clients.push(sameOriginWindow);
  const notification = {data: {url: "https://job.example.attacker/?page=daily"}, close: () => {notification.closed = true;}};
  await worker.dispatch("notificationclick", {notification});
  assert.equal(notification.closed, true);
  assert.equal(sameOriginWindow.navigated, `${ORIGIN}/?page=daily`);
  assert.equal(sameOriginWindow.focused, true);

  worker.clients.length = 0;
  const foreignWindow = {url: "https://job.example.attacker/", navigate: async () => {throw new Error("must not navigate");}};
  worker.clients.push(foreignWindow);
  const second = {data: {url: "javascript:alert(1)"}, close: () => {}};
  await worker.dispatch("notificationclick", {notification: second});
  assert.equal(worker.windows.opened, `${ORIGIN}/?page=daily`);
}

function testStaticArtifacts() {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "app", "static", "manifest.webmanifest"), "utf8"));
  const worker = fs.readFileSync(path.join(root, "app", "static", "sw.js"), "utf8");
  assert.match(worker, /job-companion-shell-v36/, "per-area UI update uses a fresh service-worker shell cache");
  const index = fs.readFileSync(path.join(root, "app", "static", "index.html"), "utf8");
  assert.match(index, /home\.js\?v=36/, "home script query version matches the UI change");
  const admin = fs.readFileSync(path.join(root, "app", "static", "admin.html"), "utf8");
  for (const [name, html] of [["index", index], ["admin", admin]]) {
    assert.match(html, /common\.js\?v=36/, `${name} uses the same common script version`);
    assert.match(html, /style\.css\?v=36/, `${name} uses the same stylesheet version`);
  }
  assert.equal(manifest.lang, "zh-Hant");
  assert.equal(manifest.start_url, "/");
  assert.equal(manifest.scope, "/");
  assert.equal(manifest.display, "standalone");
  assert.match(manifest.theme_color, /^#/);
  assert.equal(manifest.icons[0].src, "/static/icon.svg");
  assert.equal(manifest.icons[0].sizes, "any");
  const offline = fs.readFileSync(path.join(root, "app", "static", "offline.html"), "utf8");
  assert.match(offline, /目前沒有網路/);
  assert.match(offline, /\/static\/icon\.svg/);
  assert.doesNotMatch(offline, /<script|\/api\/|登入|管理員|職缺卡片/);
  const icon = fs.readFileSync(path.join(root, "app", "static", "icon.svg"), "utf8");
  assert.match(icon, /<svg[^>]+viewBox="0 0 128 128"/);
  assert.doesNotMatch(icon, /<(?:image|script)\b|(?:href|src)=['"]https?:/i);
}

(async () => {
  await testInstallActivateAndCacheBoundary();
  await testSensitiveAndOriginRequestsBypassWorker();
  await testNavigationFallbackWithoutDynamicCache();
  await testPushPayloadAndClickSanitization();
  testStaticArtifacts();
  console.log("M3 PWA service-worker checks passed");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
