"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = Object.create(null);
    this.value = "";
    this.selected = false;
    this.attributes = Object.create(null);
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return (this._text || "") + this.children.map(child => child.textContent).join(""); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this._text = ""; this.children = items; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type) { for (const listener of this.listeners[type] || []) listener({type, currentTarget: this}); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  get selectedOptions() { return this.tagName === "SELECT" ? this.children.filter(option => option.selected) : []; }
  get options() { return this.tagName === "SELECT" ? this.children : []; }
  querySelector(selector) {
    const match = /^\[name=([^\]]+)\]$/.exec(selector);
    if (!match) return null;
    const visit = element => element.name === match[1] ? element : element.children.map(visit).find(Boolean) || null;
    return visit(this);
  }
}

const source = fs.readFileSync(path.join(__dirname, "..", "app", "static", "common.js"), "utf8");
const prefix = source.slice(0, source.indexOf("\nlet theme ="));
const context = {document: {createElement: tag => new Element(tag)}};
vm.createContext(context);
vm.runInContext(`${prefix}\nglobalThis.testApi = {criteriaFields, readCriteria, describe, formatTaipeiDateTime, setRegions(items) { regionCatalog = items; }};`, context);
const {criteriaFields, readCriteria, describe, formatTaipeiDateTime, setRegions} = context.testApi;

function field(root, name) { return root.querySelector(`[name=${name}]`); }
function optionCodes(select) { return select.options.map(option => option.value); }
function selectedCodes(root) { return Array.from(readCriteria(root).area_codes); }
function summary(root) { return root.children.find(child => child.tagName === "DIV").children.find(child => child.tagName === "P"); }

setRegions([
  {code: "TNN", name: "台南市"},
  {code: "KHH", name: "高雄市"},
  {code: "TPE", name: "台北市"},
]);

const searchable = criteriaFields({keyword: "工程師", area_codes: [], min_monthly_salary: null});
const search = searchable.children.find(child => child.tagName === "DIV").children[0].children[0];
const select = field(searchable, "area_codes");
search.value = "台南"; search.dispatch("input");
assert.deepEqual(optionCodes(select), ["TNN"], "name search filters the native select");
assert.deepEqual(selectedCodes(searchable), [], "search text does not select a region");
assert.equal(field(searchable, "remote_only").type, "checkbox");
assert.equal(field(searchable, "remote_only").checked, false);
field(searchable, "remote_only").checked = true;
assert.equal(readCriteria(searchable).remote_only, true);
assert.match(describe({keyword: "工程師", area_codes: [], min_monthly_salary: null, remote_only: true}), /僅遠端/);
assert.doesNotMatch(describe({keyword: "工程師", area_codes: [], min_monthly_salary: null}), /僅遠端/);
search.value = "tpe"; search.dispatch("input");
assert.deepEqual(optionCodes(select), ["TPE"], "code search is case-insensitive");
search.value = "missing"; search.dispatch("input");
assert.match(summary(searchable).textContent, /找不到符合的地區/);

const preserved = criteriaFields({keyword: "x", area_codes: ["TNN", "OLD"], min_monthly_salary: null});
const preservedSearch = preserved.children.find(child => child.tagName === "DIV").children[0].children[0];
const preservedSelect = field(preserved, "area_codes");
assert.deepEqual(optionCodes(preservedSelect), ["TNN", "OLD", "KHH", "TPE"]);
assert.match(preservedSelect.options.find(option => option.value === "OLD").textContent, /待核對/);
preservedSearch.value = "高雄"; preservedSearch.dispatch("input");
assert.deepEqual(optionCodes(preservedSelect), ["TNN", "OLD", "KHH"]);
assert.deepEqual(selectedCodes(preserved), ["TNN", "OLD"], "filtered-out known and unknown selections survive");
assert.match(summary(preserved).textContent, /台南市.*既有代碼 OLD（待核對）/);
preservedSearch.value = ""; preservedSearch.dispatch("input");
assert.deepEqual(optionCodes(preservedSelect), ["TNN", "OLD", "KHH", "TPE"]);
const clear = preserved.children.find(child => child.tagName === "DIV").children[3];
clear.dispatch("click");
assert.deepEqual(selectedCodes(preserved), []);
assert.match(summary(preserved).textContent, /不限 · 已選 0 個地區/);

