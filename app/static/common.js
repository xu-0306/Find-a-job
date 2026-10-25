"use strict";
let csrfToken = "";
let csrfHeaderToken = "";
let csrfRecoveryEnabled = false;
let csrfRecoveryAccount = "";
let csrfRecoveryAccountChanged = null;
let csrfRecoveryPromise = null;
let regionCatalog = [];
const LOGIN_CHANGED_MESSAGE = "登入狀態已變更，請重新整理。";
const CSRF_RECOVERY_UNAVAILABLE_MESSAGE = "暫時無法確認登入狀態，請稍後再試。";
const CSRF_RECOVERY_TIMEOUT_MS = 10000;
const CSRF_RECOVERY_RESULT = Object.freeze({success: "success", accountChanged: "account_changed", unavailable: "unavailable", aborted: "aborted"});
function enableCsrfRecovery(username, onAccountChanged = null) {
  csrfRecoveryAccountChanged = onAccountChanged;
  csrfRecoveryEnabled = true;
  csrfRecoveryAccount = typeof username === "string" ? username : "";
}
function disableCsrfRecovery() {
  csrfRecoveryEnabled = false;
  csrfRecoveryAccountChanged = null;
  csrfRecoveryAccount = "";
  csrfRecoveryPromise = null;
}
function csrfAbortError() {
  const error = new Error("請求已取消。");
  error.name = "AbortError";
  return error;
}
function recoverCsrfToken(requestToken, signal) {
  if (signal?.aborted) return Promise.resolve(CSRF_RECOVERY_RESULT.aborted);
  if (!csrfRecoveryEnabled || !csrfToken || requestToken !== csrfToken) return Promise.resolve(CSRF_RECOVERY_RESULT.unavailable);
  const generation = csrfToken;
  if (!csrfRecoveryPromise) {
    const recovery = (async () => {
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      let timeout = null;
      try {
        const response = await Promise.race([
          fetch("/api/bootstrap", {method: "GET", credentials: "same-origin", ...(controller ? {signal: controller.signal} : {})}),
          new Promise((_, reject) => { timeout = setTimeout(() => { controller?.abort(); reject(new Error("csrf recovery timeout")); }, CSRF_RECOVERY_TIMEOUT_MS); }),
        ]);
        if (response.status === 401) {
          if (csrfToken === generation) window.dispatchEvent(new Event("session-expired"));
          return CSRF_RECOVERY_RESULT.unavailable;
        }
        if (!response.ok) return CSRF_RECOVERY_RESULT.unavailable;
        let bootstrap;
        try { bootstrap = await response.json(); } catch (_) { return CSRF_RECOVERY_RESULT.unavailable; }
        if (!csrfRecoveryEnabled || csrfToken !== generation) return CSRF_RECOVERY_RESULT.unavailable;
        const account = typeof bootstrap?.account?.username === "string" ? bootstrap.account.username : "";
        const token = typeof bootstrap?.csrf_token === "string" ? bootstrap.csrf_token : "";
        if (!csrfRecoveryAccount || !account || !token) return CSRF_RECOVERY_RESULT.unavailable;
        if (account !== csrfRecoveryAccount) {
          csrfRecoveryAccountChanged?.();
          return CSRF_RECOVERY_RESULT.accountChanged;
        }
        csrfHeaderToken = token;
        return CSRF_RECOVERY_RESULT.success;
      } catch (_) {
        return CSRF_RECOVERY_RESULT.unavailable;
      } finally {
        if (timeout !== null) clearTimeout(timeout);
        controller?.abort();
      }
    })();
    let sharedPromise;
    sharedPromise = recovery.finally(() => { if (csrfRecoveryPromise === sharedPromise) csrfRecoveryPromise = null; });
    csrfRecoveryPromise = sharedPromise;
  }
  if (!signal) return csrfRecoveryPromise;
  let onAbort;
  const caller = new Promise(resolve => {
    onAbort = () => resolve(CSRF_RECOVERY_RESULT.aborted);
    signal.addEventListener("abort", onAbort, {once: true});
  });
  return Promise.race([csrfRecoveryPromise, caller]).finally(() => signal.removeEventListener("abort", onAbort));
}
const $ = (selector, parent = document) => parent.querySelector(selector);
const secretKeys = ["llm_api_key", "brave_api_key", "tavily_api_key"];
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function message(text, failed = false) {
  $("#message").textContent = text;
  $("#message").classList.toggle("error", failed);
}
async function api(path, options = {}, recoveryAttempted = false) {
  const requestToken = csrfToken;
  const method = String(options.method || "GET").toUpperCase();
  const headers = {"Content-Type": "application/json", "X-CSRF-Token": csrfHeaderToken || requestToken};
  const response = await fetch(path, {credentials: "same-origin", ...options, headers});
  const result = response.status === 204 ? null : await response.json();
  if (requestToken !== csrfToken) throw new Error(LOGIN_CHANGED_MESSAGE);
  const userCsrfRejected = csrfRecoveryEnabled && method !== "GET" && response.status === 403 && result?.error?.code === "csrf_rejected";
  let recoveryResult = CSRF_RECOVERY_RESULT.unavailable;
  if (userCsrfRejected && !recoveryAttempted) {
    recoveryResult = await recoverCsrfToken(requestToken, options.signal);
    if (recoveryResult === CSRF_RECOVERY_RESULT.aborted) throw csrfAbortError();
    if (requestToken !== csrfToken) throw new Error(LOGIN_CHANGED_MESSAGE);
    if (recoveryResult === CSRF_RECOVERY_RESULT.success) return api(path, options, true);
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/admin/login" && csrfToken) {
      window.dispatchEvent(new Event("session-expired"));
    }
    const defaultErrorMessage = result?.error?.message || "操作失敗，請稍後再試。";
    let errorMessage = defaultErrorMessage;
    if (userCsrfRejected) {
      if (recoveryAttempted || recoveryResult === CSRF_RECOVERY_RESULT.accountChanged) errorMessage = LOGIN_CHANGED_MESSAGE;
      else if (recoveryResult === CSRF_RECOVERY_RESULT.unavailable) errorMessage = CSRF_RECOVERY_UNAVAILABLE_MESSAGE;
    }
    const exception = new Error(errorMessage);
    exception.status = response.status;
    throw exception;
  }
  return result;
}
function action(button, handler) {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await handler(); } catch (error) { message(error.message, true); }
    finally { button.disabled = false; }
  });
}
function formAction(form, handler) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("button[type=submit]", form);
    button.disabled = true;
    try { await handler(); } catch (error) { message(error.message, true); }
    finally { button.disabled = false; }
  });
}
function criteriaFields(criteria = {keyword: "", area_codes: [], min_monthly_salary: null, remote_only: false}) {
  const fields = node("div", undefined, "grid");
  for (const [name, title, value] of [["keyword", "工作關鍵字", criteria.keyword],
    ["min_monthly_salary", "最低月薪（元；空白不限）", criteria.min_monthly_salary ?? ""]]) {
    const label = node("label", title);
    const input = node("input");
    input.name = name; input.value = value;
    input.type = name === "min_monthly_salary" ? "number" : "text";
    if (name === "keyword") { input.required = true; input.maxLength = 100; }
    if (name === "area_codes") input.maxLength = 419;
    if (name === "min_monthly_salary") { input.min = 0; input.max = 10000000; input.step = 1; }
    label.append(input); fields.append(label);
  }
  const remoteLabel = node("label", "僅遠端");
  const remoteInput = node("input"); remoteInput.type = "checkbox"; remoteInput.name = "remote_only"; remoteInput.checked = criteria.remote_only === true;
  remoteLabel.append(remoteInput); fields.append(remoteLabel);
  const regions = node("div", undefined, "stack");
  const searchLabel = node("label", "搜尋地區名稱或代碼");
  const search = node("input"); search.type = "search"; search.placeholder = "輸入地區名稱或代碼";
  searchLabel.append(search);
  const label = node("label", "地區（可複選；不選表示不限）");
  const select = node("select"); select.name = "area_codes"; select.multiple = true; select.size = 5;
  const selectedCodes = () => [...select.selectedOptions].map(option => option.value);
  const selectedNames = () => selectedCodes().map(code => regionCatalog.find(item => item.code === code)?.name || `既有代碼 ${code}（待核對）`);
  const summary = node("p", undefined, "hint"); summary.setAttribute("aria-live", "polite");
  const clear = node("button", "清除選取"); clear.type = "button";
  function matchingRegions() {
    const query = search.value.trim().toLowerCase();
    return regionCatalog.filter(item => `${item.name} ${item.code}`.toLowerCase().includes(query));
  }
  function updateSummary(matches = matchingRegions()) {
    const selected = selectedNames();
    const count = selected.length;
    summary.textContent = count ? `已選 ${count} 個地區：${selected.join("、")}` : "不限 · 已選 0 個地區";
    if (count > 20) summary.textContent += `；最多20個，請移除${count - 20}個再儲存。`;
    if (!matches.length) summary.textContent += "；找不到符合的地區。";
  }
  function renderRegions(selected = selectedCodes()) {
    const selectedSet = new Set(selected);
    const matches = matchingRegions();
    const items = [
      ...selected.map(code => regionCatalog.find(item => item.code === code) || {code, name: `既有代碼 ${code}（待核對）`}),
      ...matches.filter(item => !selectedSet.has(item.code)),
    ];
    select.replaceChildren(...items.map(item => {
      const option = node("option", item.name); option.value = item.code; option.selected = selectedSet.has(item.code); return option;
    }));
    updateSummary(matches);
  }
  label.append(select);
  regions.append(searchLabel, label, summary, clear);
  fields.append(regions);
  search.addEventListener("input", () => renderRegions());
  select.addEventListener("change", () => updateSummary());
  clear.addEventListener("click", () => {
    for (const option of select.options) option.selected = false;
    updateSummary();
  });
  renderRegions(criteria.area_codes);
  return fields;
}
function readCriteria(container) {
  return {keyword: $("[name=keyword]", container).value.trim(),
    area_codes: [...$("[name=area_codes]", container).selectedOptions].map(option => option.value),
    min_monthly_salary: $("[name=min_monthly_salary]", container).value === "" ? null : Number($("[name=min_monthly_salary]", container).value),
    remote_only: $("[name=remote_only]", container).checked};
}
function describe(criteria) {
  return criteria ? `${criteria.keyword} · 地區 ${criteria.area_codes.map(code => regionCatalog.find(item => item.code === code)?.name || code).join("、") || "不限"} · ${criteria.min_monthly_salary === null ? "薪資不限" : `最低月薪 ${criteria.min_monthly_salary} 元`}${criteria.remote_only ? " · 僅遠端" : ""}` : "無條件";
}
function formatTaipeiDateTime(value) {
  if (value == null || value === "") return "時間未提供";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "時間格式無效";
  return new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei", dateStyle: "medium", timeStyle: "short", hour12: false,
  }).format(date);
}
let theme = "a";
try { theme = localStorage.getItem("theme") === "b" ? "b" : "a"; } catch (_) {}
function applyTheme() {
  document.documentElement.dataset.theme = theme;
  $("#theme").textContent = theme === "b" ? "外觀 B · 切換 A" : "外觀 A · 切換 B";
}
applyTheme();
$("#theme").addEventListener("click", () => {
  theme = theme === "a" ? "b" : "a";
  try { localStorage.setItem("theme", theme); } catch (_) {}
  applyTheme();
});
