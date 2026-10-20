"use strict";
let editing = null;
let favoriteCursor = null;
let messageCursor = null;
let selectedConversationId = null;
let conversationCursor = null;
let conversationListRequest = 0;
let messageLoadRequest = 0;
let switchingConversation = false;
let activeChat = null;
let pendingChatAction = null;
let editOrigin = null;
let previousValidAttachments = [];
const CHAT_RETRY_LIMIT = 5;
const CHAT_RETRY_DELAY_MS = 5000;
const chatSearchResults = new WeakMap();
const chatSearchMetadata = new WeakMap();
const chatRecommendationIds = new WeakMap();
const chatRecommendationItems = new WeakMap();
const searchStates = new Map();
// The conversation's working list, replayed from jobs/recommendations events in
// order with the same rules as Store.conversation_state: the latest
// recommendation replaces the shortlist, and removed jobs leave the main list.
const conversationWork = {shortlist: [], removed: new Map(), recommendations: null, excludedCompanies: []};
function resetConversationWork() {
  conversationWork.excludedCompanies = []; conversationWork.shortlist = []; conversationWork.removed.clear(); conversationWork.recommendations = null;
}
// The server replays the whole conversation in order; loaded pages and edits only approximate it.
function applyConversationWork(state) {
  if (!state || typeof state !== "object") return;
  conversationWork.shortlist = Array.isArray(state.shortlist) ? [...new Set(state.shortlist.map(String))] : [];
  conversationWork.excludedCompanies = Array.isArray(state.excluded_companies) ? state.excluded_companies.map(String) : [];
  conversationWork.removed.clear();
  for (const item of Array.isArray(state.removed) ? state.removed : []) {
    if (item?.job_id) conversationWork.removed.set(String(item.job_id), {by: String(item.by || "agent"), reason: String(item.reason || "")});
  }
  if (Array.isArray(state.recommendations)) conversationWork.recommendations = state.recommendations;
  // Keep the latest authoritative or live notes when older pages replay obsolete recommendation events.
  if (conversationWork.recommendations) {
    for (const search of searchStates.values()) search.recommendations.clear();
    for (const recommendation of conversationWork.recommendations) {
      const id = String(recommendation?.job_id || "");
      if (!id || !conversationWork.shortlist.includes(id) || conversationWork.removed.has(id)) continue;
      const owner = [...searchStates.values()].find(search => search.jobs.has(id));
      if (owner) owner.recommendations.set(id, {...recommendation, job_id: id});
    }
  }
}
// Replays the conversation's recent searches (oldest first) into the panel, whichever messages are loaded.
function replayConversationSearches(searches) {
  const reset = new Set();
  for (const search of Array.isArray(searches) ? searches : []) {
    if (!search?.search_id) continue;
    const state = stateForSearch(search.search_id);
    if (!reset.has(state.id)) {
      // Messages keep references to this object, so reset it in place before replaying all its events.
      Object.assign(state, createSearchState(state.id), {serverBacked: true});
      reset.add(state.id);
    }
    updateSearchState(search, null, {render: false});
  }
}
async function refreshConversationWork(conversationId = selectedConversationId) {
  if (activeChat || !conversationId) return;
  const request = ++messageLoadRequest;
  const requestToken = csrfToken;
  const result = await api(`/api/messages?${new URLSearchParams({conversation_id: String(conversationId), limit: "1"})}`);
  if (activeChat || !csrfToken || requestToken !== csrfToken || request !== messageLoadRequest
      || String(conversationId) !== selectedConversationId) return;
  const searches = result.conversation_state?.searches;
  if (Array.isArray(searches)) {
    // An edit can drop a reply together with its searches; their candidates leave the panel too.
    const valid = new Set(searches.map(search => String(search?.search_id)));
    for (const id of [...searchStates.keys()]) if (!id.startsWith("conversation:") && !valid.has(id)) searchStates.delete(id);
    if (activeSearchId && !searchStates.has(activeSearchId)) { activeSearchId = null; activeSearchState = null; }
    if (resultSearchId && !searchStates.has(resultSearchId)) resultSearchId = null;
    replayConversationSearches(searches);
  }
  applyConversationWork(result.conversation_state);
  if (!searchStates.size) { const panel = $("#search-results-panel"); if (panel) panel.hidden = true; }
  renderSearchResultsPanel();
}
let activeSearchId = null;
let activeSearchState = null;
// null keeps the result panel on the conversation-wide aggregate. Selecting a
// batch stores that search id without changing which search receives events.
let resultSearchId = null;
let resultSort = "recommended";
const resultSources = new Set();
let salaryOnly = false;
let newOnly = false;
let hideDismissed = true;
let resultView = "chat";
let resultObserver = null;
let basicReviewGeneration = 0;
let basicReviewActive = 0;
const basicReviewQueue = [];
const basicReviewPending = new Map();
const basicReviewControllers = new Map();
const basicReviewFailedAt = new Map();
const basicReviewRetryTimers = new Map();
const basicReviewCache = new Map();
const dismissedUndoTimers = new Map();
const dismissedRequestIds = new Map();
let dailyCursor = null;
let reviewRequest = 0;
const favoritePending = new Set();
let companionController = null;
let encourageController = null;
let companionSprites = [];
let companionQuotes = [];
let companionEncourageDate = "";
let companionPollTimer = null;
let mailedRunIds = new Set();
let thinkingTools = new Set();
let pushBootstrap = null;
let pushConfigured = false;
let pushPublicKey = null;
let pushSubscriptionId = null;
let pushSubscription = null;
let pushKeyMismatch = false;
let pushOperation = 0;
const ENCOURAGE_STORAGE_KEY = "job-companion-encourage-date";
$("#review-dialog").addEventListener("close", () => {
  reviewRequest++;
  $("#review-result").setAttribute("aria-busy", "false");
});
$("#encourage-dialog").addEventListener("close", closeEncouragement);

