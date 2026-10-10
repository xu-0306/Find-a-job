"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.listeners = Object.create(null);
    this.className = "";
    this._text = "";
    this.disabled = false;
  }

  set textContent(value) { this._text = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  get hidden() { return this._hidden === true; }
  set hidden(value) { this._hidden = Boolean(value); }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  click() {
    if (!this.disabled) for (const listener of this.listeners.click || []) listener({currentTarget: this});
  }
}

function findAll(root, predicate) {
  return [root, ...root.children.flatMap((child) => findAll(child, predicate))].filter(predicate);
}

global.window = global;
global.node = (tag, text, className) => {
  const item = new FakeElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
};
const JobCards = require(path.join(__dirname, "..", "app", "static", "cards.js"));

const job = {
  id: "job-1",
  source: "104",
  source_job_id: "104-1",
  title: "<script>alert(1)</script>",
  company_name: "<b>公司</b>",
  company_source_id: null,
  location: null,
  salary_text: null,
  url: "javascript:alert(1)",
  published_at: null,
  fetched_at: "2026-10-05T00:00:00Z",
  summary: null,
  filter_warnings: ["地區篩選可能被來源調整"],
  is_favorite: false
};
const favoriteCalls = [];
const reviewCalls = [];
const jobElement = JobCards.jobCard(job, {
  onFavorite: (value, button) => favoriteCalls.push([value, button]),
  onReview: (value) => reviewCalls.push(value)
});
assert.equal(jobElement.className, "job-card item");
assert.equal(jobElement.dataset.jobId, "job-1");
assert(jobElement.textContent.includes("<script>alert(1)</script>"));
assert(jobElement.textContent.includes("未提供日期"));
assert(jobElement.textContent.includes("未提供"));
assert(jobElement.textContent.includes("地區篩選可能被來源調整"));
const jobButtons = findAll(jobElement, (item) => item.tagName === "BUTTON");
const jobLinks = findAll(jobElement, (item) => item.tagName === "A");
assert.equal(jobButtons.length, 3, "favorite, more and company-review buttons; no 不感興趣 without a handler");
assert.equal(jobElement.notInterestedElement, undefined);
assert.equal(jobButtons[0].type, "button");
assert.equal(jobButtons[0].getAttribute("aria-pressed"), "false");
assert.equal(jobButtons[0].textContent, "收藏");
assert.equal(jobLinks[0].getAttribute("href"), undefined);
assert.match(jobElement.reviewSummaryElement.textContent, /查詢中/);
jobButtons[0].click();
jobButtons[2].click();
assert.deepEqual(favoriteCalls, [[job, jobButtons[0]]]);
jobButtons[0].setAttribute("aria-pressed", "true");
jobButtons[0].listeners["favorite-change"][0]();
assert.equal(job.is_favorite, true, "local callbacks retain the synchronized favorite value");
assert.deepEqual(reviewCalls, [job]);

