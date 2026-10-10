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
vm.runInContext(`${prefix}\nglobalThis.testApi = {criteriaFields, readCriteria, formatTaipeiDateTime, setRegions(items) { regionCatalog = items; }};`, context);
const {criteriaFields, readCriteria, formatTaipeiDateTime, setRegions} = context.testApi;

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

console.log("M3 common criteria and time formatting checks passed");