function storageDate() {
  try { return window.localStorage.getItem(ENCOURAGE_STORAGE_KEY); } catch (_) { return null; }
}
function rememberStorageDate(value) {
  try { window.localStorage.setItem(ENCOURAGE_STORAGE_KEY, value); } catch (_) {}
}
function clearCompanionPoll() {
  if (companionPollTimer !== null) clearTimeout(companionPollTimer);
  companionPollTimer = null;
}
function clearCompanionTransient(clearPrivate = false) {
  thinkingTools.clear();
  if (companionController) companionController.reset();
  if (clearPrivate) {
    const dialog = $("#encourage-dialog");
    if (dialog && typeof dialog.close === "function" && dialog.open) dialog.close();
    else closeEncouragement();
    companionQuotes = [];
    const text = $("#encourage-text");
    if (text) text.textContent = "";
  }
}
function closeEncouragement() {
  if (encourageController) encourageController.destroy();
  encourageController = null;
  if (companionController) {
    companionController.reset();
    companionController.setThinking(thinkingTools.size > 0);
  }
}
function clearCompanion() {
  clearCompanionPoll();
  const dialog = $("#encourage-dialog");
  if (dialog && typeof dialog.close === "function") dialog.close();
  if (encourageController) encourageController.destroy();
  encourageController = null;
  if (companionController) companionController.destroy();
  companionController = null;
  companionSprites = [];
  renderPortrait($("#header-companion"), 40);
  companionQuotes = [];
  companionEncourageDate = "";
  mailedRunIds.clear();
  thinkingTools.clear();
  pushOperation++;
  pushBootstrap = null;
  pushConfigured = false;
  pushPublicKey = null;
  pushSubscriptionId = null;
  pushSubscription = null;
  pushKeyMismatch = false;
  if (dialog) dialog.hidden = true;
  const text = $("#encourage-text");
  if (text) text.textContent = "";
  const pushStatus = $("#push-status");
  if (pushStatus) pushStatus.textContent = "";
}
function setupCompanion(bootstrap) {
  companionSprites = Array.isArray(bootstrap?.sprites) ? bootstrap.sprites.slice() : [];
  renderPortrait($("#header-companion"), 40);
  renderPortrait($("#status-companion"), 44);
  companionQuotes = Array.isArray(bootstrap?.quotes)
    ? bootstrap.quotes.filter(item => typeof item === "string" && item.trim()).map(item => item.trim()) : [];
  const element = $("#companion");
  if (!element || !window.CompanionSprite || typeof window.CompanionSprite.create !== "function") return;
  if (companionController) companionController.destroy();
  const reducedMotion = typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)") : false;
  companionController = window.CompanionSprite.create(element, companionSprites, {reducedMotion, maxDisplaySize: 96});
}
function renderPortrait(element, size = 36, state = "idle") {
  if (window.CompanionSprite?.renderStatic) window.CompanionSprite.renderStatic(element, companionSprites, state, size);
}
function maybeShowEncouragement(bootstrap) {
  const localDate = typeof bootstrap?.local_date === "string" ? bootstrap.local_date : "";
  if (!localDate || companionEncourageDate === localDate) return;
  if (storageDate() === localDate) { companionEncourageDate = localDate; return; }
  companionEncourageDate = localDate;
  rememberStorageDate(localDate);
  const text = $("#encourage-text");
  const quote = companionQuotes.length
    ? companionQuotes[Math.min(companionQuotes.length - 1, Math.floor(Math.random() * companionQuotes.length))]
    : "今天也辛苦了，慢慢來就好。";
  if (text) text.textContent = quote;
  if (companionController) companionController.play("encourage", 3000);
  const dialog = $("#encourage-dialog");
  if (!dialog) return;
  dialog.hidden = false;
  const popupElement = $("#encourage-companion");
  if (encourageController) encourageController.destroy();
  encourageController = null;
  if (popupElement && window.CompanionSprite && typeof window.CompanionSprite.create === "function") {
    const reducedMotion = typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-reduced-motion: reduce)") : false;
    encourageController = window.CompanionSprite.create(popupElement, companionSprites, {reducedMotion});
    encourageController.play("encourage", 3000);
  }
  if (typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else dialog.hidden = false;
}
function maybePlayMail(run) {
  const runId = run && run.id !== undefined && run.id !== null ? String(run.id) : "";
  const count = Number(run?.new_count);
  if (!runId || run.trigger === "baseline" || !["success", "partial"].includes(run?.status)
    || !Number.isFinite(count) || count <= 0 || mailedRunIds.has(runId)) return;
  mailedRunIds.add(runId);
  if (companionController) companionController.play("mail");
}
function updateToolThinking(data) {
  if (!data || data.name === "reply" || typeof data.name !== "string" || !["started", "completed", "interrupted", "failed"].includes(data.phase)) return;
  const key = String(data.call_id ?? data.id ?? data.name);
  if (data.phase === "started") thinkingTools.add(key);
  else thinkingTools.delete(key);
  if (companionController) companionController.setThinking(thinkingTools.size > 0);
}
function updatePushBootstrap(data) {
  pushBootstrap = data || null;
  pushConfigured = Boolean(data?.push_configured);
  pushPublicKey = typeof data?.push_public_key === "string" ? data.push_public_key : null;
  pushSubscriptionId = data?.push_subscription_id ? String(data.push_subscription_id) : null;
  updatePushUi();
}
function scheduleCompanionPoll() {
  if (companionPollTimer !== null || !csrfToken) return;
  const requestToken = csrfToken;
  companionPollTimer = setTimeout(async () => {
    companionPollTimer = null;
    if (!csrfToken || requestToken !== csrfToken) return;
    try {
      const data = await api("/api/bootstrap");
      if (requestToken !== csrfToken) return;
      maybePlayMail(data?.recent_run);
      updatePushBootstrap(data);
    } catch (_) {
      // Polling is best effort; the next cycle retries while the session is live.
    } finally {
      if (requestToken === csrfToken && csrfToken) scheduleCompanionPoll();
    }
  }, 30000);
}
function pushEnvironment() {
  const notification = window.Notification;
  if (!window.isSecureContext) return {ok: false, message: "需要 HTTPS 才能開啟通知。"};
  if (!navigator.serviceWorker) return {ok: false, message: "這個瀏覽器不支援 Service Worker 通知。"};
  if (!("PushManager" in window)) return {ok: false, message: "這個瀏覽器不支援背景通知。"};
  if (!notification || typeof notification.requestPermission !== "function") {
    return {ok: false, message: "這個瀏覽器不支援通知權限。"};
  }
  return {ok: true, notification};
}
function withIosHint(text) {
  const agent = String(navigator.userAgent || "");
  return /iPhone|iPad|iPod/.test(agent) && !navigator.standalone
    ? `${text} iPhone 請先將網站加入主畫面，再從主畫面開啟通知。` : text;
}
function setPushStatus(text, summary = "通知待確認") {
  const status = $("#push-status");
  if (status) status.textContent = withIosHint(text);
  const title = $("#push-summary");
  if (title) title.textContent = summary;
}
function updatePushUi() {
  const enable = $("#enable-push");
  const disable = $("#disable-push");
  if (!enable || !disable) return;
  const environment = pushEnvironment();
  enable.disabled = false;
  disable.disabled = false;
  if (!environment.ok) {
    enable.hidden = true;
    disable.hidden = !pushSubscriptionId;
    setPushStatus(environment.message, "目前無法開啟通知");
    return;
  }
  if (!pushConfigured) {
    enable.hidden = true;
    disable.hidden = !pushSubscriptionId;
    setPushStatus("通知服務尚未設定，請聯絡管理者。", "通知尚未設定");
    return;
  }
  const permission = environment.notification.permission || "default";
  if (pushKeyMismatch) {
    enable.hidden = false;
    enable.disabled = true;
    disable.hidden = !(pushSubscription || pushSubscriptionId);
    setPushStatus("通知金鑰已變更，請先關閉通知，再重新開啟。", "通知需重新綁定");
  } else if (permission === "denied") {
    enable.hidden = false;
    enable.disabled = true;
    disable.hidden = !pushSubscriptionId;
    setPushStatus("通知權限已拒絕，請到瀏覽器設定重新開啟。", "通知權限已拒絕");
  } else if (pushSubscription && pushSubscriptionId) {
    enable.hidden = true;
    disable.hidden = false;
    setPushStatus("此裝置已開啟通知。", "通知已開啟");
  } else if (permission === "granted") {
    enable.hidden = false;
    disable.hidden = !pushSubscriptionId;
    setPushStatus("通知權限已允許，請完成此裝置的通知綁定。", "通知待綁定");
  } else {
    enable.hidden = false;
    disable.hidden = !pushSubscriptionId;
    setPushStatus("需要你按下開啟通知，才會請求瀏覽器權限。", "通知尚未開啟");
  }
}
function decodePublicKey(value) {
  if (typeof value !== "string" || !value) return null;
  const decoder = typeof window.atob === "function" ? window.atob.bind(window) : null;
  if (!decoder) return null;
  try {
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    const binary = decoder(normalized + "=".repeat((4 - normalized.length % 4) % 4));
    return Uint8Array.from(binary, character => character.charCodeAt(0));
  } catch (_) { return null; }
}
function asBytes(value) {
  if (value === null || value === undefined) return null;
  try {
    if (typeof value.byteLength === "number") return new Uint8Array(value.buffer || value, value.byteOffset || 0, value.byteLength);
    if (Array.isArray(value)) return Uint8Array.from(value);
  } catch (_) {}
  return null;
}
function sameApplicationKey(subscription) {
  const expected = decodePublicKey(pushPublicKey);
  const actual = asBytes(subscription?.options?.applicationServerKey);
  if (!expected || !actual || expected.length !== actual.length) return false;
  return expected.every((value, index) => value === actual[index]);
}
async function pushRegistration() {
  if (!navigator.serviceWorker || !navigator.serviceWorker.ready) return null;
  return await navigator.serviceWorker.ready;
}
function subscriptionPayload(subscription) {
  const payload = subscription && typeof subscription.toJSON === "function" ? subscription.toJSON() : null;
  if (!payload || typeof payload.endpoint !== "string" || !payload.keys) throw new Error("通知訂閱格式不符，請重新啟用。");
  return payload;
}
async function savePushSubscription(subscription, requestToken, operation) {
  if (requestToken !== csrfToken || operation !== pushOperation) return false;
  const payload = subscriptionPayload(subscription);
  if (requestToken !== csrfToken || operation !== pushOperation) return false;
  const result = await api("/api/push-subscriptions", {method: "POST", body: JSON.stringify(payload)});
  if (requestToken !== csrfToken || operation !== pushOperation) return false;
  pushSubscription = subscription;
  pushSubscriptionId = result?.id ? String(result.id) : pushSubscriptionId;
  pushKeyMismatch = false;
  updatePushUi();
  return true;
}
async function rebindExistingPush() {
  const environment = pushEnvironment();
  if (!environment.ok || !pushConfigured || environment.notification.permission !== "granted") return;
  const operation = ++pushOperation;
  const requestToken = csrfToken;
  try {
    const registration = await pushRegistration();
    if (!registration?.pushManager || requestToken !== csrfToken || operation !== pushOperation) return;
    const existing = await registration.pushManager.getSubscription();
    if (requestToken !== csrfToken || operation !== pushOperation) return;
    pushSubscription = existing || null;
    if (!existing) { updatePushUi(); return; }
    if (!sameApplicationKey(existing)) {
      pushKeyMismatch = true; updatePushUi(); return;
    }
    await savePushSubscription(existing, requestToken, operation);
  } catch (_) {
    if (requestToken === csrfToken && operation === pushOperation) setPushStatus("目前無法確認通知設定，請稍後再試。");
  }
}
async function enablePush() {
  const environment = pushEnvironment();
  if (!environment.ok || !pushConfigured || !decodePublicKey(pushPublicKey)) { updatePushUi(); return; }
  const operation = ++pushOperation;
  const requestToken = csrfToken;
  try {
    let permission = environment.notification.permission || "default";
    if (permission === "denied") { updatePushUi(); return; }
    if (permission === "default") permission = await environment.notification.requestPermission();
    if (requestToken !== csrfToken || operation !== pushOperation) return;
    if (permission !== "granted") { updatePushUi(); return; }
    const registration = await pushRegistration();
    if (requestToken !== csrfToken || operation !== pushOperation) return;
    if (!registration?.pushManager) throw new Error("通知服務尚未就緒，請稍後再試。");
    let subscription = await registration.pushManager.getSubscription();
    if (requestToken !== csrfToken || operation !== pushOperation) return;
    if (subscription && !sameApplicationKey(subscription)) {
      pushSubscription = subscription; pushKeyMismatch = true; updatePushUi(); return;
    }
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: decodePublicKey(pushPublicKey)});
      if (requestToken !== csrfToken || operation !== pushOperation) return;
    }
    await savePushSubscription(subscription, requestToken, operation);
  } catch (error) {
    if (requestToken === csrfToken && operation === pushOperation) {
      updatePushUi();
      setPushStatus(error.message || "通知目前未能開啟，請稍後再試。");
    }
  }
}
async function disablePush() {
  const operation = ++pushOperation;
  const requestToken = csrfToken;
  const subscription = pushSubscription;
  const subscriptionId = pushSubscriptionId;
  let failure = null;
  try {
    if (subscription && typeof subscription.unsubscribe === "function") await subscription.unsubscribe();
  } catch (error) { failure = error; }
  if (requestToken !== csrfToken || operation !== pushOperation) return;
  try {
    if (subscriptionId) await api(`/api/push-subscriptions/${encodeURIComponent(subscriptionId)}`, {method: "DELETE"});
  } catch (error) { if (!failure) failure = error; }
  if (requestToken !== csrfToken || operation !== pushOperation) return;
  pushSubscription = null; pushSubscriptionId = null; pushKeyMismatch = false; updatePushUi();
  if (failure) throw failure;
}
function jobIdentifier(job) {
  const value = job?.id ?? job?.job_id ?? job?.url;
  return value === null || value === undefined || value === "" ? "" : String(value);
}
function reviewCompanyKey(job) {
  const name = String(job?.company_name || "").normalize("NFKC").trim().toLocaleLowerCase();
  if (!name) return "";
  const source = String(job?.source || "").normalize("NFKC").trim().toLocaleLowerCase() || "unknown-source";
  const companyId = String(job?.company_source_id ?? "").normalize("NFKC").trim().toLocaleLowerCase();
  // A source company id is the only safe cross-card identity. Keep the
  // id-bearing and name-only forms separate so an ambiguous card cannot
  // inherit a verified company's review cache.
  return companyId ? `id:${source}:${companyId}` : `name:${source}:${name}`;
}
function searchStorageKey(searchId) {
  return `job-companion-search-dismissed:${String(searchId || "")}`;
}
function readDismissed(searchId) {
  if (!searchId) return new Set();
  try {
    const value = JSON.parse(window.sessionStorage?.getItem(searchStorageKey(searchId)) || "[]");
    return new Set(Array.isArray(value) ? value.map(String) : []);
  } catch (_) { return new Set(); }
}
function writeDismissed(searchId, values) {
  if (!searchId) return;
  try { window.sessionStorage?.setItem(searchStorageKey(searchId), JSON.stringify([...values])); } catch (_) {}
}
function createSearchState(searchId) {
  return {
    id: String(searchId || ""), jobs: new Map(), jobNumbers: new Map(), recommendations: new Map(),
    sourceResults: [], areaResults: [], criteria: null, sources: [], canContinue: false,
    counts: {}, newIds: new Set(), dismissed: readDismissed(searchId), filterWarnings: [],
  };
}
function stableJobNumber(state, job) {
  const id = jobIdentifier(job);
  if (!id) return "";
  const explicit = job.citation_number;
  if (explicit !== null && explicit !== undefined && explicit !== "") return String(explicit);
  if (!state.jobNumbers.has(id)) state.jobNumbers.set(id, state.jobNumbers.size + 1);
  return String(state.jobNumbers.get(id));
}
function stateForSearch(searchId) {
  const id = String(searchId || "");
  if (!searchStates.has(id)) searchStates.set(id, createSearchState(id));
  return searchStates.get(id);
}
function aggregateSearchState() {
  const states = [...searchStates.values()];
  if (!states.length) return null;
  if (states.length === 1) return states[0];
  const aggregate = {
    id: "__all__", isAggregate: true, jobs: new Map(), jobOwners: new Map(), jobNumbers: new Map(), recommendations: new Map(),
    sourceResults: [], areaResults: [], criteria: states.at(-1)?.criteria || null,
    sources: [...new Set(states.flatMap(state => state.sources || []))],
    canContinue: states.some(state => state.canContinue), counts: {}, newIds: new Set(), dismissed: new Set(), filterWarnings: [],
    states, actionState: (activeSearchState?.canContinue ? activeSearchState : states.find(state => state.canContinue)) || activeSearchState || states.at(-1),
  };
  for (const [index, state] of states.entries()) {
    for (const source of state.sourceResults || []) aggregate.sourceResults.push({...source, search_id: state.id, batch_index: index + 1});
    for (const area of state.areaResults || []) aggregate.areaResults.push({...area, search_id: state.id, batch_index: index + 1});
    for (const warning of state.filterWarnings || []) if (!aggregate.filterWarnings.includes(warning)) aggregate.filterWarnings.push(warning);
    for (const job of state.jobs.values()) {
      const id = jobIdentifier(job); if (!id) continue;
      if (!aggregate.jobs.has(id)) {
        aggregate.jobs.set(id, job); aggregate.jobOwners.set(id, state);
        aggregate.jobNumbers.set(id, stableJobNumber(state, job));
        if (state.dismissed.has(id)) aggregate.dismissed.add(id);
      }
      if (state.newIds.has(id)) aggregate.newIds.add(id);
      const recommendation = state.recommendations.get(id);
      if (recommendation) aggregate.recommendations.set(id, recommendation);
    }
  }
  aggregate.counts.total_count = aggregate.jobs.size;
  aggregate.counts.new_count = aggregate.newIds.size;
  return aggregate;
}
function resultState() {
  if (resultSearchId && searchStates.has(resultSearchId)) return searchStates.get(resultSearchId);
  return aggregateSearchState() || activeSearchState;
}
function mergeJob(state, raw) {
  if (!raw || typeof raw !== "object") return null;
  const id = jobIdentifier(raw);
  if (!id) return null;
  const job = state.jobs.get(id);
  if (job) Object.assign(job, raw, {id});
  else state.jobs.set(id, {...raw, id});
  stableJobNumber(state, state.jobs.get(id));
  return state.jobs.get(id);
}
function searchLabel(state) {
  if (state?.isAggregate) return `全部搜尋結果（${availableJobCount(state)} 筆）`;
  const criteria = state?.criteria?.criteria || state?.criteria || {};
  const keyword = typeof criteria.keyword === "string" ? criteria.keyword.trim() : "";
  const areas = Array.isArray(criteria.area_codes) ? [...new Set(criteria.area_codes.filter(Boolean).map(code => {
    const match = Array.isArray(regionCatalog) ? regionCatalog.find(item => String(item.code) === String(code)) : null;
    return match?.name || "指定地區";
  }))].join("、") : "";
  const source = (state?.sources || []).filter(Boolean).join("＋");
  return [areas, keyword, criteria.remote_only ? "僅遠端" : "", source].filter(Boolean).join(" · ");
}
function salaryPresent(job) {
  const salary = String(job?.salary_text || "").trim();
  return Boolean(salary) && !/^(面議|面談|依公司規定|待遇面議|negotiable)$/i.test(salary);
}
function jobRating(job) {
  const review = job?.basic_review;
  const ratings = Array.isArray(review?.ratings) ? review.ratings : [];
  const preferred = ratings.find(rating => rating?.source === "GoodJob" && rating?.metric === "overall" && Number.isFinite(Number(rating.value)));
  return preferred ? Number(preferred.value) : null;
}
function isFeaturedJob(job) {
  return conversationWork.shortlist.includes(jobIdentifier(job)) || Boolean(job.is_recommended || job.featured);
}
function availableJobCount(state) {
  return state.jobs.size - removedJobs(state).length;
}
function removedJobs(state) {
  return [...(state?.jobs?.values() || [])].filter(job => conversationWork.removed.has(jobIdentifier(job)));
}
function resultJobs(state) {
  const values = [...(state?.jobs?.values() || [])].filter(job => {
    const owner = state?.jobOwners?.get(jobIdentifier(job)) || state;
    if (conversationWork.removed.has(jobIdentifier(job))) return false;
    if (resultSources.size && !resultSources.has(String(job.source || ""))) return false;
    if (salaryOnly && !salaryPresent(job)) return false;
    if (newOnly && !owner.newIds.has(jobIdentifier(job))) return false;
    if (hideDismissed && owner.dismissed.has(jobIdentifier(job)) && !dismissedUndoTimers.has(jobIdentifier(job))) return false;
    return true;
  });
  const number = job => Number(stableJobNumber(state, job)) || Number.MAX_SAFE_INTEGER;
  values.sort((a, b) => {
    if (resultSort === "latest") {
      const at = Date.parse(a.published_at || ""); const bt = Date.parse(b.published_at || "");
      if (Number.isFinite(at) || Number.isFinite(bt)) return (Number.isFinite(bt) ? bt : -Infinity) - (Number.isFinite(at) ? at : -Infinity) || number(a) - number(b);
    } else if (resultSort === "rating") {
      const ar = jobRating(a); const br = jobRating(b);
      if (ar !== null || br !== null) return (br === null ? -Infinity : br) - (ar === null ? -Infinity : ar) || number(a) - number(b);
    }
    // Shortlisted jobs keep the agent's recommendation order ahead of everything else.
    const rank = job => {
      const index = conversationWork.shortlist.indexOf(jobIdentifier(job));
      return index >= 0 ? index : isFeaturedJob(job) ? conversationWork.shortlist.length : Number.MAX_SAFE_INTEGER;
    };
    return rank(a) - rank(b) || number(a) - number(b);
  });
  return values;
}
// Panel counts describe retained candidates; server totals describe the original search batch.
function updateResultCounts(state, visible) {
  const total = availableJobCount(state);
  const totalText = Number.isFinite(total) ? total : state.jobs.size;
  const newCount = state.counts.new_count;
  const duplicate = state.counts.duplicate_count;
  const hasNew = newCount !== undefined && newCount !== null;
  const shown = visible.length !== total ? `目前顯示 ${visible.length}` : "";
  const full = [`共 ${totalText} 筆`, hasNew && `本輪新增 ${newCount}`, duplicate !== undefined && duplicate !== null && `重複 ${duplicate}`, shown].filter(Boolean);
  const short = [`${totalText} 筆`, hasNew && `新 ${newCount}`, shown && `顯示 ${visible.length}`].filter(Boolean);
  return {short: short.join(" · "), full: full.join(" · ") + (state.canContinue ? " · 還可以繼續找" : ""), total: totalText};
}
function sourceResultLabel(source) {
  const status = String(source?.status || "").toLowerCase();
  const count = source?.returned_count ?? source?.count;
  if (source?.quota_limited && count !== undefined && count !== null) {
    return `已取前 ${count} 筆，可繼續找`;
  }
  return ({
    success: "已完成",
    empty: "沒有找到職缺",
    partial: source?.parser_warning_count ? "部分完成／解析略過" : "部分完成",
    failed: "來源故障",
    blocked: "來源要求驗證或拒絕請求",
    unsupported: "來源不支援",
    not_queried: "尚未查詢",
  })[status] || status || "狀態未知";
}
function appendAreaResults(container, results) {
  if (!Array.isArray(results) || !results.length) return;
  const groups = new Map();
  for (const result of results) {
    const key = String(result.area_code ?? "all");
    if (!groups.has(key)) groups.set(key, {name: result.area_name || "全台", sources: []});
    groups.get(key).sources.push(result);
  }
  const details = node("details", undefined, "area-results");
  details.append(node("summary", `逐地區來源狀態（${groups.size} 區）`));
  for (const area of groups.values()) {
    const sources = area.sources.map(result => {
      const count = Number.isInteger(result.returned_count) ? ` ${result.returned_count}筆` : "";
      const successLabel = result.returned_count === 0 ? "已查，無新增職缺" : result.returned_count > 0 ? `有結果${count}` : "查詢成功";
      const label = ({success: successLabel, empty: "零筆", partial: `${result.completed ? "已完成，有限制／警告" : "尚未完成"}${count}`,
        not_queried: "尚未查詢", blocked: "來源受阻", failed: "查詢失敗", unsupported: "不支援"})[result.status] || "狀態未知";
      return `${result.source || "來源"} ${label}`;
    });
    details.append(node("p", `${area.name}：${sources.join("；")}`, "hint"));
    const messages = area.sources.filter(result => result.message);
    if (messages.length) {
      const notes = node("details", undefined, "area-result-messages");
      notes.append(node("summary", `查看來源訊息（${messages.length}）`));
      for (const result of messages) notes.append(node("p", `${result.source || "來源"}：${result.message}`, "hint"));
      details.append(notes);
    }
  }
  container.append(details);
}
function resultControl(label, pressed, callback) {
  const button = node("button", label); button.type = "button";
  button.setAttribute("aria-pressed", String(Boolean(pressed)));
  button.addEventListener("click", callback);
  return button;
}
function showSearchPanel() {
  const panel = $("#search-results-panel");
  if (panel) panel.hidden = Boolean($("#chat")?.hidden);
}
function setResultView(view) {
  resultView = view === "results" ? "results" : "chat";
  const chat = $("#chat"); const panel = $("#search-results-panel");
  if (chat) chat.classList.toggle("results-view", resultView === "results");
  if (panel) panel.classList.toggle("mobile-active", resultView === "results");
  const chatButton = $("#show-chat-view"); const resultButton = $("#show-results-view");
  if (chatButton) chatButton.setAttribute("aria-pressed", String(resultView === "chat"));
  if (resultButton) resultButton.setAttribute("aria-pressed", String(resultView === "results"));
}
function resultCardJob(state, job) {
  const id = jobIdentifier(job);
  const owner = state?.jobOwners?.get(id) || state;
  const card = jobCard(job, {
    onNotInterested: (item, element) => dismissResultJob(owner, item, element),
  });
  card.dataset.searchId = owner.id;
  card.dataset.resultNumber = stableJobNumber(owner, job);
  const removed = conversationWork.removed.get(id);
  const featured = !removed && isFeaturedJob(job);
  const label = removed ? "已移出" : featured ? "精選" : "尚未精選";
  card.topElement.prepend(node("span", `${label} #${stableJobNumber(owner, job)}`, featured ? "job-featured" : "job-unfeatured"));
  // Fit notes belong to the current shortlist; an older recommendation no longer explains a candidate.
  const recommendation = featured ? state.recommendations.get(id) || owner.recommendations.get(id) : null;
  const fits = recommendation ? JobCards.fitList(recommendation) : null;
  if (fits) card.insertBefore(fits, card.footerElement);
  if (removed?.reason) card.insertBefore(node("p", `移出原因：${removed.reason}`, "hint"), card.footerElement);
  if (owner.dismissed.has(id)) markDismissedCard(card, true);
  return card;
}
// Bottom-of-list footer: the one "繼續找" button (same continueSearch flow), or a hint once the server says there is nothing more.
function continueFooter(state, seen) {
  const box = node("div", undefined, "continue-footer");
  if (!state.canContinue) { box.append(node("p", "這次搜尋的範圍內已沒有更多結果，可調整條件再找。", "hint")); return box; }
  const button = node("button"); button.id = "continue-search"; button.type = "button"; button.className = "primary";
  button.addEventListener("click", () => continueSearch(state));
  box.append(button, node("span", `已看 ${seen} 筆`, "hint"));
  syncContinueButton(button);
  return box;
}
function syncContinueButton(button = $("#continue-search")) {
  if (!button) return;
  const busy = Boolean(activeChat || switchingConversation);
  button.disabled = busy; button.textContent = busy ? "搜尋中…" : "繼續找";
}
function renderSearchResultsPanel() {
  const panel = $("#search-results-panel");
  const state = resultState();
  if (!panel || !state) return;
  showSearchPanel();
  panel.dataset.searchId = state.id;
  const title = $("#search-results-title");
  if (title) title.textContent = state.isAggregate ? `這段對話的職缺（${availableJobCount(state)} 筆）` : ["本次搜尋", searchLabel(state)].filter(Boolean).join(" · ");
  const summary = $("#search-results-summary");
  const jobs = resultJobs(state);
  const counts = updateResultCounts(state, jobs);
  if (summary) summary.textContent = counts.short;
  const fullCounts = $("#search-results-counts");
  if (fullCounts) fullCounts.textContent = counts.full;
  const badge = $("#results-count-badge");
  if (badge) badge.textContent = `(${counts.total})`;
  const controls = $("#search-results-controls");
  if (controls) {
    controls.replaceChildren();
    if (searchStates.size > 1) {
      const batchLabel = node("label", "搜尋批次");
      const batch = node("select"); batch.setAttribute("aria-label", "搜尋批次");
      const all = node("option", "全部搜尋結果"); all.value = ""; all.selected = !resultSearchId; batch.append(all);
      [...searchStates.values()].forEach((candidate, index) => {
        const option = node("option", [`第${index + 1}批`, searchLabel(candidate)].filter(Boolean).join(" · "));
        option.value = candidate.id; option.selected = resultSearchId === candidate.id; batch.append(option);
      });
      batch.addEventListener("change", () => { resultSearchId = batch.value || null; renderSearchResultsPanel(); });
      batchLabel.append(batch); controls.append(batchLabel);
    }
    const sort = node("select"); sort.setAttribute("aria-label", "職缺排序");
    for (const [value, label] of [["recommended", "推薦優先"], ["latest", "最新發布"], ["rating", "GoodJob 工作評分"]]) {
      const option = node("option", label); option.value = value; option.selected = resultSort === value; sort.append(option);
    }
    sort.addEventListener("change", () => { resultSort = sort.value; renderSearchResultsPanel(); });
    const sortLabel = node("label", "排序"); sortLabel.append(sort); controls.append(sortLabel);
    const sources = state.sources?.length ? state.sources : [...new Set([...state.jobs.values()].map(job => job.source).filter(Boolean))];
    for (const source of sources) {
      controls.append(resultControl(String(source), resultSources.has(String(source)), () => {
        const key = String(source); if (resultSources.has(key)) resultSources.delete(key); else resultSources.add(key); renderSearchResultsPanel();
      }));
    }
    const salary = node("label", "有標示薪資"); const salaryInput = node("input"); salaryInput.type = "checkbox"; salaryInput.checked = salaryOnly; salaryInput.addEventListener("change", () => { salaryOnly = salaryInput.checked; renderSearchResultsPanel(); }); salary.prepend(salaryInput); controls.append(salary);
    controls.append(resultControl("只看本次新增", newOnly, () => { newOnly = !newOnly; renderSearchResultsPanel(); }));
    controls.append(resultControl("顯示不感興趣", !hideDismissed, () => { hideDismissed = !hideDismissed; renderSearchResultsPanel(); }));
  }
  const details = $("#search-results-details");
  if (details) {
    details.replaceChildren();
    for (const source of state.sourceResults || []) {
      const count = source.returned_count ?? source.count ?? "未知數";
      const label = sourceResultLabel(source);
      const countText = (source.quota_limited || source.truncated) ? "" : ` · ${count} 筆`;
      details.append(node("p", `${source.source || "來源"} · ${label}${countText}${source.message ? ` · ${source.message}` : ""}`, "hint"));
    }
    appendAreaResults(details, state.areaResults);
    for (const warning of state.filterWarnings || []) details.append(node("p", warning, "hint"));
    details.hidden = !details.children.length;
  }
  const actions = $("#search-results-actions");
  if (actions) {
    actions.replaceChildren();
    const daily = node("button", "存成每日提醒"); daily.type = "button"; action(daily, () => prefillDailyReminder(state)); actions.append(daily);
  }
  const list = $("#search-results-list");
  if (!list) return;
  list.replaceChildren();
  if (conversationWork.excludedCompanies.length) {
    const section = node("section", undefined, "excluded-companies");
    section.append(node("h3", "已排除公司"));
    const chips = node("div", undefined, "excluded-company-chips");
    for (const company of conversationWork.excludedCompanies) {
      const chip = node("button", `${company} ×`); chip.type = "button";
      chip.setAttribute("aria-label", `取消排除 ${company}`);
      chip.disabled = Boolean(activeChat || switchingConversation || !csrfToken);
      chip.addEventListener("click", () => restoreExcludedCompany(company, chip)); chips.append(chip);
    }
    section.append(chips); list.append(section);
  }
  const removed = removedJobs(state);
  const removedSection = () => {
    const section = node("details", undefined, "removed-jobs");
    section.append(node("summary", `已移出 ${removed.length}`));
    for (const job of removed) section.append(resultCardJob(state, job));
    return section;
  };
  if (!jobs.length) {
    list.append(node("p", state.jobs.size && !counts.total ? "職缺都已移出，請展開「已移出」區查看。" : state.jobs.size ? "目前篩選沒有職缺。" : "這次搜尋尚未取得職缺。", "hint"));
    if (removed.length) list.append(removedSection());
    if (state.canContinue || state.jobs.size) list.append(continueFooter(state, counts.total));
    return;
  }
  const featured = jobs.filter(isFeaturedJob);
  const rest = jobs.filter(job => !featured.includes(job));
  if (featured.length) {
    list.append(node("h3", `精選 ${featured.length}`));
    for (const job of featured) list.append(resultCardJob(state, job));
  }
  if (rest.length) {
    list.append(node("h3", `其他候選 ${rest.length}`));
    for (const job of rest) list.append(resultCardJob(state, job));
  }
  if (removed.length) list.append(removedSection());
  list.append(continueFooter(state, counts.total));
}
function updateSearchState(data, message, {historical = false, render = true} = {}) {
  if (!data || typeof data !== "object") return null;
  const requestedId = data.search_id ? String(data.search_id) : (activeSearchId || `conversation:${selectedConversationId || ""}`);
  const saved = searchStates.get(requestedId);
  if (historical && saved?.serverBacked) {
    for (const item of Array.isArray(data.items) ? data.items : []) if (!saved.jobs.has(jobIdentifier(item))) mergeJob(saved, item);
    if (message) {
      chatSearchResults.set(message, [...(chatSearchResults.get(message) || []), ...(data.items || []).map(item => ({...item, id: jobIdentifier(item)})).filter(item => item.id)]);
      chatSearchMetadata.set(message, saved);
    }
    return saved;
  }
  if (activeSearchId !== requestedId) {
    activeSearchState = stateForSearch(requestedId);
    // A new batch belongs in the conversation aggregate by default. The
    // selector can still narrow the panel back to any earlier batch.
    resultSearchId = null;
    resultSources.clear(); resultSort = "recommended"; salaryOnly = false; newOnly = false; hideDismissed = true;
  }
  activeSearchId = requestedId;
  const state = activeSearchState || stateForSearch(requestedId);
  if (data.criteria) state.criteria = data.criteria;
  if (Array.isArray(data.sources)) state.sources = data.sources.map(String);
  if (Array.isArray(data.source_results)) state.sourceResults = data.source_results;
  if (Array.isArray(data.area_results)) state.areaResults = data.area_results;
  if (Array.isArray(data.filter_warnings)) state.filterWarnings = data.filter_warnings;
  if (data.can_continue !== undefined) state.canContinue = Boolean(data.can_continue);
  if (Array.isArray(data.new_ids)) state.newIds = new Set(data.new_ids.map(String));
  for (const key of ["new_count", "duplicate_count", "total_count"]) if (data[key] !== undefined) state.counts[key] = data[key];
  if (Array.isArray(data.dismissed_ids)) state.dismissed = new Set(data.dismissed_ids.map(String));
  writeDismissed(state.id, state.dismissed);
  for (const item of Array.isArray(data.items) ? data.items : []) mergeJob(state, item);
  if (Array.isArray(data.excluded_companies)) conversationWork.excludedCompanies = data.excluded_companies.map(String);
  for (const id of Array.isArray(data.excluded_job_ids) ? data.excluded_job_ids : []) {
    conversationWork.removed.set(String(id), {by: "company", reason: "公司已排除"});
  }
  if (message) {
    chatSearchResults.set(message, [...(chatSearchResults.get(message) || []), ...(data.items || []).map(item => ({...item, id: jobIdentifier(item)})).filter(item => item.id)]);
    chatSearchMetadata.set(message, state);
  }
  if (render) renderSearchResultsPanel();
  return state;
}
function updateRecommendations(data, message, {historical = false} = {}) {
  const items = Array.isArray(data?.items) ? data.items : [];
  const current = historical ? null : new Map((conversationWork.recommendations || []).map(item => [String(item.job_id), item]));
  const recommendationIds = items.map(recommendation => String(recommendation?.job_id ?? jobIdentifier(recommendation?.job))).filter(Boolean);
  const explicitState = data?.search_id ? stateForSearch(data.search_id) : null;
  let state = explicitState;
  if (message) chatRecommendationItems.set(message, items);
  const ids = [];
  for (const recommendation of items) {
    const job = recommendation?.job || recommendation;
    const id = String(recommendation?.job_id ?? jobIdentifier(job));
    if (!id) continue;
    current?.set(id, {...recommendation, job_id: id});
    // A recommendation without search_id may span multiple search calls in
    // one reply. Keep each recommendation on the batch that already owns it;
    // an unknown full job remains available in the chat citation only.
    const owner = explicitState || [...searchStates.values()].find(candidate => candidate.jobs.has(id)) || null;
    if (!owner) { ids.push(id); continue; }
    if (recommendation?.job) mergeJob(owner, {...job, id});
    owner.recommendations.set(id, {...recommendation, job_id: id});
    state ||= owner;
    ids.push(id);
  }
  if (message) {
    chatRecommendationIds.set(message, ids.length ? ids : recommendationIds);
    if (state) chatSearchMetadata.set(message, state);
  }
  // Events saved before `replace` existed always carried a non-empty replacement list.
  if (data?.replace ?? recommendationIds.length > 0) conversationWork.shortlist = [...new Set(recommendationIds)];
  for (const item of Array.isArray(data?.removed) ? data.removed : []) {
    if (item?.job_id) conversationWork.removed.set(String(item.job_id), {by: "agent", reason: String(item.reason || "")});
  }
  for (const id of recommendationIds) conversationWork.removed.delete(id);
  // Live events and restored snapshots supersede the page cache; history must not overwrite it.
  if (current) conversationWork.recommendations = conversationWork.shortlist
    .filter(id => !conversationWork.removed.has(id)).map(id => current.get(id)).filter(Boolean);
  renderSearchResultsPanel();
  return state;
}
function focusJob(job, state = activeSearchState) {
  if (!job) return;
  if (state && state.isAggregate) {
    // The aggregate already contains the target; keep its batch ownership so
    // dismiss/continue actions still use the originating search id.
    resultSearchId = null;
  } else if (state && state !== activeSearchState) {
    activeSearchState = state; activeSearchId = state.id; resultSearchId = state.id; renderSearchResultsPanel();
  } else if (state && resultSearchId && resultSearchId !== state.id) {
    resultSearchId = state.id; renderSearchResultsPanel();
  }
  showSearchPanel(); setResultView("results");
  const id = jobIdentifier(job);
  const panel = $("#search-results-panel");
  const cards = panel ? [...(panel.querySelectorAll?.(".job-card") || [])] : [];
  const target = cards.find(card => String(card.dataset.jobId) === id);
  if (target) {
    target.scrollIntoView?.({block: "center"}); target.classList.add("job-highlight");
    setTimeout(() => target.classList.remove("job-highlight"), 1800);
  }
}
function focusJobByUrl(href) {
  const states = activeSearchState ? [activeSearchState, ...[...searchStates.values()].filter(state => state !== activeSearchState)] : [...searchStates.values()];
  for (const state of states) {
    const job = [...state.jobs.values()].find(item => JobCards.safeHref(item.url) === href);
    if (job) { focusJob(job, state); return; }
  }
}
function markDismissedCard(card, dismissed) {
  if (!card) return;
  card.classList?.toggle("is-not-interested", dismissed);
  if (dismissed) card.setMoreOpen?.(true); // keep the 5-second undo visible
  if (card.notInterestedElement) {
    card.notInterestedElement.textContent = dismissed ? "復原" : "不感興趣";
    card.notInterestedElement.setAttribute("aria-pressed", String(dismissed));
  }
}
async function restoreExcludedCompany(company, button) {
  if (activeChat || switchingConversation || !csrfToken || !selectedConversationId) return;
  const token = csrfToken; const conversationId = selectedConversationId;
  button.disabled = true;
  try {
    await api(`/api/conversations/${encodeURIComponent(conversationId)}/excluded-companies`, {
      method: "PATCH", body: JSON.stringify({company}),
    });
    if (token !== csrfToken || conversationId !== selectedConversationId) return;
    await refreshConversationWork(conversationId);
  } catch (error) {
    if (token === csrfToken && conversationId === selectedConversationId) message(`取消排除未能保存：${error.message}`, true);
  } finally {
    if (token === csrfToken && conversationId === selectedConversationId) renderSearchResultsPanel();
  }
}
async function dismissResultJob(state, job, card) {
  const id = jobIdentifier(job); if (!id || !state) return;
  const dismissed = !state.dismissed.has(id);
  if (dismissed) state.dismissed.add(id); else state.dismissed.delete(id);
  writeDismissed(state.id, state.dismissed); markDismissedCard(card, dismissed);
  if (dismissed) {
    if (dismissedUndoTimers.has(id)) clearTimeout(dismissedUndoTimers.get(id));
    dismissedUndoTimers.set(id, setTimeout(() => { dismissedUndoTimers.delete(id); renderSearchResultsPanel(); }, 5000));
  } else if (dismissedUndoTimers.has(id)) { clearTimeout(dismissedUndoTimers.get(id)); dismissedUndoTimers.delete(id); }
  renderSearchResultsPanel();
  const requestId = (dismissedRequestIds.get(id) || 0) + 1;
  dismissedRequestIds.set(id, requestId);
  const token = csrfToken;
  try {
    const result = await api(`/api/searches/${encodeURIComponent(state.id)}`, {method: "PATCH", body: JSON.stringify({conversation_id: selectedConversationId, job_id: id, dismissed})});
    if (!token || token !== csrfToken || dismissedRequestIds.get(id) !== requestId) return;
    if (Array.isArray(result?.dismissed_ids)) { state.dismissed = new Set(result.dismissed_ids.map(String)); writeDismissed(state.id, state.dismissed); renderSearchResultsPanel(); }
  } catch (error) {
    if (token === csrfToken) message(`職缺隱藏狀態未能保存：${error.message}`, true);
  }
}
function continueSearch(state) {
  if (!state?.id || !state.canContinue || activeChat || switchingConversation) return;
  if (state.isAggregate) state = state.actionState || activeSearchState;
  if (!state?.id || !state.canContinue) return;
  const input = $("#chat-form [name=text]"); if (!input) return;
  $("#result-options-dialog")?.close();
  pendingChatAction = {type: "continue_search", search_id: state.id};
  input.value = "請繼續找一批不同的職缺。";
  updateChatCount(); setResultView("chat"); input.focus();
  $("#chat-form").requestSubmit?.($("#chat-form button[type=submit]"));
}
function prefillDailyReminder(state) {
  const input = $("#chat-form [name=text]"); if (!input) return;
  $("#result-options-dialog")?.close();
  input.value = `請把「${searchLabel(state) || "本次搜尋"}」存成每日搜尋條件，先整理成提案，等我確認後再更新。`;
  updateChatCount(); showPage("chat"); setResultView("chat"); input.focus();
  setChatStatus("已預填每日提醒提案訊息，送出後仍需你確認才會生效。", "complete");
}
function resetBasicReviews({clearCache = false} = {}) {
  basicReviewGeneration++;
  basicReviewQueue.length = 0;
  for (const controller of basicReviewControllers.values()) controller.abort();
  basicReviewControllers.clear();
  basicReviewPending.clear();
  basicReviewActive = 0;
  basicReviewFailedAt.clear();
  for (const timer of basicReviewRetryTimers.values()) clearTimeout(timer);
  basicReviewRetryTimers.clear();
  if (clearCache) basicReviewCache.clear();
  if (resultObserver) { resultObserver.disconnect(); resultObserver = null; }
}
function queueBasicReview(job, card) {
  const key = reviewCompanyKey(job); if (!key || !card || !csrfToken) return;
  const failed = basicReviewFailedAt.get(key);
  if (failed && Date.now() - failed < 60000) return;
  const cached = basicReviewCache.get(key);
  if (cached) { JobCards.updateReview(card, cached); return; }
  if (basicReviewPending.has(key)) return;
  if (!basicReviewQueue.some(task => task.key === key)) basicReviewQueue.push({key, job, card, generation: basicReviewGeneration});
  drainBasicReviews();
}
function updateBasicReviewCards(key, review) {
  for (const card of [...(document.querySelectorAll?.(".job-card") || [])]) {
    if (card.dataset.reviewCompany === key) {
      if (card.reviewJob && typeof card.reviewJob === "object") card.reviewJob.basic_review = review;
      JobCards.updateReview(card, review);
    }
  }
  if (resultSort === "rating" && [...searchStates.values()].some(state => [...state.jobs.values()].some(job => reviewCompanyKey(job) === key))) renderSearchResultsPanel();
}
function visibleReviewCard(key) {
  return [...(document.querySelectorAll?.(".job-card") || [])].find(item => item.dataset.reviewCompany === key && item.reviewVisible);
}
function scheduleBasicReviewRetry(key, generation, token) {
  if (basicReviewRetryTimers.has(key)) return;
  const timer = setTimeout(() => {
    basicReviewRetryTimers.delete(key);
    if (generation !== basicReviewGeneration || token !== csrfToken || !token) return;
    const card = visibleReviewCard(key);
    if (!card) return;
    basicReviewFailedAt.delete(key);
    queueBasicReview(card.reviewJob, card);
  }, 60000);
  basicReviewRetryTimers.set(key, timer);
}
async function drainBasicReviews() {
  while (basicReviewActive < 2 && basicReviewQueue.length) {
    const task = basicReviewQueue.shift(); if (!task) return;
    const {key, job, generation} = task; if (basicReviewPending.has(key)) continue;
    if (!visibleReviewCard(key)) continue; // scrolled away while queued; re-queued when a card re-enters the viewport
    basicReviewActive++;
    const token = csrfToken;
    const controller = new AbortController();
    const promise = api("/api/reviews", {method: "POST", body: JSON.stringify({company_name: job.company_name, job_id: jobIdentifier(job), scope: "basic"}), signal: controller.signal});
    basicReviewPending.set(key, promise);
    basicReviewControllers.set(key, controller);
    promise.then(result => {
      if (generation !== basicReviewGeneration || token !== csrfToken || !token) return;
      const review = result && typeof result === "object" ? result : {status: "failed"};
      const failed = review.status === "failed";
      if (failed) { basicReviewFailedAt.set(key, Date.now()); scheduleBasicReviewRetry(key, generation, token); }
      else {
        basicReviewCache.set(key, review); basicReviewFailedAt.delete(key);
        if (basicReviewRetryTimers.has(key)) { clearTimeout(basicReviewRetryTimers.get(key)); basicReviewRetryTimers.delete(key); }
      }
      updateBasicReviewCards(key, review);
    }).catch(() => {
      if (generation === basicReviewGeneration && token === csrfToken) {
        basicReviewFailedAt.set(key, Date.now());
        scheduleBasicReviewRetry(key, generation, token);
        updateBasicReviewCards(key, {status: "failed"});
      }
    }).finally(() => {
      if (basicReviewPending.get(key) !== promise) return;
      basicReviewPending.delete(key);
      if (basicReviewControllers.get(key) === controller) basicReviewControllers.delete(key);
      basicReviewActive = Math.max(0, basicReviewActive - 1);
      drainBasicReviews();
    });
  }
}
function observeReviewCard(card, job) {
  const key = reviewCompanyKey(job); if (!key || !card) return;
  card.dataset.reviewCompany = key; card.reviewJob = job;
  if (!resultObserver && typeof window.IntersectionObserver === "function") {
    resultObserver = new window.IntersectionObserver(entries => {
      for (const entry of entries) {
        entry.target.reviewVisible = Boolean(entry.isIntersecting);
        if (entry.isIntersecting) queueBasicReview(entry.target.reviewJob, entry.target);
      }
    }, {rootMargin: "0px"});
  }
  if (resultObserver) resultObserver.observe(card);
}