const scored = JobCards.jobCard({...job, basic_review: {status: "success", ratings: [{source: "GoodJob", metric: "overall", value: 4.2, scale_min: 1, scale_max: 5, sample_count: 17, url: "https://goodjob.example/company", fetched_at: "2026-10-09T00:00:00Z"}], evidence: [{source: "Qollie", title: "面試經驗", date: "2026-10-08", url: "https://interview.example/evidence"}]}}, {
  onFavorite: () => {}, onReview: () => {}, onNotInterested: () => {},
});
assert.match(scored.reviewSummaryElement.textContent, /★4\.2 · 17 人 GoodJob/, "header chip shows the single source score, no average");
assert.equal(findAll(scored.reviewSummaryElement, item => item.className === "job-score")[0].getAttribute("aria-label"), "GoodJob 工作評價 4.2／1–5（17 人）");
assert.match(scored.reviewDetailsElement.textContent, /取得於 2026-10-09T00:00:00Z/);
assert.match(scored.reviewDetailsElement.textContent, /面試經驗.*Qollie.*2026-10-08/);
assert.equal(findAll(scored.reviewDetailsElement, item => item.tagName === "A")[0].getAttribute("href"), "https://goodjob.example/company");
assert.equal(findAll(scored.reviewDetailsElement, item => item.tagName === "A")[1].getAttribute("href"), "https://interview.example/evidence");
const nullSample = JobCards.jobCard({...job, basic_review: {status: "success", ratings: [{source: "GoodJob", metric: "overall", value: 4, sample_count: null}]}}, {onFavorite: () => {}, onReview: () => {}});
assert.doesNotMatch(nullSample.reviewSummaryElement.textContent, /0 人/);
assert.ok(scored.notInterestedElement, "result cards expose an unsuitable-job action");
assert.ok(findAll(scored.descriptionElement, item => item === scored.notInterestedElement).length, "不感興趣 lives inside the 更多 region");
assert.ok(findAll(scored.descriptionElement, item => item === scored.reviewDetailsElement).length, "review evidence lives inside the 更多 region");
assert.equal(scored.descriptionElement.hidden, true);
assert.equal(scored.moreElement.getAttribute("aria-expanded"), "false");
scored.moreElement.click();
assert.equal(scored.descriptionElement.hidden, false);
assert.equal(scored.moreElement.getAttribute("aria-expanded"), "true");
scored.setMoreOpen(false);
assert.equal(scored.descriptionElement.hidden, true);
assert.deepEqual(scored.actionsElement.children.map(item => item.tagName), ["BUTTON", "A", "BUTTON"], "footer has favorite, original link and more");
assert.equal(scored.actionsElement.children[0].getAttribute("aria-pressed"), "true", "favorite reflects the synchronized job state");
assert.equal(scored.footerElement.children[0], scored.actionsElement, "the footer wraps the actions");
const floatingNote = scored.footerElement.children[1];
assert.equal(floatingNote.className, "card-feedback", "feedback lives in the footer so CSS can float it above the action bar");
assert.equal(floatingNote.getAttribute("role"), "status");
assert.equal(floatingNote.textContent, "");
assert.ok(scored.children.indexOf(scored.footerElement) < scored.children.indexOf(scored.descriptionElement), "footer stays above the expandable details");
assert.equal(scored.topElement.children[0].textContent, "104");
const fit = JobCards.fitList({reasons: ["技能符合"], concerns: ["通勤遠", "需輪班"], unknowns: []});
assert.deepEqual(fit.children.map(row => row.textContent), ["適合技能符合", "注意通勤遠、需輪班"], "fit tags: empty groups are omitted and nothing is truncated");
assert.equal(JobCards.fitList({reasons: [], concerns: [], unknowns: []}), null);
for (const [status, text] of [[undefined, "評價查詢中…"], ["ambiguous", "公司待確認"], ["empty", "尚無公開評分"], ["failed", "評價暫時無法取得"]]) {
  const state = JobCards.jobCard({...job, basic_review: status ? {status, ratings: []} : null}, {});
  assert.equal(state.reviewSummaryElement.textContent, text);
  assert.doesNotMatch(state.reviewSummaryElement.textContent, /★/);
}
// No score from any source: one short merged header state; per-source wording moves into the 更多 review details.
const sourcesOf = (...statuses) => ({status: "partial", ratings: [], source_results: statuses.map((status, index) => ({source: ["104", "GoodJob", "Qollie"][index], status}))});
for (const [statuses, text] of [
  [["empty", "empty"], "尚無公開評分"],
  [["empty", "success"], "尚無公開評分"],
  [["empty", "pending"], "評價查詢中…"],
  [["failed", "pending"], "評價查詢中…"],
  [["empty", "failed"], "評價暫時無法取得"],
  [["failed", "ambiguous", "pending"], "公司待確認"],
]) {
  const merged = JobCards.jobCard({...job, basic_review: sourcesOf(...statuses)}, {});
  assert.equal(merged.reviewSummaryElement.textContent, text, statuses.join("+"));
  assert.doesNotMatch(merged.reviewSummaryElement.textContent, /104|GoodJob|★/);
  assert.equal(merged.reviewSummaryElement.children.length, 0);
}
const separate = JobCards.jobCard({...job, basic_review: sourcesOf("empty", "empty")}, {});
assert.match(separate.reviewDetailsElement.textContent, /104 尚未找到公開評分/);
assert.match(separate.reviewDetailsElement.textContent, /GoodJob 尚未找到公開評分/);
assert.equal(separate.reviewDetailsElement.hidden, false);
assert.match(JobCards.jobCard({...job, basic_review: sourcesOf("empty", "pending")}, {}).reviewDetailsElement.textContent, /GoodJob 查詢中…/);
const oneScore = JobCards.jobCard({...job, basic_review: {...sourcesOf("success", "empty"), ratings: [{source: "104", metric: "overall", value: 3.6, scale_max: 5, sample_count: 12}]}}, {});
assert.match(oneScore.reviewSummaryElement.textContent, /★3\.6 · 12 人 104/, "a scored source keeps the chip; the other source stays only in details");
assert.doesNotMatch(oneScore.reviewSummaryElement.textContent, /尚無公開評分/);
assert.match(oneScore.reviewDetailsElement.textContent, /GoodJob 尚未找到公開評分/);