const first = criteriaFields({keyword: "a", area_codes: ["TNN"], min_monthly_salary: null});
const second = criteriaFields({keyword: "b", area_codes: ["KHH"], min_monthly_salary: null});
const firstSearch = first.children.find(child => child.tagName === "DIV").children[0].children[0];
const secondSelect = field(second, "area_codes");
firstSearch.value = "台北"; firstSearch.dispatch("input");
assert.deepEqual(selectedCodes(first), ["TNN"]);
assert.deepEqual(selectedCodes(second), ["KHH"], "separate forms keep independent selections");
assert.ok(optionCodes(secondSelect).includes("TPE"), "search in one form does not filter another form");

setRegions(Array.from({length: 21}, (_, index) => ({code: `R${index}`, name: `地區${index}`})));
const overLimit = criteriaFields({keyword: "x", area_codes: Array.from({length: 20}, (_, index) => `R${index}`), min_monthly_salary: null});
const overSelect = field(overLimit, "area_codes");
overSelect.options.find(option => option.value === "R20").selected = true;
overSelect.dispatch("change");
assert.match(summary(overLimit).textContent, /已選 21 個地區/);
assert.match(summary(overLimit).textContent, /最多20個/);
assert.match(summary(overLimit).textContent, /移除1個再儲存/);

const options = {timeZone: "Asia/Taipei", dateStyle: "medium", timeStyle: "short", hour12: false};
assert.equal(formatTaipeiDateTime("2026-10-07T17:30:00Z"), new Intl.DateTimeFormat("zh-TW", options).format(new Date("2026-10-07T17:30:00Z")));
const taipeiParts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
}).formatToParts(new Date("2026-10-07T17:30:00Z")).map(part => [part.type, part.value]));
assert.deepEqual([taipeiParts.year, taipeiParts.month, taipeiParts.day, taipeiParts.hour, taipeiParts.minute], ["2026", "10", "08", "01", "30"]);
assert.equal(formatTaipeiDateTime(null), "時間未提供");
assert.equal(formatTaipeiDateTime("not-a-date"), "時間格式無效");