function clearEditor() {
  editing = null;
  $("#criteria-fields").replaceChildren(criteriaFields());
  $("#editor-title").textContent = "提出新增條件";
  $("#cancel-edit").hidden = true;
}
function signedOut() {
  reviewRequest++;
  resetBasicReviews({clearCache: true});
  dismissedRequestIds.clear();
  conversationListRequest++; messageLoadRequest++;
  if (activeChat) activeChat.controller.abort();
  activeChat = null; pendingChatAction = null; selectedConversationId = null; conversationCursor = null; messageCursor = null; dailyCursor = null; switchingConversation = false;
  activeSearchId = null; activeSearchState = null; resultSearchId = null; searchStates.clear(); resetConversationWork(); setResultView("chat");
  clearCompanion();
  csrfToken = ""; editing = null; favoriteCursor = null;
  $("#preferences-feedback").textContent = "";
  $("#user-panel").hidden = true; $("#logout").hidden = true; $("#logout").textContent = "登出"; $("#access-needed").hidden = false;
  for (const id of ["preference-list", "proposal-list", "favorite-list", "criteria-fields", "chat-list", "conversation-list", "daily-list", "daily-sources", "daily-date"]) $(`#${id}`).replaceChildren();
  const results = $("#search-results-list"); if (results) results.replaceChildren();
  const resultPanel = $("#search-results-panel"); if (resultPanel) resultPanel.hidden = true;
  $("#daily-status").textContent = "";
  $("#conversation-history-status").textContent = ""; $("#conversation-query").value = ""; $("#active-conversation-title").textContent = "尚未選取對話";
  $("#conversation-history-status").setAttribute("aria-busy", "false"); $("#more-conversations").disabled = false;
  $("#chat-form").reset(); $("#chat-status").textContent = ""; $("#cancel-chat").hidden = true;
  editOrigin = null; $("#message-edit-banner").hidden = true; $("#message-edit-status").textContent = "";
  previousValidAttachments = []; clearChatFileDrag();
  updateResumeSelection(); updateChatCount();
  $("#review-dialog").close(); $("#review-result").replaceChildren();
  $("#conversation-history-dialog").close(); $("#rename-conversation-dialog").close(); $("#delete-conversation-dialog").close();
  updateConversationControls();
}
function preferencesFeedback(text, failed = false) {
  const status = $("#preferences-feedback");
  status.textContent = text; status.classList.toggle("error", failed);
  status.focus(); status.scrollIntoView({block: "nearest"});
}
async function loadPreferences(proposalId = null) {
  const data = await api("/api/preferences");
  $("#preferences-status").textContent = data.paused ? "目前沒有有效條件。每日搜尋將保持暫停。" : data.user_has_chosen ? "以下是你已確認的條件。" : "目前使用管理者的預設；確認自己的條件後會改用你的選擇。";
  $("#preference-list").replaceChildren();
  for (const item of data.effective) {
    const card = node("article", undefined, "item");
    card.append(node("p", describe(item.criteria)));
    if (item.is_default) card.append(node("small", "管理者預設；可在下方提出自己的條件。"));
    else {
      const buttons = node("div", undefined, "row");
      const edit = node("button", "提出修改"); edit.type = "button";
      edit.addEventListener("click", () => {
        editing = item; $("#criteria-fields").replaceChildren(criteriaFields(item.criteria));
        $("#editor-title").textContent = "提出條件修改"; $("#cancel-edit").hidden = false;
        $("#criteria-fields input").focus();
      });
      const remove = node("button", "提出刪除"); remove.type = "button";
      action(remove, async () => {
        try {
          const result = await api("/api/preference-proposals", {method: "POST", body: JSON.stringify({operation: "delete", preference_id: item.id, base_revision: item.revision})});
          await loadPreferences(result.id);
        } catch (error) { if (csrfToken) preferencesFeedback(error.message, true); }
      });
      buttons.append(edit, remove); card.append(buttons);
    }
    $("#preference-list").append(card);
  }
  $("#proposal-list").replaceChildren();
  if (!data.proposals.length) $("#proposal-list").append(node("p", "目前沒有等待確認的提案。", "hint"));
  for (const proposal of data.proposals) {
    const card = node("article", undefined, "item stack");
    card.dataset.proposalId = String(proposal.id);
    const heading = node("h3", {create: "新增條件", update: "修改條件", delete: "刪除條件"}[proposal.operation]);
    heading.tabIndex = -1;
    card.append(heading, node("p", "尚未生效，確認後才更新。", "hint"));
    const comparison = node("div", undefined, "compare");
    for (const [title, criteria] of [["變更前", proposal.before], ["確認後", proposal.after]]) {
      const column = node("div"); column.append(node("strong", title), node("p", describe(criteria))); comparison.append(column);
    }
    const buttons = node("div", undefined, "row");
    for (const [operation, title] of [["confirm", "確認變更"], ["cancel", "取消提案"]]) {
      const button = node("button", title, operation === "confirm" ? "primary" : ""); button.type = "button";
      action(button, async () => {
        try {
          await api(`/api/preference-proposals/${encodeURIComponent(proposal.id)}/${operation}`, {method: "POST"});
          await loadPreferences(); preferencesFeedback(operation === "confirm" ? "已確認並更新條件。" : "已取消提案，原條件保持不變。");
        } catch (error) {
          if (!csrfToken) return;
          const feedback = $(".proposal-feedback", card);
          feedback.textContent = error.status === 409 ? `${error.message} 請重新檢視最新條件後提出變更。` : error.message;
          feedback.focus(); feedback.scrollIntoView({block: "nearest"});
        }
      });
      buttons.append(button);
    }
    const feedback = node("p", undefined, "proposal-feedback notice error"); feedback.tabIndex = -1; feedback.setAttribute("role", "alert");
    card.append(comparison, buttons, feedback); $("#proposal-list").append(card);
    if (String(proposal.id) === String(proposalId)) { heading.focus(); heading.scrollIntoView({block: "nearest"}); }
  }
}
async function loadFavorites(more = false) {
  const result = await api("/api/favorites" + (more && favoriteCursor ? `?cursor=${encodeURIComponent(favoriteCursor)}` : ""));
  if (!more) $("#favorite-list").replaceChildren();
  else for (const item of [...$("#favorite-list").children]) if (!item.dataset.jobId) item.remove();
  if (!more && !result.items.length) $("#favorite-list").append(node("p", "還沒有收藏職缺，可以在聊天搜尋結果中收藏。", "hint"));
  for (const job of result.items) {
    $("#favorite-list").append(jobCard(job));
  }
  favoriteCursor = result.next_cursor; $("#more-favorites").hidden = !favoriteCursor;
}
async function loadDaily(more = false) {
  const parameters = new URLSearchParams({date: $("#daily-date").value});
  if (more && dailyCursor) parameters.set("cursor", dailyCursor);
  const result = await api("/api/daily-jobs?" + parameters);
  if (!more) $("#daily-list").replaceChildren();
  const run = result.run;
  const states = {running: "正在搜尋", success: "已完成", partial: "部分完成／範圍受限", failed: "搜尋未完成"};
  $("#daily-status").textContent = run ? `${result.local_date} · ${states[run.status]} · 新增 ${run.new_count} 筆 · 初始候選 ${run.initial_count} 筆${run.paused ? " · 搜尋已暫停" : ""}` : `這天還沒有搜尋批次。${result.last_success_at ? "最近成功時間：" + formatTaipeiDateTime(result.last_success_at) : "尚未建立初始候選。"}`;
  if (run && result.last_success_at) $("#daily-status").textContent += ` · 最近成功時間：${formatTaipeiDateTime(result.last_success_at)}`;
  $("#daily-sources").replaceChildren();
  for (const source of run?.source_results || []) $("#daily-sources").append(node("p", `${source.source} · ${source.baseline ? "建立初始基準" : "每日搜尋"} · ${source.completed ? "結果已保存" : "未保存"}${source.truncated ? " · 查詢範圍受限" : ""}${source.message ? " · " + source.message : ""}`, "hint"));
  if (!more && !result.items.length) $("#daily-list").append(node("p", run?.status === "failed" ? "來源未完成查詢，可稍後請管理者重試。" : "這天沒有新增職缺，慢慢來就好。", "hint"));
  for (const job of result.items) {
    const card = jobCard(job);
    card.prepend(node("small", job.is_initial ? "初始候選，不會推播" : "每日新發現"));
    const read = node("button", job.read_at ? "已讀" : "標記已讀", "job-read"); read.type = "button"; read.disabled = Boolean(job.read_at);
    action(read, async () => { await api(`/api/jobs/${encodeURIComponent(job.id)}/read?date=${encodeURIComponent(result.local_date)}`, {method: "POST"}); await loadDaily(); });
    card.append(read); $("#daily-list").append(card);
  }
  dailyCursor = result.next_cursor; $("#more-daily").hidden = !dailyCursor;
}
function jobCard(job, extra = {}) {
  const card = JobCards.jobCard(job, {...extra, onFavorite: (item, button) => {
    void toggleFavorite(item, button);
  }, onReview: item => {
    showReviews({company_name: item.company_name, job_id: item.id}).catch(error => message(error.message, true));
  }});
  observeReviewCard(card, job);
  return card;
}
function reviewContent(container, data, request) {
  container.append(JobCards.reviewCard(data));
  for (const text of data.limitations || []) container.append(node("p", text, "hint"));
  if (data.cached) container.append(node("small", "包含24小時內已成功取得的快取資料。"));
  for (const choice of data.choices || []) {
    const button = node("button", `${choice.source} · ${choice.company_name} · ${choice.company_id}`); button.type = "button";
    action(button, () => showReviews({company_name: choice.company_name, source: choice.source, company_id: choice.company_id}));
    container.append(button);
  }
  const refresh = node("button", "重新查詢（忽略快取）"); refresh.type = "button";
  action(refresh, () => showReviews({...request, refresh: true})); container.append(refresh);
}
async function showReviews(request) {
  const section = $("#review-result");
  if (section.getAttribute("aria-busy") === "true") return;
  const operation = ++reviewRequest;
  const requestToken = csrfToken;
  section.replaceChildren(node("p", "正在查詢公司評價…", "hint"));
  section.setAttribute("aria-busy", "true");
  const dialog = $("#review-dialog");
  if (!dialog.open) dialog.showModal();
  try {
    const result = await api("/api/reviews", {method: "POST", body: JSON.stringify(request)});
    if (operation !== reviewRequest || !csrfToken || requestToken !== csrfToken) return;
    section.replaceChildren(); reviewContent(section, result, request);
  } catch (error) {
    if (operation !== reviewRequest || !csrfToken || requestToken !== csrfToken) return;
    section.replaceChildren(node("p", error.message || "評價查詢失敗，請稍後再試。", "notice error"));
    const retry = node("button", "重試查詢"); retry.type = "button";
    action(retry, () => showReviews(request)); section.append(retry);
  } finally {
    if (operation === reviewRequest) section.setAttribute("aria-busy", "false");
  }
}
// The card feedback floats over the card (see .card-feedback), so it can appear and vanish without moving anything.
const cardFeedbackTimers = new WeakMap();
function showCardFeedback(card, text, visibleMs = 0) {
  const note = $(".card-feedback", card);
  if (cardFeedbackTimers.has(note)) clearTimeout(cardFeedbackTimers.get(note));
  cardFeedbackTimers.delete(note);
  note.textContent = text;
  if (text) cardFeedbackTimers.set(note, setTimeout(() => { cardFeedbackTimers.delete(note); note.textContent = ""; }, visibleMs));
}
async function toggleFavorite(job, button) {
  const id = String(job.id);
  if (favoritePending.has(id)) return;
  favoritePending.add(id);
  const requestToken = csrfToken;
  const cards = () => [...document.querySelectorAll(".job-card")].filter(card => card.dataset.jobId === id);
  const selected = button.getAttribute("aria-pressed") !== "true";
  const hadFocus = document.activeElement === button;
  for (const card of cards()) $("[data-favorite]", card).disabled = true;
  const origin = button.closest(".job-card");
  showCardFeedback(origin, "");
  try {
    await api(`/api/favorites/${encodeURIComponent(id)}`, {method: selected ? "PUT" : "DELETE"});
    if (!csrfToken || requestToken !== csrfToken) return;
    job.is_favorite = selected;
    for (const state of searchStates.values()) {
      const candidate = state.jobs.get(id); if (candidate) candidate.is_favorite = selected;
    }
    for (const message of $("#chat-list").children) {
      for (const candidate of chatSearchResults.get(message) || []) if (String(candidate.id) === id) candidate.is_favorite = selected;
    }
    for (const card of cards()) {
      const favorite = $("[data-favorite]", card);
      (favorite.labelElement || favorite).textContent = selected ? "取消收藏" : "收藏";
      favorite.setAttribute("aria-pressed", String(selected)); favorite.dispatchEvent(new Event("favorite-change"));
      showCardFeedback(card, selected ? "已收藏。" : "已取消收藏。", 2500);
      if (!selected && card.parentElement === $("#favorite-list")) {
        const list = $("#favorite-list");
        const adjacent = card.nextElementSibling || card.previousElementSibling;
        const restoreFocus = card.contains(document.activeElement) || (card === origin && hadFocus && document.activeElement === document.body);
        card.remove();
        let target = adjacent ? $("[data-favorite]", adjacent) : !$("#more-favorites").hidden ? $("#more-favorites") : null;
        if (!list.children.length) {
          const empty = node("p", favoriteCursor ? "這頁已沒有收藏，請載入更多查看。" : "還沒有收藏職缺，可以在聊天搜尋結果中收藏。", "hint"); empty.tabIndex = -1; list.append(empty);
          target ||= empty;
        }
        if (restoreFocus && target) target.focus();
      }
    }
    if (companionController) companionController.play("happy");
  } catch (error) {
    if (csrfToken && requestToken === csrfToken) showCardFeedback(origin, `收藏未能更新：${error.message}`, 6000);
  } finally {
    favoritePending.delete(id);
    for (const card of cards()) $("[data-favorite]", card).disabled = false;
    if (hadFocus && button.isConnected && document.activeElement === document.body) button.focus({preventScroll: true});
  }
}
function showPage(page) {
  document.querySelectorAll("[data-page]").forEach(item => {
    const active = item.dataset.page === page;
    item.setAttribute("aria-pressed", String(active)); $(`#${item.dataset.page}`).hidden = !active;
    if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute?.("aria-current");
  });
  const resultsPanel = $("#search-results-panel");
  if (resultsPanel) resultsPanel.hidden = page !== "chat" || !searchStates.size;
  if (page === "chat") scrollChatToBottom();
}
function renderChatText(container, text) {
  container.replaceChildren();
  const links = new Set();
  function inline(parent, value) {
    const pattern = /\x60([^\x60\n]+)\x60|\*\*([^\n]+?)\*\*|\[([^\]\n]+)\]\((https?:\/\/[^\s<>]+)\)|(https?:\/\/[^\s<>"'\x60*]+)/g;
    let offset = 0;
    for (const match of value.matchAll(pattern)) {
      parent.append(node("span", value.slice(offset, match.index)));
      if (match[1] !== undefined) parent.append(node("code", match[1]));
      else if (match[2] !== undefined) {
        const strong = node("strong"); inline(strong, match[2]); parent.append(strong);
      } else {
        let url = match[4] || match[5];
        if (!match[4]) {
          url = url.replace(/[.,!?;:，。！？；：、\]】」』]+$/, "");
          while (url.endsWith(")") && url.split(")").length > url.split("(").length) url = url.slice(0, -1);
        }
        const href = JobCards.safeHref(url);
        if (href) {
          links.add(href);
          const link = JobCards.externalLink(match[3] || url, href, "chat-link");
          link.addEventListener("click", () => focusJobByUrl(href));
          parent.append(link);
          if (!match[4]) parent.append(node("span", match[5].slice(url.length)));
        } else parent.append(node("span", match[0]));
      }
      offset = match.index + match[0].length;
    }
    parent.append(node("span", value.slice(offset)));
  }
  let paragraph = [], list = null;
  function flushParagraph() {
    if (paragraph.length) {
      const p = node("p"); inline(p, paragraph.join("\n")); container.append(p); paragraph = [];
    }
  }
  for (const line of String(text || "").replace(/\r\n?/g, "\n").split("\n")) {
    if (!line.trim()) { flushParagraph(); list = null; continue; }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const item = line.match(/^\s*(?:([-*])\s+|(\d+)\.\s+)(.+)$/);
    const quote = line.match(/^>\s?(.*)$/);
    if (heading || item || quote) {
      flushParagraph();
      if (item) {
        const tag = item[1] ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== tag) {
          list = node(tag);
          if (tag === "ol") list.setAttribute("start", item[2]);
          container.append(list);
        }
        const li = node("li"); inline(li, item[3]); list.append(li);
      } else {
        list = null;
        const block = node(heading ? "h" + heading[1].length : "blockquote");
        inline(block, heading ? heading[2] : quote[1]); container.append(block);
      }
    } else { list = null; paragraph.push(line); }
  }
  flushParagraph();
  return links;
}
function renderChatRecommendations(container, text) {
  const links = renderChatText($(".chat-text", container), text);
  const selected = new Map();
  const state = chatSearchMetadata.get(container) || activeSearchState;
  const directRecommendations = chatRecommendationItems.get(container) || [];
  const recommendationById = new Map(directRecommendations.map(recommendation => [String(recommendation?.job_id ?? jobIdentifier(recommendation?.job)), recommendation]));
  const candidates = new Map([...(chatSearchResults.get(container) || []), ...(state?.jobs?.values() || [])]
    .filter(job => job && jobIdentifier(job)).map(job => [JobCards.safeHref(job.url), job]));
  for (const recommendation of directRecommendations) {
    const job = recommendation?.job;
    if (job && jobIdentifier(job)) selected.set(jobIdentifier(job), job);
  }
  for (const href of links) {
    const job = candidates.get(href);
    if (job) selected.set(job.id, job);
  }
  if (state) {
    const numbers = [...state.jobs.values()];
    const refs = [...String(text || "").matchAll(/(?:職缺|候選|工作)\s*(?:#|第\s*)?(\d+)/g)].map(match => Number(match[1]));
    for (const index of refs) {
      const job = numbers.find(candidate => Number(stableJobNumber(state, candidate)) === index);
      if (job) selected.set(jobIdentifier(job), job);
    }
    for (const id of chatRecommendationIds.get(container) || []) {
      const recommendation = state.recommendations.get(id);
      const job = state.jobs.get(id);
      if (job && recommendation) selected.set(id, job);
    }
  }
  const previous = container.recommendationSection;
  if (previous) previous.remove();
  if (!selected.size) return;
  const section = node("section", undefined, "chat-jobs list");
  section.append(node("h3", "回覆中提到的職缺"));
  for (const job of selected.values()) {
    const card = jobCard(job);
    card.dataset.chatRecommendation = "true";
    const owner = [...searchStates.values()].find(candidate => candidate.jobs.has(jobIdentifier(job))) || state;
    const number = owner ? stableJobNumber(owner, job) : String(section.children.length);
    card.prepend(node("p", `職缺 #${number}`, "hint"));
    const recommendation = recommendationById.get(jobIdentifier(job)) || owner?.recommendations.get(jobIdentifier(job));
    if (recommendation) {
      const fits = JobCards.fitList(recommendation);
      if (fits) card.insertBefore(fits, card.footerElement);
    }
    card.dataset.citationNumber = number;
    section.append(card);
  }
  container.recommendationSection = section;
  container.append(section);
}
function ensureToolProgress(message) {
  if (message.toolProgressElement) return message.toolProgressElement;
  const bubble = $(".chat-bubble", message);
  if (!bubble) return null;
  const details = node("details", undefined, "chat-progress");
  details.open = true;
  details.append(node("summary", "正在處理…"));
  const body = node("div", undefined, "chat-progress-body");
  details.progressBody = body; details.progressSteps = new Map();
  details.progressStepsBody = node("div");
  body.append(details.progressStepsBody);
  details.append(body); bubble.append(details);
  message.toolProgressElement = details;
  return details;
}
function renderToolProgress(message, data) {
  if (!data || typeof data !== "object") return;
  const details = ensureToolProgress(message); if (!details) return;
  const key = String(data.call_id ?? data.id ?? data.name ?? "tool");
  details.progressSteps.set(key, {...data, key});
  details.progressStepsBody.replaceChildren();
  for (const step of details.progressSteps.values()) {
    const phaseText = {completed: "已完成", interrupted: "已中斷", failed: "未完成"}[step.phase] || "進行中";
    const text = `${step.summary || step.name || "工作"} · ${phaseText}`;
    const row = node("p", text, step.phase === "completed" ? "chat-progress-done" : ["interrupted", "failed"].includes(step.phase) ? "chat-progress-error" : "chat-progress-active");
    row.setAttribute("role", "status"); details.progressStepsBody.append(row);
    for (const source of step.source_results || []) details.progressStepsBody.append(node("small", `${source.source || "來源"} · ${sourceResultLabel(source)} · ${source.returned_count ?? "未知數"} 筆`, "hint"));
  }
  const current = [...details.progressSteps.values()].at(-1);
  const currentLabel = {completed: "工作進度（已完成）", interrupted: "工作進度（已中斷）", failed: "工作進度（未完成）"}[current?.phase];
  details.children[0].textContent = currentLabel || (current?.summary || "工作進度");
}
function finishToolProgress(message, status = "completed") {
  const details = message?.toolProgressElement;
  if (!details) return;
  details.children[0].textContent = status === "completed" ? "工作進度（已完成）" : "工作進度（已中斷）";
  details.open = false;
}
function renderChatEvent(message, name, data, historical = false) {
  const container = $(".chat-bubble", message);
  if (name === "preference_proposal") {
    const card = node("article", undefined, "item");
    card.append(node("h3", "條件變更提案"), node("p", `原條件：${describe(data.before)}`), node("p", `提案：${describe(data.after)}`), node("small", "請到我的條件查看目前狀態；確認後才會生效。"));
    const button = node("button", "檢視並決定"); button.type = "button";
    action(button, async () => { showPage("preferences"); await loadPreferences(data.id); });
    card.append(button); container.append(card);
  } else if (name === "jobs") {
    const state = updateSearchState(data, message, {historical});
    const details = node("details", undefined, "chat-search-log");
    details.append(node("summary", "搜尋紀錄與限制"));
    for (const source of data.source_results || []) {
      const count = source.returned_count ?? source.count ?? "未知數";
      const countText = (source.quota_limited || source.truncated) ? "" : ` · ${count}筆候選`;
      details.append(node("p", `${source.source || "來源"}：${sourceResultLabel(source)}${countText}${source.truncated ? " · 尚有結果未掃描" : ""}${source.message ? " · " + source.message : ""}`, "notice"));
    }
    appendAreaResults(details, data.area_results);
    for (const warning of data.filter_warnings || []) details.append(node("p", warning, "hint"));
    const progress = ensureToolProgress(message);
    if (progress?.progressBody) progress.progressBody.append(details); else container.append(details);
    if (state) {
      renderSearchResultsPanel();
      if (message.resultsButton) message.resultsButton.remove();
      const display = resultState() || state;
      const resultsButton = node("button", `查看全部職缺（${display.jobs.size}）`); resultsButton.type = "button";
      action(resultsButton, () => { resultSearchId = null; renderSearchResultsPanel(); showSearchPanel(); setResultView("results"); });
      message.resultsButton = resultsButton; container.append(resultsButton);
    }
  } else if (name === "review") reviewContent(container, data, {company_name: data.company_name});
  else if (name === "recommendations") updateRecommendations(data, message, {historical});
  else if (name === "job_detail") {
    const item = data.identity_verified && data.item ? data.item : null;
    const detailId = String(data.job_id || jobIdentifier(item));
    let state = chatSearchMetadata.get(message) || activeSearchState;
    if (detailId && !state?.jobs.has(detailId)) state = [...searchStates.values()].find(candidate => candidate.jobs.has(detailId)) || state;
    if (item && state) {
      const updated = mergeJob(state, {...item, id: data.job_id || item.id});
      for (const job of chatSearchResults.get(message) || []) if (jobIdentifier(job) === jobIdentifier(updated)) Object.assign(job, updated);
      renderSearchResultsPanel();
    }
    const details = node("details", undefined, "job-detail-event");
    details.append(node("summary", data.identity_verified ? "職缺詳細內容（已核對，點開閱讀）" : "職缺詳細內容（未核對，點開閱讀）"));
    const detail = data.detail && typeof data.detail === "object" ? data.detail : {};
    const fields = [["職稱", detail.title || item?.title], ["公司", detail.company_name || item?.company_name], ["地點", detail.location || item?.location], ["薪資", detail.salary_text || item?.salary_text], ["發布日期", detail.published_at || item?.published_at], ["摘要", detail.summary || item?.summary]];
    let rendered = false;
    for (const [label, value] of fields) if (value) { details.append(node("p", `${label}：${value}`)); rendered = true; }
    if (!rendered) details.append(node("p", "來源未提供可用詳細內容。"));
    for (const warning of data.limitations || []) details.append(node("p", warning, "hint"));
    const progress = ensureToolProgress(message);
    if (progress?.progressBody) progress.progressBody.append(details); else container.append(details);
  }
  else if (name === "error") container.append(node("p", data.message, "notice error"));
}
function setConversationHistoryStatus(text, failed = false) {
  const status = $("#conversation-history-status");
  status.textContent = text; status.classList.toggle("error", failed);
  status.setAttribute("aria-live", failed ? "assertive" : "polite");
}
function updateConversationControls() {
  const blocked = Boolean(activeChat || switchingConversation);
  $("#new-conversation").disabled = blocked || !csrfToken;
  $("#open-conversation-history").disabled = !csrfToken;
  $("#chat-form button[type=submit]").disabled = blocked || !selectedConversationId;
  $("#refresh-messages").disabled = Boolean(activeChat && !activeChat.awaitingSnapshot) || switchingConversation || !selectedConversationId;
  $("#more-messages").disabled = blocked;
  syncContinueButton();
  for (const chip of $("#search-results-list")?.querySelectorAll?.(".excluded-company-chips button") || []) {
    chip.disabled = blocked || !csrfToken;
  }
  for (const card of $("#conversation-list").children) {
    const id = card.dataset.conversationId;
    const open = card.querySelector("[data-conversation-open]");
    const remove = card.querySelector("[data-conversation-delete]");
    if (open) open.disabled = blocked || id === selectedConversationId;
    if (remove) remove.disabled = switchingConversation || Boolean(activeChat && id === selectedConversationId);
  }
}
function renderConversationItem(conversation) {
  const id = String(conversation.id);
  const current = id === selectedConversationId;
  const card = node("article", undefined, "item conversation-item");
  card.dataset.conversationId = id; card.setAttribute("aria-current", String(current));
  card.append(node("h3", conversation.title || "新對話", "conversation-title"));
  card.append(node("p", `${Number(conversation.message_count) || 0} 則訊息 · 更新 ${formatTaipeiDateTime(conversation.updated_at)}`, "hint"));
  const buttons = node("div", undefined, "row");
  const open = node("button", current ? "目前對話" : "開啟對話"); open.type = "button"; open.dataset.conversationOpen = id; open.disabled = current || Boolean(activeChat || switchingConversation);
  open.setAttribute("aria-pressed", String(current));
  open.addEventListener("click", () => { void openConversation(conversation); });
  const rename = node("button", "改名稱"); rename.type = "button";
  rename.addEventListener("click", () => showRenameConversation(conversation));
  const remove = node("button", "刪除"); remove.type = "button"; remove.dataset.conversationDelete = id;
  remove.disabled = Boolean(activeChat && current) || switchingConversation;
  remove.addEventListener("click", () => showDeleteConversation(conversation));
  buttons.append(open, rename, remove); card.append(buttons);
  return card;
}
async function loadConversations(more = false, query = $("#conversation-query").value.trim()) {
  const request = ++conversationListRequest;
  const requestToken = csrfToken;
  const status = $("#conversation-history-status");
  const list = $("#conversation-list");
  const moreButton = $("#more-conversations");
  if (!more) { conversationCursor = null; list.replaceChildren(); }
  status.setAttribute("aria-busy", "true");
  setConversationHistoryStatus(more ? "正在載入更多對話…" : "正在載入對話…");
  moreButton.disabled = true;
  const parameters = new URLSearchParams({limit: "20"});
  if (query) parameters.set("q", query);
  if (more && conversationCursor) parameters.set("cursor", conversationCursor);
  try {
    const result = await api(`/api/conversations?${parameters}`);
    if (!csrfToken || requestToken !== csrfToken || request !== conversationListRequest) return null;
    const items = Array.isArray(result?.items) ? result.items : [];
    if (!more && !items.length) list.append(node("p", query ? "找不到符合的對話。" : "目前沒有對話，按「新對話」開始。", "hint"));
    for (const item of items) list.append(renderConversationItem(item));
    conversationCursor = result.next_cursor || null;
    moreButton.hidden = !conversationCursor;
    setConversationHistoryStatus(`已載入 ${[...list.children].filter(item => item.dataset.conversationId).length} 段對話。`);
    updateConversationControls();
    return {items, next_cursor: conversationCursor};
  } catch (error) {
    if (csrfToken && requestToken === csrfToken && request === conversationListRequest) setConversationHistoryStatus(error.message, true);
    throw error;
  } finally {
    if (request === conversationListRequest) status.setAttribute("aria-busy", "false");
    moreButton.disabled = false;
  }
}
function showNoConversations() {
  if (!csrfToken) return;
  const hadEdit = Boolean(editOrigin);
  cancelMessageEdit(false);
  selectedConversationId = null; messageCursor = null;
  $("#active-conversation-title").textContent = "尚未選取對話";
  $("#chat-list").replaceChildren(node("p", "目前沒有對話，按「新對話」開始。", "hint"));
  $("#more-messages").hidden = true; $("#chat-transcript").scrollTop = 0; $("#latest-message").hidden = true;
  setChatStatus(hadEdit ? "原對話已刪除；編輯文字與附件保留為草稿，新增對話後可另作新訊息。" : "目前沒有對話，請先新增一段對話。", "complete");
  updateConversationControls();
}
async function openConversation(conversation, closeHistory = true) {
  const id = String(conversation.id ?? "");
  if (!id) { setConversationHistoryStatus("找不到這段對話的識別資料。", true); return false; }
  if (activeChat || switchingConversation) { setConversationHistoryStatus("回覆進行中，請等候完成後再切換對話。", true); return false; }
  if (id === selectedConversationId) {
    $("#active-conversation-title").textContent = conversation.title || "新對話";
    if (closeHistory && $("#conversation-history-dialog").open) $("#conversation-history-dialog").close();
    return true;
  }
  // Stop reviews from the previous conversation before loading its messages;
  // otherwise a same-company card can be blocked by an obsolete pending key.
  resetBasicReviews();
  const requestToken = csrfToken;
  switchingConversation = true; updateConversationControls();
  setConversationHistoryStatus("正在載入這段對話…");
  try {
    const loaded = await loadMessages(false, id);
    if (!loaded || !csrfToken || requestToken !== csrfToken) return false;
    if (editOrigin) {
      cancelMessageEdit(false);
      setChatStatus("已切換對話；編輯文字與附件保留為草稿，送出會建立新訊息。", "complete");
    }
    $("#active-conversation-title").textContent = conversation.title || "新對話";
    if (!$("#chat-list").children.length) setChatStatus("這段對話還沒有訊息，輸入一句話即可開始。", "complete");
    if (closeHistory && $("#conversation-history-dialog").open) $("#conversation-history-dialog").close();
    return true;
  } catch (error) {
    if (csrfToken && requestToken === csrfToken) {
      setConversationHistoryStatus(error.message, true);
      setChatStatus(error.message, "error");
    }
    return false;
  } finally {
    switchingConversation = false; updateConversationControls();
  }
}
function showRenameConversation(conversation) {
  const dialog = $("#rename-conversation-dialog");
  dialog.dataset.conversationId = String(conversation.id);
  $("#rename-conversation-input").value = conversation.title || "";
  $("#rename-conversation-status").textContent = "";
  dialog.showModal(); $("#rename-conversation-input").focus(); $("#rename-conversation-input").select();
}
function showDeleteConversation(conversation) {
  const dialog = $("#delete-conversation-dialog");
  dialog.dataset.conversationId = String(conversation.id);
  $("#delete-conversation-name").textContent = `確定刪除「${conversation.title || "新對話"}」及其中的訊息嗎？`;
  $("#delete-conversation-status").textContent = "";
  dialog.showModal();
}
async function createConversation() {
  if (activeChat || switchingConversation) { setChatStatus("回覆進行中，請等候完成後再新增對話。", "error"); return false; }
  const requestToken = csrfToken;
  if (!requestToken) return false;
  switchingConversation = true; updateConversationControls();
  setChatStatus("正在建立新對話…");
  try {
    const conversation = await api("/api/conversations", {method: "POST", body: JSON.stringify({title: "新對話"})});
    if (!csrfToken || requestToken !== csrfToken) return false;
    switchingConversation = false;
    if (!await openConversation(conversation, false)) {
      setChatStatus("新對話已建立，但目前無法載入，請從對話紀錄重新開啟。", "error");
      $("#conversation-query").value = "";
      await loadConversations(false, "").catch(() => {});
      return false;
    }
    $("#conversation-query").value = "";
    await loadConversations(false, "").catch(error => setConversationHistoryStatus(error.message, true));
    setChatStatus("已建立新對話。", "complete");
    $("#chat-form [name=text]").focus();
    return true;
  } catch (error) {
    if (csrfToken && requestToken === csrfToken) setChatStatus(error.message, "error");
    return false;
  } finally { switchingConversation = false; updateConversationControls(); }
}
async function initializeConversations() {
  try {
    const result = await loadConversations(false, "");
    if (!csrfToken || !result) return;
    if (result.items.length) {
      if (!await openConversation(result.items[0], false) && csrfToken) setChatStatus("無法載入最近的對話，請從對話紀錄重新開啟。", "error");
    } else showNoConversations();
  } catch (error) {
    if (!csrfToken) return;
    $("#chat-list").replaceChildren(node("p", "目前無法載入對話，請稍後重新整理。", "hint"));
    setChatStatus(error.message, "error"); updateConversationControls();
  }
}
function chatActionIcon() {
  const svgNamespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNamespace, "svg");
  svg.setAttribute("class", "icon chat-action-icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svgNamespace, "use");
  use.setAttribute("href", "/static/icons.svg#edit");
  svg.append(use);
  return svg;
}
function chatMessage(role, text, status = "completed", events = [], attachments = [], createdAt = null, requestId = null, messageId = null, historical = false) {
  const card = node("article", undefined, `chat-message ${role === "user" ? "chat-user" : ""}`);
  if (requestId) card.dataset.requestId = String(requestId);
  card.dataset.role = role;
  if (messageId !== null && messageId !== undefined) card.dataset.messageId = String(messageId);
  if (role !== "user") {
    const avatar = node("div", undefined, "chat-avatar");
    avatar.setAttribute("aria-hidden", "true");
    const portrait = node("div", undefined, "companion"); renderPortrait(portrait);
    avatar.append(portrait); card.append(avatar);
  }
  const bubble = node("div", undefined, "chat-bubble");
  bubble.append(node("strong", role === "user" ? "你" : "小幫手"), node("div", undefined, "chat-text"));
  for (const attachment of attachments) bubble.append(node("small", `附件：${attachment.filename}${attachment.pages ? ` · ${attachment.pages} 頁` : ""}`, "hint"));
  if (role === "user") {
    const meta = node("div", undefined, "chat-message-meta");
    const date = createdAt ? new Date(createdAt) : null;
    if (date && !Number.isNaN(date.getTime())) {
      const stamp = node("time", formatTaipeiDateTime(createdAt), "chat-time");
      stamp.dateTime = date.toISOString(); meta.append(stamp);
    }
    const actions = node("div", undefined, "chat-message-actions");
    const edit = node("button", undefined, "chat-message-edit"); edit.type = "button";
    edit.append(chatActionIcon());
    edit.setAttribute("aria-label", "編輯這則訊息"); edit.title = "編輯這則訊息";
    edit.disabled = messageId === null || messageId === undefined || !requestId;
    edit.addEventListener("click", () => prepareMessageEdit(text, attachments, messageId, requestId, card));
    actions.append(edit); meta.append(actions); card.append(bubble, meta);
  } else {
    card.append(bubble);
  }
  for (const event of events) {
    if (event.event === "tool_status") renderToolProgress(card, event.data);
    else renderChatEvent(card, event.event, event.data, historical);
  }
  renderChatRecommendations(card, text);
  if (status === "completed" || status === "interrupted" || status === "failed") finishToolProgress(card, status);
  if (status !== "completed") {
    card.chatStatusElement = node("small", status === "running" ? "回覆正在進行；重新整理可查看最新內容。" : "回覆未完成，已取得的內容已保留。", "hint");
    bubble.append(card.chatStatusElement);
  }
  const date = createdAt ? new Date(createdAt) : null;
  if (role !== "user" && date && !Number.isNaN(date.getTime())) {
    const stamp = node("time", formatTaipeiDateTime(createdAt), "chat-time");
    stamp.dateTime = date.toISOString(); bubble.append(stamp);
  }
  return card;
}
async function loadMessages(more = false, conversationId = selectedConversationId) {
  if (activeChat || !conversationId) return false;
  const id = String(conversationId);
  const request = ++messageLoadRequest;
  const requestToken = csrfToken;
  const parameters = new URLSearchParams({conversation_id: id});
  if (more && messageCursor) parameters.set("cursor", messageCursor);
  const result = await api(`/api/messages?${parameters}`);
  if (!csrfToken || requestToken !== csrfToken || request !== messageLoadRequest) return false;
  const changed = id !== selectedConversationId;
  const viewport = $("#chat-transcript");
  const top = viewport.scrollTop;
  const height = viewport.scrollHeight;
  const follow = !$("#chat-list").children.length || chatAtBottom();
  if (changed || !more) {
    activeSearchId = null; activeSearchState = null; resultSearchId = null; searchStates.clear(); resetConversationWork(); resetBasicReviews();
    const panel = $("#search-results-panel"); if (panel) panel.hidden = true;
    replayConversationSearches(result.conversation_state?.searches);
  }
  const fragment = document.createDocumentFragment();
  for (const item of result.items) {
    const requestId = item.request_id ? String(item.request_id) : null;
    fragment.append(chatMessage(item.role, item.content.text, item.content.status, item.content.events, item.content.attachments, item.created_at, requestId, item.id, true));
  }
  if (more) $("#chat-list").prepend(fragment);
  else $("#chat-list").replaceChildren(fragment);
  selectedConversationId = id;
  applyConversationWork(result.conversation_state);
  renderSearchResultsPanel();
  messageCursor = result.next_cursor; $("#more-messages").hidden = !messageCursor;
  if (changed) viewport.scrollTop = 0;
  else if (more) viewport.scrollTop = top + viewport.scrollHeight - height;
  else if (follow) scrollChatToBottom();
  else viewport.scrollTop = top;
  updateLatestMessage();
  updateConversationControls();
  return true;
}
function chatAtBottom() {
  const viewport = $("#chat-transcript");
  return viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 24;
}
function updateLatestMessage() {
  $("#latest-message").hidden = chatAtBottom();
}
function scrollChatToBottom() {
  $("#chat-transcript").scrollTop = $("#chat-transcript").scrollHeight;
  $("#latest-message").hidden = true;
}
function scrollChatToReplyStart(element) {
  if (element && typeof element.scrollIntoView === "function") {
    element.scrollIntoView({block: "start", inline: "nearest"});
    updateLatestMessage();
  } else scrollChatToBottom();
}
function chatAbortError() {
  const error = new Error("回覆處理已停止。");
  error.name = "AbortError";
  return error;
}
function waitForChatRetry(operation, attempt, text) {
  if (operation.controller.signal.aborted) return Promise.reject(chatAbortError());
  setChatStatus(text || `連線中斷，${CHAT_RETRY_DELAY_MS / 1000} 秒後重試（${attempt}/${CHAT_RETRY_LIMIT}）。`);
  return new Promise((resolve, reject) => {
    let timer = setTimeout(finish, CHAT_RETRY_DELAY_MS);
    function cleanup() {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      operation.controller.signal.removeEventListener("abort", abort);
    }
    function finish() { cleanup(); resolve(); }
    function abort() { cleanup(); reject(chatAbortError()); }
    operation.controller.signal.addEventListener("abort", abort, {once: true});
  });
}
function chatOperationIsCurrent(operation) {
  return activeChat === operation && csrfToken === operation.csrfToken
    && String(selectedConversationId) === operation.conversationId;
}
function clearAcceptedComposer(operation) {
  if (operation.composerCleared) return;
  const input = $("#chat-form [name=text]");
  if (input.value.trim() === operation.text.trim()) input.value = "";
  const selected = [...($("#resume-file").files || [])];
  if (operation.attachments.length && selected.length === operation.attachments.length && selected.every((file, index) => file === operation.attachments[index])) {
    $("#resume-file").value = "";
    previousValidAttachments = [];
    updateResumeSelection();
  }
  operation.composerCleared = true;
  if (operation.editMessage && editOrigin === operation.editMessage) {
    editOrigin = null;
    $("#message-edit-banner").hidden = true;
    $("#message-edit-status").textContent = "";
  }
  updateChatCount();
}
function cancelMessageEdit(showStatus = true) {
  if (!editOrigin) return;
  editOrigin = null;
  $("#message-edit-banner").hidden = true;
  $("#message-edit-status").textContent = "";
  if (showStatus) setChatStatus("已取消編輯；輸入框文字與附件仍保留，可作為新訊息送出。", "complete");
}
function getChatSnapshot(operation) {
  return api(`/api/chat/${encodeURIComponent(operation.id)}`, {signal: operation.controller.signal});
}
function chatMessageStatus(item) {
  return typeof item?.content?.status === "string" ? item.content.status : "completed";
}
function replaceChatPair(operation, userMessage, assistantMessage, userCard, assistantCard) {
  const list = $("#chat-list");
  if (!operation.editMessage) {
    for (const child of [...list.children]) if (child.dataset.requestId === operation.id) child.remove();
    list.append(userCard, assistantCard);
  } else {
    const origin = operation.editMessage;
    const oldUser = (origin.element?.parentElement === list && origin.element)
      || [...list.children].find(item => item.dataset.messageId === String(userMessage.id ?? origin.messageId));
    const oldAssistant = [...list.children].find(item => item.dataset.role === "assistant"
      && [origin.requestId, operation.id].includes(item.dataset.requestId));
    if (oldUser && oldUser.parentElement === list) oldUser.replaceWith(userCard);
    else if (oldAssistant && oldAssistant.parentElement === list) list.insertBefore(userCard, oldAssistant);
    else list.append(userCard, assistantCard);
    if (oldAssistant && oldAssistant.parentElement === list) oldAssistant.replaceWith(assistantCard);
    else if (userCard.parentElement === list && assistantCard.parentElement !== list) list.insertBefore(assistantCard, userCard.nextElementSibling);
  }
  operation.userElement = userCard;
  operation.replyElement = assistantCard;
  operation.messagesAdded = true;
}
function renderChatSnapshot(operation, snapshot) {
  if (!chatOperationIsCurrent(operation)) return null;
  if (snapshot?.request_id !== operation.id || String(snapshot.conversation_id) !== operation.conversationId
    || !Array.isArray(snapshot.items)) throw new Error("回覆狀態資料不完整，請重新整理歷史確認。");
  const user = snapshot.items.find(item => item.role === "user" && item.request_id === operation.id);
  const assistant = snapshot.items.find(item => item.role === "assistant" && item.request_id === operation.id);
  if (!user || !assistant || !user.content || !assistant.content) throw new Error("回覆狀態資料不完整，請重新整理歷史確認。");
  if (operation.editMessage && String(user.id) !== String(operation.editMessage.messageId)) throw new Error("原訊息版本已變更，請重新整理歷史後再編輯。");
  const status = chatMessageStatus(assistant);
  if (!["running", "completed", "interrupted", "failed"].includes(status)) throw new Error("回覆狀態無法辨識，請重新整理歷史確認。");
  operation.accepted = true;
  clearAcceptedComposer(operation);
  replaceChatPair(operation, user, assistant,
    chatMessage("user", user.content.text || "", user.content.status || "completed", [], user.content.attachments || [], user.created_at, operation.id, user.id),
    chatMessage("assistant", assistant.content.text || "", status, assistant.content.events || [], assistant.content.attachments || [], assistant.created_at, operation.id, assistant.id));
  operation.snapshotRestored = true;
  operation.awaitingSnapshot = status === "running";
  scrollChatToBottom();
  if (status === "running") setChatStatus("已恢復目前回覆進度；伺服器仍在處理，正在確認狀態…");
  else if (status === "completed") setChatStatus("回覆已完成。", "complete");
  else setChatStatus("這則回覆未完成；可按原訊息旁的鉛筆編輯後送出。", "error");
  return status;
}
async function restoreCompletedEditSnapshot(operation) {
  try {
    const snapshot = await getChatSnapshot(operation);
    if (!chatOperationIsCurrent(operation)) return false;
    const user = snapshot?.items?.find(item => item.role === "user" && item.request_id === operation.id);
    const assistant = snapshot?.items?.find(item => item.role === "assistant" && item.request_id === operation.id);
    if (!user || !assistant || String(user.id) !== String(operation.editMessage.messageId)
      || chatMessageStatus(assistant) !== "completed") return false;
    return renderChatSnapshot(operation, snapshot) === "completed";
  } catch (_) {
    return false;
  }
}
async function recoverChatSnapshot(operation, firstSnapshot) {
  let snapshot = firstSnapshot;
  let sawSnapshot = firstSnapshot !== undefined && firstSnapshot !== null;
  for (let attempt = 0; attempt <= CHAT_RETRY_LIMIT; attempt++) {
    if (!chatOperationIsCurrent(operation)) throw chatAbortError();
    if (attempt > 0) await waitForChatRetry(operation, attempt, `正在確認回覆狀態（${attempt}/${CHAT_RETRY_LIMIT}），不會重新送出訊息。`);
    if (attempt > 0 || snapshot === undefined || snapshot === null) {
      try {
        snapshot = await getChatSnapshot(operation);
        sawSnapshot = true;
      } catch (error) {
        if (error.name === "AbortError" || error.status === 401 || error.status === 403) throw error;
        snapshot = null;
      }
    }
    if (!chatOperationIsCurrent(operation)) throw chatAbortError();
    if (!snapshot) continue;
    const status = renderChatSnapshot(operation, snapshot);
    if (!status) throw chatAbortError();
    if (status !== "running") return {status, snapshot, sawSnapshot: true};
  }
  return {status: "running", snapshot: sawSnapshot ? snapshot : null, sawSnapshot};
}
function unlockChatAfterRecovery(operation) {
  if (activeChat !== operation) return;
  operation.awaitingSnapshot = false;
  activeChat = null;
  clearCompanionTransient();
  const input = $("#chat-form [name=text]");
  input.readOnly = false;
  $("#resume-file").disabled = false; $("#remove-resume").disabled = false;
  $("#chat-form button[type=submit]").disabled = false;
  $("#chat-form button[type=submit]").textContent = "送出";
  $("#cancel-chat").hidden = true;
  updateConversationControls();
}
function prepareMessageEdit(text, attachments = [], messageId, requestId, element) {
  const input = $("#chat-form [name=text]");
  const file = $("#resume-file").files?.length;
  if (activeChat || switchingConversation) {
    setChatStatus("回覆進行中，請等候完成後再編輯。", "error");
    return;
  }
  if (messageId === null || messageId === undefined || !requestId) {
    setChatStatus("無法確認這則訊息的版本，請重新整理歷史後再試。", "error");
    return;
  }
  if (editOrigin || file || (input.value.trim() && input.value.trim() !== String(text).trim())) {
    setChatStatus("請先送出或清除目前的草稿與附件，再編輯這則訊息。", "error");
    input.focus();
    return;
  }
  editOrigin = {messageId: String(messageId), requestId: String(requestId), conversationId: String(selectedConversationId), element};
  input.value = String(text).slice(0, input.maxLength || 10000);
  updateChatCount();
  const attachmentNote = attachments.length ? " 歷史附件原檔未保存，請重新附加原附件或替換成新附件後送出。" : "";
  $("#message-edit-status").textContent = `編輯這則訊息，重新生成這次回覆；後面的對話會保留。${attachmentNote}`;
  $("#message-edit-banner").hidden = false;
  input.focus();
  setChatStatus("原訊息已載入輸入框；送出後會替換該訊息與這次回覆。", "complete");
}
if ($("#show-chat-view")) $("#show-chat-view").addEventListener("click", () => setResultView("chat"));
if ($("#show-results-view")) $("#show-results-view").addEventListener("click", () => { showSearchPanel(); setResultView("results"); });
// Filter/sort/continue controls live in a native modal dialog; focus returns to the opener on close.
if ($("#open-result-options")) {
  const opener = $("#open-result-options"); const dialog = $("#result-options-dialog");
  opener.addEventListener("click", () => { if (!dialog.open) dialog.showModal(); });
  $("#close-result-options").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => opener.focus());
}
// One "⋯" menu pattern for the header (narrow screens: theme/logout) and the chat heading (history/reload).
// The menu items keep their own handlers; choosing a chat item only closes the menu.
function wireMenu(toggle, host, panel = null) {
  const setMenu = open => { toggle.setAttribute("aria-expanded", String(open)); host.classList.toggle("menu-open", open); };
  toggle.addEventListener("click", () => setMenu(toggle.getAttribute("aria-expanded") !== "true"));
  if (panel) panel.addEventListener("click", () => { setMenu(false); toggle.focus(); });
  document.addEventListener("click", event => { if (!host.contains(event.target)) setMenu(false); });
  document.addEventListener("keydown", event => { if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") { setMenu(false); toggle.focus(); } });
}
if ($("#bar-menu-toggle")) wireMenu($("#bar-menu-toggle"), $("#bar-menu-toggle").closest(".bar"));
wireMenu($("#chat-menu-toggle"), $("#chat-menu"), $("#chat-menu-actions"));
// Desktop chat/results splitter. The chat column width is --chat-w (px) on #user-panel; without it the CSS default applies.
const PANEL_CHAT_MIN = 360;
const PANEL_RESULTS_MIN = 400; // one 320px result card plus panel padding, border and a 15px scrollbar
const PANEL_SPLITTER = 14; // splitter track width in style.css
const PANEL_WIDTH_KEY = "job-companion-chat-width";
let chatWidthPreference = readChatWidthPreference();
let panelDrag = null;
function chatWidthBounds(available) {
  return {min: PANEL_CHAT_MIN, max: Math.max(PANEL_CHAT_MIN, available - PANEL_RESULTS_MIN - PANEL_SPLITTER)};
}
function clampChatWidth(px, available) {
  const {min, max} = chatWidthBounds(available);
  return Math.round(Math.min(max, Math.max(min, px)));
}
function readChatWidthPreference() {
  try {
    const value = Number(window.localStorage.getItem(PANEL_WIDTH_KEY));
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch (_) { return null; }
}
function saveChatWidthPreference(px) {
  chatWidthPreference = px;
  try {
    if (px === null) window.localStorage.removeItem(PANEL_WIDTH_KEY);
    else window.localStorage.setItem(PANEL_WIDTH_KEY, String(px));
  } catch (_) {}
}
function currentChatWidth() { return Math.round($("#chat").getBoundingClientRect().width); }
// px is clamped to the current panel width; null restores the CSS default. Returns the clamped px, or null when the panel is hidden or px is null.
function applyChatWidth(px) {
  const panel = $("#user-panel"); const resizer = $("#panel-resizer");
  if (panel.clientWidth <= 0) return null;
  const width = px === null ? null : clampChatWidth(px, panel.clientWidth);
  if (width === null) panel.style.removeProperty("--chat-w");
  else panel.style.setProperty("--chat-w", `${width}px`);
  const bounds = chatWidthBounds(panel.clientWidth);
  resizer.setAttribute("aria-valuemin", String(bounds.min));
  resizer.setAttribute("aria-valuemax", String(bounds.max));
  resizer.setAttribute("aria-valuenow", String(currentChatWidth()));
  return width;
}
function resetChatWidth() {
  saveChatWidthPreference(null);
  applyChatWidth(null);
}
function wirePanelResizer() {
  const panel = $("#user-panel"); const resizer = $("#panel-resizer");
  // Re-clamp when the panel resizes or the splitter appears (results column shown), keeping width and aria values current.
  const observer = new ResizeObserver(() => applyChatWidth(chatWidthPreference));
  observer.observe(panel); observer.observe(resizer);
  resizer.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); resetChatWidth(); return; }
    const current = currentChatWidth(); const {min, max} = chatWidthBounds(panel.clientWidth); const step = event.shiftKey ? 64 : 16;
    const target = {ArrowLeft: current - step, ArrowRight: current + step, Home: min, End: max}[event.key];
    if (target === undefined) return;
    event.preventDefault();
    const width = applyChatWidth(target);
    if (width !== null) saveChatWidthPreference(width);
  });
  resizer.addEventListener("dblclick", resetChatWidth);
  resizer.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    event.preventDefault(); // keeps text from being selected during the drag; focus is moved explicitly below
    resizer.setPointerCapture(event.pointerId);
    resizer.focus();
    panelDrag = {id: event.pointerId, startX: event.clientX, x: event.clientX, startWidth: currentChatWidth(), moved: false, frame: 0};
  });
  resizer.addEventListener("pointermove", event => {
    if (!panelDrag || event.pointerId !== panelDrag.id) return;
    panelDrag.x = event.clientX;
    if (event.clientX !== panelDrag.startX) panelDrag.moved = true;
    if (!panelDrag.frame) panelDrag.frame = requestAnimationFrame(() => {
      if (!panelDrag) return;
      panelDrag.frame = 0;
      applyChatWidth(panelDrag.startWidth + panelDrag.x - panelDrag.startX);
    });
  });
  const endDrag = event => {
    if (!panelDrag || event.pointerId !== panelDrag.id) return;
    const drag = panelDrag; panelDrag = null;
    cancelAnimationFrame(drag.frame);
    if (!drag.moved) return; // a plain click or double-click must not store the default width
    const width = applyChatWidth(drag.startWidth + drag.x - drag.startX);
    if (width !== null) saveChatWidthPreference(width);
  };
  resizer.addEventListener("pointerup", endDrag);
  resizer.addEventListener("pointercancel", endDrag);
  resizer.addEventListener("lostpointercapture", endDrag);
}
wirePanelResizer();
$("#chat-transcript").addEventListener("scroll", updateLatestMessage);
window.addEventListener("resize", updateChatCount);
$("#latest-message").addEventListener("click", scrollChatToBottom);
function setChatStatus(text, state = "pending") {
  const status = $("#chat-status");
  status.dataset.state = state;
  status.setAttribute("aria-live", state === "error" ? "assertive" : "polite");
  status.textContent = text;
  renderPortrait($("#status-companion"), 44, state === "pending" ? "thinking" : "idle");
  if (state === "error") status.scrollIntoView({block: "nearest"});
}
function updateChatCount() {
  const input = $("#chat-form [name=text]");
  const follow = chatAtBottom();
  $("#chat-count").textContent = `${input.value.length}/10000`;
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
  if (follow) scrollChatToBottom();
  else updateLatestMessage();
}
document.querySelectorAll("[data-chat-draft]").forEach(button => {
  button.addEventListener("click", () => {
    const input = $("#chat-form [name=text]");
    if (input.readOnly) return;
    input.value = (input.value.trim() ? input.value.trimEnd() + "\n" : "") + button.dataset.chatDraft;
    input.value = input.value.slice(0, input.maxLength || 10000);
    updateChatCount(); input.focus();
  });
});
$("#toggle-quick-options").addEventListener("click", () => {
  const panel = $("#quick-options"); panel.hidden = !panel.hidden;
  $("#toggle-quick-options").setAttribute("aria-expanded", String(!panel.hidden));
  $("#toggle-quick-options").textContent = panel.hidden ? "展開建議" : "收起建議";
});
const MAX_ATTACHMENTS = 5;
const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;
const MAX_ATTACHMENT_TOTAL_BYTES = 50 * 1024 * 1024;
function attachmentKind(file) {
  const extension = String(file?.name || "").split(".").pop().toLowerCase();
  if (extension === "pdf") return "PDF";
  if (["png", "jpg", "jpeg", "webp"].includes(extension)) return "圖片";
  if (["md", "markdown", "txt"].includes(extension)) return "文件";
  return "";
}
function attachmentError(files) {
  if (!files?.length) return "沒有可附加的檔案；原附件與訊息草稿已保留。";
  if (files.length > MAX_ATTACHMENTS) return `最多附加 ${MAX_ATTACHMENTS} 個檔案；原附件與訊息草稿已保留。`;
  for (const file of files) {
    if (!file.size) return `${file.name} 是空檔案；原附件與訊息草稿已保留。`;
    if (file.size > MAX_ATTACHMENT_BYTES) return `${file.name} 超過 20 MiB 上限；原附件與訊息草稿已保留。`;
    if (!attachmentKind(file)) return "支援 PDF、PNG／JPEG／WebP、Markdown 或 TXT；原附件與訊息草稿已保留。";
  }
  if (files.reduce((total, file) => total + file.size, 0) > MAX_ATTACHMENT_TOTAL_BYTES) return "附件合計超過 50 MiB；原附件與訊息草稿已保留。";
  return "";
}
// New picks and drops add to the current selection; the same file picked twice is kept once.
function mergeAttachments(current, added) {
  const key = file => [file.name, file.size, file.lastModified ?? ""].join("|");
  const seen = new Set(current.map(key));
  return [...current, ...added.filter(file => !seen.has(key(file)) && seen.add(key(file)))];
}
function setAttachmentInput(files) {
  const input = $("#resume-file");
  if (!files.length) { input.value = ""; return true; }
  try {
    const transfer = new DataTransfer();
    for (const file of files) transfer.items.add(file);
    input.files = transfer.files;
    return true;
  } catch (_) {
    input.value = ""; return false;
  }
}
function removeAttachment(file) {
  if (activeChat) return;
  previousValidAttachments = [...($("#resume-file").files || [])].filter(item => item !== file);
  if (!setAttachmentInput(previousValidAttachments)) previousValidAttachments = [];
  updateResumeSelection(); updateChatCount();
}
function updateResumeSelection() {
  const files = [...($("#resume-file").files || [])];
  $("#resume-selection").hidden = !files.length;
  const list = $("#resume-name"); list.replaceChildren();
  for (const file of files) {
    const chip = node("span", undefined, "attachment-chip");
    chip.append(node("span", `${file.name} · ${attachmentKind(file)} · ${(file.size / 1024).toFixed(0)} KiB`));
    const remove = node("button", "×"); remove.type = "button";
    remove.setAttribute("aria-label", `移除 ${file.name}`); remove.title = `移除 ${file.name}`;
    remove.addEventListener("click", () => removeAttachment(file));
    chip.append(remove); list.append(chip);
  }
  // A separate "remove all" only helps once there is more than one file.
  $("#remove-resume").hidden = files.length < 2;
}
function restorePreviousAttachment() {
  if (!setAttachmentInput(previousValidAttachments)) previousValidAttachments = [];
  updateResumeSelection();
}
function attachmentPrompt(files) {
  if (files.length > 1) return "請根據這些檔案整理技能與經驗的重點，並協助我處理求職相關問題；缺少的條件請先和我確認。";
  const kind = attachmentKind(files[0]);
  if (kind === "PDF") return "請根據這份履歷整理技能與經驗，並搜尋適合我的職缺；缺少的條件請先和我確認。";
  if (kind === "圖片") return "請根據這張圖片整理重點，並協助我處理求職相關問題；缺少的條件請先和我確認。";
  return "請根據這份文件整理重點，並協助我處理求職相關問題；缺少的條件請先和我確認。";
}
function acceptAttachmentSelection(files) {
  const merged = mergeAttachments(previousValidAttachments, [...(files || [])]);
  const error = activeChat ? "回覆進行中，暫時無法更換附件；原附件與訊息草稿已保留。" : attachmentError(merged);
  if (error) {
    restorePreviousAttachment(); setChatStatus(error, "error"); updateChatCount(); return false;
  }
  if (!setAttachmentInput(merged)) {
    restorePreviousAttachment(); setChatStatus("無法加入這些檔案；原附件與訊息草稿已保留。", "error"); updateChatCount(); return false;
  }
  previousValidAttachments = merged;
  const text = $("#chat-form [name=text]");
  if (!text.value.trim()) text.value = attachmentPrompt(merged);
  updateResumeSelection(); updateChatCount();
  setChatStatus(merged.length > 1 ? `已附加 ${merged.length} 個檔案；可以修改訊息後再按送出。` : "已附加檔案；可以修改訊息後再按送出。");
  return true;
}
function transferHasFiles(event) {
  const transfer = event?.dataTransfer;
  if (!transfer) return false;
  return Boolean(transfer.files?.length || Array.from(transfer.types || []).includes("Files"));
}
function clearChatFileDrag() {
  const panel = $("#chat"); const overlay = $("#chat-drop-overlay");
  if (panel) { panel.classList.remove("is-file-dragging"); panel.classList.remove("is-file-drop-blocked"); }
  if (overlay) overlay.hidden = true;
}
function showChatFileDrag() {
  const panel = $("#chat"); const overlay = $("#chat-drop-overlay");
  if (!panel || !overlay) return;
  panel.classList.add("is-file-dragging");
  panel.classList.toggle("is-file-drop-blocked", Boolean(activeChat));
  overlay.textContent = activeChat ? "回覆中，暫時無法更換附件" : "放開即可附加檔案，不會自動送出";
  overlay.hidden = false;
}
$("#resume-file").addEventListener("click", () => {
  const files = [...($("#resume-file").files || [])];
  if (files.length) previousValidAttachments = files;
});
$("#resume-file").addEventListener("change", () => {
  const files = [...($("#resume-file").files || [])];
  // A cancelled picker clears the native input; keep what was already attached.
  if (!files.length) { restorePreviousAttachment(); updateChatCount(); return; }
  acceptAttachmentSelection(files);
});
$("#remove-resume").addEventListener("click", () => {
  if (activeChat) return;
  previousValidAttachments = []; $("#resume-file").value = ""; updateResumeSelection(); updateChatCount();
});
$("#chat").addEventListener("dragenter", event => {
  if (!transferHasFiles(event)) return;
  event.preventDefault(); showChatFileDrag();
});
$("#chat").addEventListener("dragover", event => {
  if (!transferHasFiles(event)) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = activeChat ? "none" : "copy";
  showChatFileDrag();
});
$("#chat").addEventListener("dragleave", event => {
  if (!transferHasFiles(event)) return;
  if (event.relatedTarget && $("#chat").contains(event.relatedTarget)) return;
  clearChatFileDrag();
});
$("#chat").addEventListener("dragend", clearChatFileDrag);
$("#chat").addEventListener("drop", event => {
  if (!transferHasFiles(event)) return;
  event.preventDefault();
  const files = event.dataTransfer?.files;
  if (activeChat) setChatStatus("回覆進行中，暫時無法更換附件；原附件與訊息草稿已保留。", "error");
  else {
    previousValidAttachments = [...($("#resume-file").files || [])];
    acceptAttachmentSelection([...(files || [])]);
  }
  clearChatFileDrag();
});
window.addEventListener("dragover", event => { if (transferHasFiles(event)) event.preventDefault(); });
window.addEventListener("drop", event => { if (transferHasFiles(event)) { event.preventDefault(); clearChatFileDrag(); } });
window.addEventListener("blur", clearChatFileDrag);
$("#chat-form [name=text]").addEventListener("input", updateChatCount);
$("#chat-form [name=text]").addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  const submit = $("#chat-form button[type=submit]");
  if (event.repeat || submit.disabled || activeChat || switchingConversation) return;
  return $("#chat-form").requestSubmit(submit);
});
function chatResponseError(response, result) {
  const error = new Error(result?.error?.message || "訊息未能送出，請稍後再試。");
  error.status = response.status;
  error.code = result?.error?.code || "";
  error.definite = (![408, 429].includes(response.status) && response.status < 500) || response.status === 501
    || ["not_configured", "test_required", "invalid_tool", "refusal"].includes(error.code);
  return error;
}
async function postChatWithRetry(operation) {
  for (let retry = 0; retry <= CHAT_RETRY_LIMIT; retry++) {
    let response;
    try {
      operation.requestSent = true;
      const headers = {"X-CSRF-Token": operation.csrfToken};
      let body;
      if (operation.attachments.length) {
        body = new FormData(); body.append("text", operation.text); body.append("request_id", operation.id); body.append("conversation_id", operation.conversationId);
        for (const file of operation.attachments) body.append("resume", file);
        if (operation.action) body.append("action", JSON.stringify(operation.action));
        if (operation.editMessage) { body.append("edit_message_id", operation.editMessage.messageId); body.append("edit_request_id", operation.editMessage.requestId); }
      } else {
        headers["Content-Type"] = "application/json";
        const payload = {text: operation.text, request_id: operation.id, conversation_id: operation.conversationId};
        if (operation.action) payload.action = operation.action;
        if (operation.editMessage) { payload.edit_message_id = operation.editMessage.messageId; payload.edit_request_id = operation.editMessage.requestId; }
        body = JSON.stringify(payload);
      }
      response = await fetch("/api/chat", {method: "POST", credentials: "same-origin", signal: operation.controller.signal, headers, body});
    } catch (error) {
      if (error.name === "AbortError" || operation.controller.signal.aborted) throw chatAbortError();
      if (retry === CHAT_RETRY_LIMIT) return {recover: true};
      await waitForChatRetry(operation, retry + 1);
      continue;
    }
    if (!response.ok) {
      const result = await response.json().catch(() => null);
      if (response.status === 401) {
        window.dispatchEvent(new Event("session-expired"));
        throw chatResponseError(response, result);
      }
      if (response.status === 409 && result?.error?.code === "duplicate_request") {
        operation.accepted = true;
        return {recover: true};
      }
      const error = chatResponseError(response, result);
      if (error.definite) throw error;
      if (retry === CHAT_RETRY_LIMIT) return {recover: true};
      await waitForChatRetry(operation, retry + 1, `伺服器暫時無法處理，${CHAT_RETRY_DELAY_MS / 1000} 秒後以同一識別碼重試（${retry + 1}/${CHAT_RETRY_LIMIT}）。`);
      continue;
    }
    operation.accepted = true;
    if (response.headers?.get("Content-Type")?.startsWith("text/event-stream") && response.body) return {response};
    return {recover: true};
  }
  return {recover: true};
}
function addOptimisticChatMessages(operation) {
  clearAcceptedComposer(operation);
  if (operation.messagesAdded) return;
  const attachments = operation.attachments.map(file => ({filename: file.name}));
  operation.userElement = chatMessage("user", operation.text, "completed", [], attachments, null, operation.id, operation.editMessage?.messageId);
  operation.replyElement = chatMessage("assistant", "", "running", [], [], null, operation.id);
  operation.replyText = "";
  replaceChatPair(operation, {id: operation.editMessage?.messageId}, null, operation.userElement, operation.replyElement);
  operation.messagesAdded = true;
  scrollChatToBottom();
}
$("#chat-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (activeChat) return;
  if (switchingConversation) { setChatStatus("正在載入另一段對話，請稍候再送出。", "error"); return; }
  if (!selectedConversationId) { setChatStatus("目前沒有選取的對話，請先新增一段對話。", "error"); return; }
  const input = $("#chat-form [name=text]"); const text = input.value.trim();
  const attachments = [...($("#resume-file").files || [])];
  if (!text) { setChatStatus("請先輸入想說的話。", "error"); return; }
  if (editOrigin && editOrigin.conversationId !== String(selectedConversationId)) {
    setChatStatus("這則訊息屬於另一段對話；請取消編輯，或切回原對話後再送出。", "error"); return;
  }
  const editMessage = editOrigin;
  let operation;
    try { operation = {id: crypto.randomUUID(), conversationId: String(selectedConversationId), csrfToken, text, attachments, editMessage, action: pendingChatAction, controller: new AbortController(), accepted: false}; pendingChatAction = null; }
  catch (_) { setChatStatus("訊息尚未送出：無法準備訊息，請使用本機或 HTTPS 網址開啟網站。你的文字已保留。", "error"); return; }
  activeChat = operation;
  messageLoadRequest++;
  updateConversationControls();
  const submit = $("#chat-form button[type=submit]"); submit.disabled = true; submit.textContent = "送出中…";
  input.readOnly = true;
  $("#resume-file").disabled = true; $("#remove-resume").disabled = true;
  $("#cancel-chat").hidden = false; $("#refresh-messages").disabled = true; $("#more-messages").disabled = true;
  setChatStatus(attachments.length ? "正在上傳並解析附件…" : "正在送出訊息…");
  let completed = false;
  let keepActive = false;
  try {
    const delivery = await postChatWithRetry(operation);
    if (!chatOperationIsCurrent(operation)) {
      if (delivery.response?.body) await delivery.response.body.cancel();
      return;
    }
    if (delivery.recover) {
      const recovery = await recoverChatSnapshot(operation);
      if (recovery.status === "running") {
        keepActive = true;
        operation.awaitingSnapshot = true;
        setChatStatus(recovery.sawSnapshot
          ? "回覆仍在進行，已恢復目前內容。可按「重新整理歷史」再確認，或按「停止回覆」。"
          : "目前無法確認回覆狀態。可按「重新整理歷史」再確認，或按「停止回覆」。", "pending");
      } else completed = true;
    } else {
      operation.accepted = true;
      addOptimisticChatMessages(operation);
      input.readOnly = false; submit.textContent = "回覆中…";
      setChatStatus("訊息已送出，小幫手正在準備回覆…");
      try {
        await ChatSSE.read(delivery.response.body.getReader(), async (name, data) => {
          if (activeChat !== operation) return;
          if (completed) throw new Error("回覆結束後收到多餘資料。");
          const follow = chatAtBottom();
          if (name === "text_delta") {
            if (typeof data?.text !== "string") throw new Error("回覆格式不符。");
            operation.replyText += data.text;
            renderChatText($(".chat-text", operation.replyElement), operation.replyText);
          } else if (name === "text_reset") {
            if (typeof data?.text !== "string") throw new Error("回覆格式不符。");
            operation.replyText = data.text;
            renderChatText($(".chat-text", operation.replyElement), operation.replyText);
          } else if (name === "retry") {
            const attempt = Number(data?.attempt); const max = Number(data?.max_attempts);
            const detail = typeof data?.message === "string" && data.message.trim() ? data.message.trim() : "回覆暫時中斷，正在重試。";
            setChatStatus(`${detail}${Number.isFinite(attempt) && Number.isFinite(max) ? `（${attempt}/${max}）` : ""}`);
          } else if (name === "tool_status") {
            const titles = {reply: "正在回覆", list_preferences: "正在查看條件", propose_preference_change: "正在整理條件提案", search_jobs: "正在搜尋職缺", get_job_details: "正在讀取職缺詳細內容", get_company_reviews: "正在查詢公司評價", read_web_page: "正在讀取公開網頁"};
            setChatStatus(data?.summary || titles[data?.name] || "正在處理請求…");
            updateToolThinking(data);
            renderToolProgress(operation.replyElement, data);
          } else if (["preference_proposal", "jobs", "review", "job_detail", "recommendations", "error"].includes(name)) {
            renderChatEvent(operation.replyElement, name, data);
          } else if (name === "done") {
            if (data?.request_id !== operation.id || !["completed", "interrupted"].includes(data.status)) throw new Error("回覆完成狀態不符。");
            completed = true;
            operation.replyFinished = true;
            renderChatRecommendations(operation.replyElement, operation.replyText);
            finishToolProgress(operation.replyElement, data.status);
            if (operation.replyElement.chatStatusElement) {
              if (data.status === "completed") operation.replyElement.chatStatusElement.remove();
              else operation.replyElement.chatStatusElement.textContent = "回覆未完成，已取得的內容已保留。";
            }
            setChatStatus(data.status === "completed" ? "回覆已完成。" : "這則回覆未完成；可按原訊息旁的鉛筆編輯後送出。", data.status === "completed" ? "complete" : "error");
            if (data.status === "completed" && operation.editMessage) await restoreCompletedEditSnapshot(operation);
          }
          if (follow) operation.replyFinished ? scrollChatToReplyStart(operation.replyElement) : scrollChatToBottom();
          else updateLatestMessage();
        });
      } catch (error) {
        if (error.name === "AbortError" || operation.controller.signal.aborted) throw chatAbortError();
      }
      if (!completed) {
        setChatStatus("串流中斷，正在恢復伺服器已保存的回覆…");
        const recovery = await recoverChatSnapshot(operation);
        if (recovery.status === "running") {
          keepActive = true;
          operation.awaitingSnapshot = true;
          setChatStatus(recovery.sawSnapshot
            ? "回覆仍在進行，已恢復目前內容。可按「重新整理歷史」再確認，或按「停止回覆」。"
            : "目前無法確認回覆狀態。可按「重新整理歷史」再確認，或按「停止回覆」。", "pending");
        } else completed = true;
      }
    }
  } catch (error) {
    if (error.name === "AbortError" && operation.cancelled) {
      if (operation.accepted && csrfToken) {
        try {
          if (operation.cancelPromise) await operation.cancelPromise.catch(() => {});
          const snapshot = await api(`/api/chat/${encodeURIComponent(operation.id)}`);
          const status = renderChatSnapshot(operation, snapshot);
          if (status === "running") {
            operation.controller = new AbortController();
            keepActive = true; operation.awaitingSnapshot = true;
            setChatStatus("停止要求已送出，伺服器仍在結束回覆；可重新整理歷史或再停止一次。");
          } else if (status) completed = true;
        } catch (_) {
          operation.controller = new AbortController();
          keepActive = true; operation.awaitingSnapshot = true;
          setChatStatus("停止要求已送出，但目前無法確認保存狀態；可重新整理歷史或再停止一次。", "error");
        }
      }
      if (csrfToken && !operation.accepted) setChatStatus("已停止等待；請重新整理歷史確認訊息是否已送達。", "complete");
    } else if (csrfToken && operation.csrfToken === csrfToken) {
      const statusText = operation.accepted
        ? `${error.message} 請重新整理歷史確認已保存內容。`
        : error.definite
          ? `訊息尚未送出：${error.message} 你的文字與附件已保留。`
          : `目前無法確認訊息是否送達：${error.message} 你的文字與附件已保留；請先重新整理歷史，避免重複送出。`;
      setChatStatus(statusText, "error");
    }
  } finally {
    if (csrfToken !== operation.csrfToken || activeChat !== operation) return;
    if (keepActive) {
      operation.awaitingSnapshot = true;
      input.readOnly = false;
      $("#resume-file").disabled = false; $("#remove-resume").disabled = false;
      submit.disabled = true; submit.textContent = "回覆處理中…";
      $("#cancel-chat").hidden = false;
      updateConversationControls();
      return;
    }
    clearCompanionTransient();
    if (activeChat === operation) activeChat = null;
    submit.disabled = false; submit.textContent = "送出"; input.readOnly = false;
    $("#resume-file").disabled = false; $("#remove-resume").disabled = false;
    $("#cancel-chat").hidden = true; $("#refresh-messages").disabled = false; $("#more-messages").disabled = false;
    updateConversationControls();
    if (csrfToken && completed && !operation.snapshotRestored && !operation.editMessage) await loadMessages().catch(error => message(error.message, true));
    // An edit drops the replaced reply's events, so the working list is recomputed from the server.
    else if (csrfToken && completed && operation.editMessage) await refreshConversationWork().catch(error => message(error.message, true));
  }
});
$("#cancel-message-edit").addEventListener("click", () => cancelMessageEdit());
action($("#cancel-chat"), async () => {
  const operation = activeChat;
  if (!operation) return;
  clearCompanionTransient(true);
  const wasAwaitingSnapshot = Boolean(operation.awaitingSnapshot);
  operation.cancelled = true;
  if (!wasAwaitingSnapshot) operation.controller.abort();
  if (operation.requestSent) {
    operation.cancelPromise = api(`/api/chat/${encodeURIComponent(operation.id)}/cancel`, {method: "POST"});
    try { await operation.cancelPromise; operation.cancelAccepted = true; }
    catch (error) { if (error.status !== 404 && csrfToken) setChatStatus(error.message, "error"); }
  }
  if (operation.awaitingSnapshot && activeChat === operation) {
    try {
      const snapshot = await api(`/api/chat/${encodeURIComponent(operation.id)}`);
      if (!chatOperationIsCurrent(operation)) return;
      const status = renderChatSnapshot(operation, snapshot);
      if (status !== "running") {
        unlockChatAfterRecovery(operation);
        setChatStatus(operation.cancelAccepted ? "已停止回覆，已更新保存的訊息。" : "回覆已結束，已更新保存的訊息。", "complete");
      } else {
        setChatStatus(operation.cancelAccepted
          ? "已送出停止要求，伺服器仍在結束回覆；可稍後重新整理或再停止一次。"
          : "目前仍無法停止回覆；可稍後重新整理或再停止一次。", "pending");
      }
    } catch (error) {
      if (csrfToken) setChatStatus("停止狀態尚未確認：" + error.message + "；可重新整理歷史或再試一次停止。", "error");
    }
    return;
  }
});
action($("#enable-push"), enablePush);
action($("#disable-push"), disablePush);
$("#refresh-messages").addEventListener("click", async () => {
  try {
    if (activeChat?.awaitingSnapshot) {
      const operation = activeChat;
      const snapshot = await getChatSnapshot(operation);
      if (!chatOperationIsCurrent(operation)) return;
      const status = renderChatSnapshot(operation, snapshot);
      if (status !== "running") unlockChatAfterRecovery(operation);
      else setChatStatus("回覆仍在進行，可稍後再重新整理或停止回覆。");
    } else await loadMessages();
  } catch (error) { if (csrfToken) setChatStatus(error.status === 404 ? "目前尚未取得已保存的回覆狀態；可稍後再確認或停止回覆。" : error.message, "error"); }
});
$("#more-messages").addEventListener("click", async () => {
  try { await loadMessages(true); } catch (error) { if (csrfToken) setChatStatus(error.message, "error"); }
});
$("#open-conversation-history").addEventListener("click", async () => {
  const dialog = $("#conversation-history-dialog");
  if (!csrfToken) return;
  if (!dialog.open) dialog.showModal();
  $("#conversation-query").focus();
  await loadConversations(false).catch(() => {});
});
$("#close-conversation-history").addEventListener("click", () => $("#conversation-history-dialog").close());
$("#new-conversation").addEventListener("click", () => {
  $("#conversation-history-dialog").close();
  void createConversation();
});
$("#conversation-search-form").addEventListener("submit", async event => {
  event.preventDefault();
  await loadConversations(false).catch(() => {});
});
$("#more-conversations").addEventListener("click", async () => {
  await loadConversations(true).catch(() => {});
});
$("#cancel-rename-conversation").addEventListener("click", () => $("#rename-conversation-dialog").close());
$("#rename-conversation-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("#rename-conversation-input");
  const title = input.value.trim();
  const status = $("#rename-conversation-status");
  const titleLength = Array.from(title).length;
  if (!title || titleLength > 80) {
    status.textContent = "名稱需為 1～80 個字。"; status.classList.toggle("error", true); input.focus(); return;
  }
  const id = $("#rename-conversation-dialog").dataset.conversationId;
  const requestToken = csrfToken;
  const button = $("#save-conversation-name"); button.disabled = true;
  status.textContent = "正在保存名稱…"; status.classList.toggle("error", false);
  try {
    const saved = await api(`/api/conversations/${encodeURIComponent(id)}`, {method: "PATCH", body: JSON.stringify({title})});
    if (!csrfToken || requestToken !== csrfToken) return;
    if (String(id) === selectedConversationId) $("#active-conversation-title").textContent = saved.title || title;
    $("#rename-conversation-dialog").close();
    await loadConversations(false).catch(error => setConversationHistoryStatus(error.message, true));
    $("#conversation-query").focus();
  } catch (error) {
    if (csrfToken && requestToken === csrfToken) {
      status.textContent = error.message; status.classList.toggle("error", true); status.focus();
    }
  } finally { button.disabled = false; }
});
$("#cancel-delete-conversation").addEventListener("click", () => $("#delete-conversation-dialog").close());
$("#confirm-delete-conversation").addEventListener("click", async () => {
  const dialog = $("#delete-conversation-dialog");
  const id = dialog.dataset.conversationId;
  const status = $("#delete-conversation-status");
  if (!id) return;
  if (activeChat && String(id) === selectedConversationId) {
    status.textContent = "這段對話正在回覆，請等候完成後再刪除。"; status.classList.toggle("error", true); status.focus(); return;
  }
  if (switchingConversation) {
    status.textContent = "正在載入另一段對話，請稍候再刪除。"; status.classList.toggle("error", true); status.focus(); return;
  }
  const requestToken = csrfToken;
  const button = $("#confirm-delete-conversation"); button.disabled = true;
  status.textContent = "正在刪除對話…"; status.classList.toggle("error", false);
  try {
    await api(`/api/conversations/${encodeURIComponent(id)}`, {method: "DELETE"});
  } catch (error) {
    if (csrfToken && requestToken === csrfToken) {
      status.textContent = error.message; status.classList.toggle("error", true); status.focus();
    }
    button.disabled = false; return;
  }
  if (!csrfToken || requestToken !== csrfToken) { button.disabled = false; return; }
  dialog.close();
  const wasSelected = String(id) === selectedConversationId;
  if (wasSelected) showNoConversations();
  let result;
  try { result = await loadConversations(false); }
  catch (error) {
    if (wasSelected) setChatStatus(`對話已刪除，但列表暫時無法更新：${error.message}`, "error");
    $("#conversation-query").focus(); button.disabled = false; return;
  }
  if (!csrfToken || requestToken !== csrfToken) { button.disabled = false; return; }
  if (wasSelected) {
    let next = result?.items[0];
    if (!next && $("#conversation-query").value.trim()) {
      $("#conversation-query").value = "";
      try { result = await loadConversations(false, ""); next = result?.items[0]; }
      catch (error) { setChatStatus(`對話已刪除，但列表暫時無法更新：${error.message}`, "error"); }
    }
    if (!csrfToken || requestToken !== csrfToken) { button.disabled = false; return; }
    if (next) await openConversation(next, false);
    else showNoConversations();
  }
  $("#conversation-query").focus(); button.disabled = false;
});
formAction($("#criteria-form"), async () => {
  try {
    const after = readCriteria($("#criteria-fields"));
    const proposal = editing ? {operation: "update", preference_id: editing.id, base_revision: editing.revision, after} : {operation: "create", after};
    const result = await api("/api/preference-proposals", {method: "POST", body: JSON.stringify(proposal)});
    clearEditor(); await loadPreferences(result.id);
  } catch (error) { if (csrfToken) preferencesFeedback(error.message, true); }
});
$("#cancel-edit").addEventListener("click", clearEditor);
action($("#more-favorites"), () => loadFavorites(true));
action($("#more-daily"), () => loadDaily(true));
action($("#refresh-daily"), () => loadDaily());
$("#daily-date").addEventListener("change", () => loadDaily().catch(error => message(error.message, true)));
action($("#logout"), async () => { await api("/api/logout", {method: "POST"}); signedOut(); message("已登出。"); });
document.querySelectorAll("[data-page]").forEach(button => {
  action(button, async () => {
    if (button.dataset.page === "favorites") await loadFavorites();
    else if (button.dataset.page === "chat") await loadMessages();
    else if (button.dataset.page === "daily") await loadDaily();
    else await loadPreferences();
    showPage(button.dataset.page);
  });
});
window.addEventListener("pagehide", signedOut);
window.addEventListener("pageshow", event => { if (event.persisted) window.location.reload(); });
window.addEventListener("session-expired", signedOut);
(async () => {
  try {
    const bootstrap = await api("/api/bootstrap"); csrfToken = bootstrap.csrf_token;
    setupCompanion(bootstrap);
    maybeShowEncouragement(bootstrap);
    updatePushBootstrap(bootstrap);
    maybePlayMail(bootstrap.recent_run);
    void rebindExistingPush();
    scheduleCompanionPoll();
    for (let offset = 0; offset < 7; offset++) {
      const date = new Date(bootstrap.local_date + "T12:00:00+08:00"); date.setUTCDate(date.getUTCDate() - offset);
      const value = date.toISOString().slice(0, 10);
      const option = node("option", value); option.value = value; $("#daily-date").append(option);
    }
    try { regionCatalog = (await api("/api/areas")).items; } catch (error) { message(error.message, true); }
    clearEditor(); await initializeConversations();
    if (!csrfToken) return;
    await loadPreferences();
    // Several people share one deployment, so show whose account this device is signed in to.
    const account = typeof bootstrap.account?.username === "string" ? bootstrap.account.username : "";
    $("#logout").textContent = account ? `登出（${account}）` : "登出";
    $("#access-needed").hidden = true; $("#user-panel").hidden = false; $("#logout").hidden = false;
    updateConversationControls();
    scrollChatToBottom();
    if (new URLSearchParams(location.search).get("page") === "daily") { await loadDaily(); showPage("daily"); }
  } catch (error) { if (error.status !== 401) message(error.message, true); }
})();
if (window.isSecureContext && navigator.serviceWorker && typeof navigator.serviceWorker.register === "function") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
