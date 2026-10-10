"use strict";

(function (root) {
  const createNode = typeof node === "function" ? node : root.node;
  let compactDescriptionId = 0;

  function element(tag, text, className) {
    if (typeof createNode !== "function") throw new Error("cards.js requires the shared node helper");
    return createNode(tag, text, className);
  }

  function display(value, fallback = "未提供") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function safeHref(value) {
    if (typeof value !== "string") return null;
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function externalLink(text, value, className = "button") {
    const link = element("a", text, className);
    const href = safeHref(value);
    if (href !== null) {
      link.href = href;
      if (typeof link.setAttribute === "function") link.setAttribute("href", href);
    }
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  function addValues(card, heading, values) {
    const items = Array.isArray(values) ? values.filter((value) => value !== null && value !== undefined && value !== "") : [];
    if (!items.length) return;
    const section = element("div", undefined, "stack");
    section.append(element("h4", heading));
    const list = element("ul");
    for (const value of items) list.append(element("li", String(value)));
    section.append(list);
    card.append(section);
  }

  function reviewPayload(item) {
    return item.basic_review || null;
  }

  function reviewScores(review) {
    if (!review || typeof review !== "object") return [];
    return Array.isArray(review.ratings) ? review.ratings.filter(score => score && typeof score === "object") : [];
  }

  function reviewScoreText(score) {
    if (!score || typeof score !== "object") return "";
    const rating = score;
    const source = score.source || "公開來源";
    const value = score.value;
    if (value === null || value === undefined || value === "") return "";
    const numeric = Number(value);
    const displayValue = Number.isFinite(numeric) ? numeric.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1") : String(value);
    const scaleMin = rating.scale_min;
    const scaleMax = rating.scale_max;
    const scale = scaleMin !== null && scaleMin !== undefined && scaleMax !== null && scaleMax !== undefined
      ? `${scaleMin}–${scaleMax}`
      : scaleMax ?? "";
    const metric = rating.metric ? ` ${rating.metric === "interview" ? "面試評價" : rating.metric === "overall" ? "工作評價" : rating.metric}` : "";
    const sample = rating.sample_count;
    const count = sample !== null && sample !== undefined && sample !== "" && Number.isFinite(Number(sample))
      ? `（${Number(sample)} 人）` : "";
    return `${source}${metric} ${displayValue}${scale ? `／${scale}` : ""}${count}`;
  }

  function reviewSourceStatus(source) {
    const label = source?.source || "公開來源";
    const status = String(source?.status || "").toLowerCase();
    const text = {
      failed: "暫時無法取得",
      blocked: "暫時無法取得",
      unsupported: "補充來源未設定",
      ambiguous: "公司待確認",
      empty: "尚未找到公開評分",
      success: "尚未找到公開評分",
      partial: "部分來源已取得",
      loading: "查詢中…",
      pending: "查詢中…",
      running: "查詢中…",
    }[status];
    return text ? `${label} ${text}` : "";
  }

  // One short header state when no source has a displayable score. Priority (most actionable first):
  // 公司待確認 > 評價查詢中… > 評價暫時無法取得 > 尚無公開評分 (everything else: empty/success/partial/unsupported).
  // Per-source wording stays in the 更多 review details (reviewSourceStatus).
  function reviewMessage(review) {
    if (!review) return "評價查詢中…";
    const sources = (Array.isArray(review.source_results) ? review.source_results : []).filter(Boolean);
    const states = new Set((sources.length ? sources.map(source => source.status) : [review.status]).map(status => String(status || "").toLowerCase()));
    if (states.has("ambiguous")) return "公司待確認";
    if (["loading", "pending", "running"].some(status => states.has(status))) return "評價查詢中…";
    if (["failed", "blocked"].some(status => states.has(status))) return "評價暫時無法取得";
    return "尚無公開評分";
  }

  function clear(target) {
    if (typeof target.replaceChildren === "function") target.replaceChildren();
    else { target.children = []; target._text = ""; }
  }

  function scoreChip(score) {
    const numeric = Number(score.value);
    const value = Number.isFinite(numeric) ? numeric.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1") : String(score.value);
    const max = score.scale_max;
    const sample = score.sample_count;
    const people = sample !== null && sample !== undefined && sample !== "" && Number.isFinite(Number(sample)) ? ` · ${Number(sample)} 人` : "";
    const chip = element("span", `★${value}${max !== null && max !== undefined && Number(max) !== 5 ? `／${max}` : ""}${people} `, "job-score");
    chip.setAttribute("role", "img");
    chip.setAttribute("aria-label", reviewScoreText(score));
    chip.append(element("small", String(score.source || "公開來源")));
    return chip;
  }

  // The card header shows one chip per scored source (never an average); status-only states stay text.
  function renderReviewSummary(target, review) {
    if (!target) return;
    clear(target);
    const seen = new Set();
    const scores = reviewScores(review).filter(score => {
      if (!reviewScoreText(score)) return false;
      const key = [score.source || "", score.metric || "", score.value ?? "", score.sample_count ?? ""].join("");
      return seen.has(key) ? false : (seen.add(key), true);
    });
    if (scores.length) for (const score of scores) target.append(scoreChip(score));
    else target.textContent = reviewMessage(review);
    if (typeof target.setAttribute === "function") target.setAttribute("data-review-state", review?.status || "pending");
  }

  function renderReviewDetails(target, review) {
    if (!target) return;
    if (typeof target.replaceChildren === "function") target.replaceChildren();
    else { target.children = []; target._text = ""; }
    const summary = element(target.tagName === "DETAILS" ? "summary" : "p", "評價來源與指標", "job-review-details-title");
    target.append(summary);
    const ratings = reviewScores(review);
    const scoredSources = new Set();
    for (const score of ratings) {
      const text = reviewScoreText(score);
      if (!text) continue;
      const source = String(score.source || "公開來源");
      scoredSources.add(source);
      const row = element("p", text);
      if (score.fetched_at) row.append(element("small", ` · 取得於 ${score.fetched_at}`));
      if (safeHref(score.url)) row.append(externalLink("查看來源 ↗", score.url));
      target.append(row);
    }
    for (const source of Array.isArray(review?.source_results) ? review.source_results : []) {
      if (!source || scoredSources.has(String(source.source || "公開來源"))) continue;
      const text = reviewSourceStatus(source);
      if (text) target.append(element("p", text));
    }
    for (const evidence of Array.isArray(review?.evidence) ? review.evidence : []) {
      if (!evidence || typeof evidence !== "object") continue;
      const entry = element("div", undefined, "job-review-evidence");
      if (evidence.title) entry.append(element("strong", String(evidence.title)));
      const metadata = [evidence.source, evidence.date].filter(value => value !== null && value !== undefined && value !== "").map(String);
      if (metadata.length) entry.append(element("small", metadata.join(" · ")));
      if (safeHref(evidence.url)) entry.append(externalLink("查看證據 ↗", evidence.url));
      if (entry.children.length) target.append(entry);
    }
    for (const limitation of Array.isArray(review?.limitations) ? review.limitations : []) target.append(element("p", limitation, "hint"));
    target.hidden = !review || (!ratings.length && !review.source_results?.length && !review.evidence?.length && !review.limitations?.length);
  }

  function updateReview(card, review) {
    if (!card) return;
    renderReviewSummary(card.reviewSummaryElement, review);
    renderReviewDetails(card.reviewDetailsElement, review);
    card.reviewState = review || null;
  }

  function icon(name) {
    if (typeof document === "undefined" || typeof document.createElementNS !== "function") return null;
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(namespace, "use");
    use.setAttribute("href", `/static/icons.svg#${name}`);
    svg.append(use);
    return svg;
  }

  function iconButton(name, label) {
    const button = element("button");
    button.type = "button";
    const glyph = icon(name);
    if (glyph) button.append(glyph);
    button.labelElement = element("span", label);
    button.append(button.labelElement);
    return button;
  }

  // 適合／注意／未知 tags from a recommendation; wrapping rows, never truncated.
  function fitList(recommendation) {
    const groups = [["ok", "適合", "reasons"], ["warn", "注意", "concerns"], ["unk", "未知", "unknowns"]];
    const list = element("div", undefined, "job-fits");
    for (const [kind, label, key] of groups) {
      const values = Array.isArray(recommendation?.[key]) ? recommendation[key].filter(Boolean) : [];
      if (!values.length) continue;
      const row = element("p", undefined, `job-fit job-fit-${kind}`);
      row.append(element("span", label, "job-fit-label"), element("span", values.join("、")));
      list.append(row);
    }
    return list.children.length ? list : null;
  }

  function jobCard(job, callbacks = {}) {
    const item = job || {};
    const handlers = callbacks || {};
    const card = element("article", undefined, "job-card item");
    const jobId = item.id === null || item.id === undefined ? "" : String(item.id);
    if (!card.dataset) card.dataset = {};
    card.dataset.jobId = jobId;
    if (typeof card.setAttribute === "function") card.setAttribute("data-job-id", jobId);

    const top = element("div", undefined, "job-card-top");
    const reviewSummary = element("span", undefined, "job-review-summary");
    reviewSummary.setAttribute("role", "status");
    renderReviewSummary(reviewSummary, reviewPayload(item));
    top.append(element("span", display(item.source, "未提供來源"), "job-source"), reviewSummary);
    const title = element("h3", display(item.title, "未提供職缺名稱"), "job-title");
    card.topElement = top;
    card.titleElement = title;
    card.reviewSummaryElement = reviewSummary;
    card.reviewState = reviewPayload(item);
    card.append(
      top,
      title,
      element("p", `${display(item.company_name, "未提供公司")} · ${display(item.location, "地點未提供")}`, "job-company"),
      element("p", display(item.salary_text, "薪資未提供"), "job-salary")
    );

    // Full detail lives behind "更多": summary, evidence, company review and "不感興趣".
    const description = element("div", undefined, "job-description");
    description.id = `job-description-${++compactDescriptionId}`;
    description.hidden = true;
    description.setAttribute("aria-label", "職缺摘要、評價與篩選提醒");
    card.descriptionElement = description;
    description.append(
      element("p", `發布日期：${display(item.published_at, "未提供日期")}`),
      element("p", `摘要：${display(item.summary)}`)
    );
    addValues(description, "篩選提醒", item.filter_warnings);
    const reviewDetails = element("div", undefined, "job-review-details");
    renderReviewDetails(reviewDetails, reviewPayload(item));
    description.append(reviewDetails);
    card.reviewDetailsElement = reviewDetails;

    const favorite = iconButton("heart", item.is_favorite ? "取消收藏" : "收藏");
    const pressed = String(Boolean(item.is_favorite));
    favorite.dataset.favorite = "true";
    favorite.setAttribute("aria-pressed", pressed);
    favorite.ariaPressed = pressed;
    if (typeof handlers.onFavorite !== "function") favorite.disabled = true;
    else favorite.addEventListener("click", () => handlers.onFavorite(item, favorite));
    favorite.addEventListener("favorite-change", () => { item.is_favorite = favorite.getAttribute("aria-pressed") === "true"; });

    const origin = externalLink("原始職缺 ↗", item.url, "button");

    const more = iconButton("more", "更多");
    more.setAttribute("aria-expanded", "false");
    more.setAttribute("aria-controls", description.id);
    card.setMoreOpen = open => {
      description.hidden = !open;
      more.setAttribute("aria-expanded", String(Boolean(open)));
    };
    more.addEventListener("click", () => card.setMoreOpen(more.getAttribute("aria-expanded") !== "true"));
    card.moreElement = more;

    const review = element("button", "查公司評價");
    review.type = "button";
    if (typeof handlers.onReview !== "function") review.disabled = true;
    else review.addEventListener("click", () => handlers.onReview(item));
    description.append(review);
    card.reviewElement = review;
    if (typeof handlers.onNotInterested === "function") {
      const notInterested = element("button", "不感興趣");
      notInterested.type = "button";
      notInterested.dataset.notInterested = "true";
      notInterested.addEventListener("click", () => handlers.onNotInterested(item, card, notInterested));
      description.append(notInterested);
      card.notInterestedElement = notInterested;
    }

    const actions = element("div", undefined, "job-actions");
    actions.append(favorite, origin, more);
    card.actionsElement = actions;
    // Feedback floats just above the action bar (CSS), so showing it never changes the card height.
    const feedback = element("p", undefined, "card-feedback");
    feedback.setAttribute("role", "status");
    const footer = element("div", undefined, "job-footer");
    footer.append(actions, feedback);
    card.footerElement = footer;
    card.append(footer, description);
    return card;
  }

  function reviewCard(review) {
    const item = review || {};
    const card = element("article", undefined, "review-card item");
    card.append(
      element("h3", display(item.company_name, "未提供公司")),
      element("p", `摘要：${display(item.summary)}`)
    );

    const evidence = Array.isArray(item.evidence) ? item.evidence : [];
    const unavailable = Array.isArray(item.unavailable_sources) ? item.unavailable_sources : [];
    if (!evidence.length) card.append(element("p", unavailable.length ? "暫時無法完整查詢" : "查無資料", "hint"));
    else {
      const section = element("div", undefined, "stack");
      section.append(element("h4", "查詢證據"));
      const list = element("ul");
      for (const source of evidence) {
        const entry = element("li", undefined, "stack");
        entry.append(
          element("h4", display(source && source.title, "未提供標題")),
          element("p", `${display(source && source.source, "未提供來源")} · ${display(source && source.date, "未提供日期")}`),
          element("p", `摘錄：${display(source && source.excerpt)}`),
          externalLink("查看來源 ↗", source && source.url)
        );
        list.append(entry);
      }
      section.append(list);
      card.append(section);
    }
    addValues(card, "暫時無法查詢的來源", unavailable);
    return card;
  }

  const JobCards = {jobCard, reviewCard, safeHref, externalLink, updateReview, reviewMessage, fitList};
  root.JobCards = JobCards;
  if (typeof module !== "undefined" && module.exports) module.exports = JobCards;
})(typeof window === "object" ? window : globalThis);