function recoveryContext({user = true} = {}) {
  const events = [];
  const sandbox = {document: {createElement: tag => new Element(tag)}, Event, AbortController, setTimeout, clearTimeout,
    window: {dispatchEvent(event) { events.push(event.type); }}};
  vm.createContext(sandbox);
  vm.runInContext(prefix, sandbox, {filename: "common.js"});
  vm.runInContext('csrfToken = "generation"; csrfHeaderToken = "initial-header";', sandbox);
  if (user) vm.runInContext('enableCsrfRecovery("alice");', sandbox);
  return {sandbox, events, run: script => vm.runInContext(script, sandbox)};
}
const jsonResult = (result, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => result});
const rejectedCsrf = () => jsonResult({error: {code: "csrf_rejected", message: "頁面驗證已失效，請重新整理！"}}, 403);
async function testApiCsrfRecovery() {
  const home = recoveryContext(); const requests = []; let bootstrap = 0;
  home.sandbox.fetch = async (path, options) => {
    if (path === "/api/bootstrap") { bootstrap++; return jsonResult({account: {username: "alice"}, csrf_token: "fresh"}); }
    requests.push({path, options});
    return requests.length === 1 ? rejectedCsrf() : jsonResult({saved: true});
  };
  assert.equal((await home.sandbox.api("/api/favorites/job", {method: "POST", body: "{}"})).saved, true);
  assert.equal(bootstrap, 1);
  assert.deepEqual(requests.map(item => item.options.headers["X-CSRF-Token"]), ["initial-header", "fresh"]);
  assert.equal(requests[0].options.body, requests[1].options.body);
  assert.equal(home.run("csrfToken"), "generation");
  assert.equal(home.run("csrfHeaderToken"), "fresh");
}
async function testApiCsrfFailuresAndBoundaries() {
  for (const scenario of ["account", "case", "missing-account", "missing-token", "network", "http", "bootstrap-json", "retry-limit"]) {
    const home = recoveryContext(); let writes = 0; let bootstraps = 0;
    home.sandbox.fetch = async path => {
      if (path !== "/api/bootstrap") {
        writes++;
        if (scenario === "retry-limit") assert.ok(writes <= 2, "retry-limit: a third write must never reach fetch");
        return rejectedCsrf();
      }
      bootstraps++;
      if (scenario === "network") throw new TypeError("offline");
      if (scenario === "http") return jsonResult({}, 503);
      if (scenario === "bootstrap-json") return {ok: true, json: async () => { throw new SyntaxError("bad bootstrap"); }};
      return jsonResult({account: scenario === "missing-account" ? null : {username: scenario === "account" ? "bob" : scenario === "case" ? "Alice" : "alice"}, csrf_token: scenario === "missing-token" ? "" : "fresh"});
    };
    await assert.rejects(home.sandbox.api("/api/preferences", {method: "PATCH", body: "{}"}), error => {
      if (scenario === "retry-limit") assert.equal(writes, 2, "retry-limit: only one replay is allowed");
      assert.equal(error.status, 403);
      const expected = ["account", "case", "retry-limit"].includes(scenario)
        ? "登入狀態已變更，請重新整理。"
        : "暫時無法確認登入狀態，請稍後再試。";
      assert.equal(error.message, expected);
      return true;
    });
    assert.equal(writes, scenario === "retry-limit" ? 2 : 1, scenario);
    assert.equal(bootstraps, 1);
    assert.deepEqual(home.events, []);
    assert.equal(home.run("csrfToken"), "generation");
  }
  for (const scenario of ["admin", "other-403", "401", "get", "admin-login-401"]) {
    const home = recoveryContext({user: !scenario.startsWith("admin")}); const requests = [];
    // Admin sessions set only csrfToken, just as admin.js does.
    if (scenario.startsWith("admin")) home.run('csrfHeaderToken = "";');
    const status = scenario.includes("401") ? 401 : 403;
    home.sandbox.fetch = async (path, options) => {
      requests.push({path, options});
      return jsonResult({error: {code: scenario === "other-403" ? "forbidden" : "csrf_rejected", message: "原有訊息"}}, status);
    };
    await assert.rejects(home.sandbox.api(scenario === "admin-login-401" ? "/api/admin/login" : "/api/settings", {method: scenario === "get" ? "GET" : "POST"}), error => error.message === "原有訊息" && error.status === status);
    assert.equal(requests.length, 1, scenario + " never requests a recovery bootstrap");
    assert.equal(requests[0].options.headers["X-CSRF-Token"], scenario.startsWith("admin") ? "generation" : "initial-header");
    assert.deepEqual(home.events, scenario === "401" ? ["session-expired"] : []);
  }
}
async function testApiRecoveryCancellationAndLogout() {
  for (const change of ["abort", "logout", "generation", "bootstrap-401"]) {
    const home = recoveryContext(); let finish; let writes = 0;
    const pending = new Promise(resolve => { finish = resolve; });
    const controller = new AbortController();
    home.sandbox.fetch = async path => {
      if (path === "/api/bootstrap") return pending;
      writes++; return rejectedCsrf();
    };
    const request = home.sandbox.api("/api/preferences", {method: "POST", signal: controller.signal});
    for (let i = 0; i < 8; i++) await Promise.resolve();
    if (change === "abort") {
      controller.abort();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    }
    if (change === "logout") home.run('disableCsrfRecovery(); csrfToken = ""; csrfHeaderToken = "";');
    if (change === "generation") home.run('csrfToken = "new-generation";');
    if (change !== "abort") finish(change === "bootstrap-401" ? jsonResult({}, 401) : jsonResult({account: {username: "alice"}, csrf_token: "fresh"}));
    await assert.rejects(request, error => change !== "abort" || error.name === "AbortError");
    if (change === "abort") finish(jsonResult({account: {username: "alice"}, csrf_token: "fresh"}));
    assert.equal(writes, 1, change + " prevents a replay");
    if (change === "bootstrap-401") assert.deepEqual(home.events, ["session-expired"]);
    if (change === "generation") assert.equal((await request.catch(error => error)).message, "登入狀態已變更，請重新整理。", "generation reports the login-state change");
  }
}
async function testApiRecoveryPromiseSurvivesDisableAndReenable() {
  const home = recoveryContext(); let resolveOld, resolveNew; let bootstraps = 0; let writes = 0;
  const oldBootstrap = new Promise(resolve => { resolveOld = resolve; });
  const newBootstrap = new Promise(resolve => { resolveNew = resolve; });
  home.sandbox.fetch = async (path, options) => {
    if (path === "/api/bootstrap") { bootstraps++; return bootstraps === 1 ? oldBootstrap : newBootstrap; }
    writes++; return writes <= 3 ? rejectedCsrf() : jsonResult({saved: true});
  };
  const oldRequest = home.sandbox.api("/api/old", {method: "POST"});
  for (let i = 0; i < 8; i++) await Promise.resolve();
  home.run('disableCsrfRecovery(); enableCsrfRecovery("alice"); csrfToken = "generation-2";');
  const newRequest = home.sandbox.api("/api/new", {method: "POST"});
  for (let i = 0; i < 8; i++) await Promise.resolve();
  resolveOld(jsonResult({account: {username: "alice"}, csrf_token: "old"}));
  await assert.rejects(oldRequest);
  const joinedRequest = home.sandbox.api("/api/joined", {method: "POST"});
  for (let i = 0; i < 8; i++) await Promise.resolve();
  assert.equal(bootstraps, 2, "the second caller joins the new shared bootstrap");
  resolveNew(jsonResult({account: {username: "alice"}, csrf_token: "fresh"}));
  await Promise.all([newRequest, joinedRequest]);
  assert.equal(bootstraps, 2);
}
async function testApiRecoveryTimeout() {
  const home = recoveryContext();
  let timeout;
  home.sandbox.setTimeout = (callback, delay) => { timeout = delay; callback(); return 1; };
  home.sandbox.clearTimeout = () => {};
  home.sandbox.fetch = async path => path === "/api/bootstrap" ? new Promise(() => {}) : rejectedCsrf();
  await assert.rejects(home.sandbox.api("/api/preferences", {method: "POST"}), error => {
    assert.equal(error.message, "暫時無法確認登入狀態，請稍後再試。");
    return true;
  });
  assert.equal(timeout, 10000);
}
async function testApiRecoveryCallerCancellationDoesNotCancelSharedRequest() {
  const home = recoveryContext(); let finish; let rejectBootstrap; let writes = 0;
  const pending = new Promise((resolve, reject) => { finish = resolve; rejectBootstrap = reject; });
  home.sandbox.fetch = async (path, options = {}) => {
    if (path === "/api/bootstrap") {
      options.signal?.addEventListener("abort", () => { const error = new Error("請求已取消。"); error.name = "AbortError"; rejectBootstrap(error); }, {once: true});
      return pending;
    }
    writes++;
    return writes <= 2 ? rejectedCsrf() : jsonResult({saved: true});
  };
  const firstController = new AbortController();
  const first = home.sandbox.api("/api/one", {method: "POST", signal: firstController.signal});
  const second = home.sandbox.api("/api/two", {method: "POST"});
  for (let i = 0; i < 8; i++) await Promise.resolve();
  firstController.abort();
  await assert.rejects(first, error => error.name === "AbortError");
  finish(jsonResult({account: {username: "alice"}, csrf_token: "fresh"}));
  assert.deepEqual(await second, {saved: true});
  assert.equal(writes, 3);
}

let testRunCompleted = false;
process.on("exit", () => {
  if (!testRunCompleted && !process.exitCode) {
    console.error("M3 common tests ended before completion");
    process.exitCode = 1;
  }
});

(async () => {
  await testApiCsrfRecovery();
  await testApiCsrfFailuresAndBoundaries();
  await testApiRecoveryCancellationAndLogout();
  await testApiRecoveryTimeout();
  await testApiRecoveryCallerCancellationDoesNotCancelSharedRequest();
  await testApiRecoveryPromiseSurvivesDisableAndReenable();
  console.log("M3 common criteria and time formatting checks passed");
  testRunCompleted = true;
  console.log("M3 common CSRF recovery checks passed (6 groups)");
})().catch(error => { console.error(error); process.exitCode = 1; });
