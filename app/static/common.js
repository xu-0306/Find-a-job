"use strict";
let csrfToken = "";
let regionCatalog = [];
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
async function api(path, options = {}) {
  const requestToken = csrfToken;
  const headers = {"Content-Type": "application/json", "X-CSRF-Token": requestToken};
  const response = await fetch(path, {credentials: "same-origin", ...options, headers});
  const result = response.status === 204 ? null : await response.json();
  if (requestToken !== csrfToken) throw new Error("登入狀態已變更，請重新整理。");
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/admin/login" && csrfToken) {
      window.dispatchEvent(new Event("session-expired"));
    }
    const exception = new Error(result?.error?.message || "操作失敗，請稍後再試。");
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
function criteriaFields(criteria = {keyword: "", area_codes: [], min_monthly_salary: null}) {
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
    min_monthly_salary: $("[name=min_monthly_salary]", container).value === "" ? null : Number($("[name=min_monthly_salary]", container).value)};
}
function describe(criteria) {
  return criteria ? `${criteria.keyword} · 地區 ${criteria.area_codes.map(code => regionCatalog.find(item => item.code === code)?.name || code).join("、") || "不限"} · ${criteria.min_monthly_salary === null ? "薪資不限" : `最低月薪 ${criteria.min_monthly_salary} 元`}` : "無條件";
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