const disabledJob = {...job, is_favorite: true, url: "https://jobs.example.test/1"};
const disabledElement = JobCards.jobCard(disabledJob);
const disabledButtons = findAll(disabledElement, (item) => item.tagName === "BUTTON");
assert.equal(disabledButtons[0].textContent, "取消收藏");
assert.equal(disabledButtons[0].getAttribute("aria-pressed"), "true");
assert.equal(disabledButtons[0].disabled, true);
assert.equal(disabledButtons[1].disabled, false, "更多 stays usable");
assert.equal(disabledButtons[2].disabled, true);
assert.equal(findAll(disabledElement, (item) => item.tagName === "A")[0].rel, "noopener noreferrer");

const review = {
  company_name: "測試公司",
  summary: "可核對的摘要",
  evidence: [
    {source: "GoodJob", title: "工作心得", url: "https://goodjob.example/evidence", date: null, excerpt: "<em>原文摘錄</em>"},
    {source: "Qollie", title: "面試經驗", url: "https://interview.example/evidence", date: "2026-10-01", excerpt: "第二來源"}
  ],
  unavailable_sources: ["PTT"],
  fetched_at: "2026-10-05T00:00:00Z",
  cached: false
};
const reviewElement = JobCards.reviewCard(review);
assert.equal(reviewElement.className, "review-card item");
assert(reviewElement.textContent.includes("GoodJob"));
assert(reviewElement.textContent.includes("Qollie"));
assert(reviewElement.textContent.includes("未提供日期"));
assert(reviewElement.textContent.includes("暫時無法查詢的來源"));
assert(reviewElement.textContent.includes("PTT"));
const reviewLinks = findAll(reviewElement, (item) => item.tagName === "A");
assert.equal(reviewLinks.length, 2);
assert.equal(reviewLinks[0].getAttribute("href"), "https://goodjob.example/evidence");
assert.equal(reviewLinks[0].rel, "noopener noreferrer");

assert(JobCards.reviewCard({company_name: "空結果", summary: "", evidence: [], unavailable_sources: []}).textContent.includes("查無資料"));
assert(JobCards.reviewCard({company_name: "來源失敗", summary: "", evidence: [], unavailable_sources: ["來源 A"]}).textContent.includes("暫時無法完整查詢"));
assert.equal(findAll(JobCards.reviewCard({company_name: "不安全", summary: "", evidence: [{source: "X", title: "Y", url: "javascript:bad", date: null, excerpt: "Z"}], unavailable_sources: []}), (item) => item.tagName === "A")[0].getAttribute("href"), undefined);

console.log("test_m2_cards: 7 checks passed");
