"use strict";
let runPoll = null;
const MAX_BACKUP_BYTES = 128 * 1024 * 1024;
const SPRITE_STATE_LABELS = {idle: "待機", thinking: "思考", happy: "開心", mail: "拿信", encourage: "鼓勵"};
let spriteTemplatePreview = null;
let spriteTemplatePreviewVersion = 0;
let spriteListVersion = 0;
const spriteListAnimations = [];
const spriteTemplateAnimations = [];
const PUSH_RESULT_LABELS = {
  accepted: "伺服器接受送出（不代表裝置已顯示）",
  expired: "訂閱已過期，伺服器已移除",
  failed: "送出失敗",
  unknown: "結果不明，未自動重送",
  blocked: "已封鎖／未送出",
};
const SHARING_STATES = new Set(["unsupported", "cli_missing", "installing", "daemon_unavailable", "needs_login", "stopped", "disabled", "consent_required", "conflict", "configured", "ready", "error"]);
let sharingSnapshot = null;
let sharingBusy = false;
let linkButtons = [];
let sharingSequence = 0;
let sharingPoll = null;
let portsSnapshot = null;

async function multipartApi(path, body, method = "POST") {
  const requestToken = csrfToken;
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {"X-CSRF-Token": requestToken},
    body,
  });
  let result = null;
  if (response.status !== 204) {
    try { result = await response.json(); } catch (_) { result = null; }
  }
  if (requestToken !== csrfToken) throw new Error("登入狀態已變更，請重新整理。");
  if (!response.ok) {
    if (response.status === 401 && csrfToken) window.dispatchEvent(new Event("session-expired"));
    const exception = new Error(result?.error?.message || "操作失敗，請稍後再試。");
    exception.status = response.status;
    throw exception;
  }
  return result;
}

function searchTestStatus(test) {
  if (!test) return "尚未測試目前設定，請按「測試搜尋連線」。";
  const result = test.passed ? `連線測試通過 · ${test.result_count} 筆可用結果 · ${test.duration_ms} ms`
    : `連線測試未通過 · ${test.message}`;
  return `${result}。最近測試：${formatTaipeiDateTime(test.tested_at)}（台灣時間）。`;
}
async function testSearchConnection() {
  $("#search-status").textContent = "正在測試搜尋連線…";
  try {
    const result = await api("/api/admin/search-test", {method: "POST"});
    $("#search-status").textContent = searchTestStatus(result);
  } catch (error) {
    $("#search-status").textContent = `連線測試未通過：${error.message}`;
    throw error;
  }
}

function validVapidSubject(value) {
  if (!value) return true;
  if (/\s|[\u0000-\u001f\u007f]/.test(value)) return false;
  if (/^mailto:/i.test(value)) {
    const address = value.slice(7);
    const at = address.indexOf("@");
    return at > 0 && at === address.lastIndexOf("@") && at < address.length - 1 && !address.includes("?") && !address.includes("#");
  }
  try {
    const parsed = new URL(value);
    const port = parsed.port ? Number(parsed.port) : null;
    const validPort = port === null || (Number.isInteger(port) && port >= 1 && port <= 65535);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password && !parsed.hash && validPort;
  } catch (_) { return false; }
}

function formatBytes(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return "大小未知";
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KiB`;
  return `${(size / 1024 ** 2).toFixed(1)} MiB`;
}

function safeSharingOrigin(value, publicPort = 8443) {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || Number(url.port || 443) !== publicPort || !url.hostname.endsWith(".ts.net") ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) return null;
    return url.origin;
  } catch (_) { return null; }
}

function safeSharingActionUrl(value, state) {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value);
    const loginFlow = ["needs_login", "consent_required"].includes(state);
    const officialDownload = ["cli_missing", "unsupported"].includes(state);
    const allowed = loginFlow ? url.hostname === "login.tailscale.com"
      : officialDownload && url.hostname === "tailscale.com" && url.pathname === "/download/windows";
    return url.protocol === "https:" && !url.port && allowed && !url.username && !url.password && !url.hash ? url.href : null;
  } catch (_) { return null; }
}

function checkedSharingStatus(result) {
  if (!result || !SHARING_STATES.has(result.state) || typeof result.can_disable !== "boolean" ||
    !Number.isInteger(result.local_port) || result.local_port < 1 || result.local_port > 65535 ||
    ![443, 8443, 10000].includes(result.public_port) || typeof result.restart_required !== "boolean" ||
    (result.origin !== null && !safeSharingOrigin(result.origin, result.public_port))) {
    throw new Error("Tailscale 狀態回應格式無效，請重新檢查。");
  }
  const actionUrl = result.action_url == null ? null : safeSharingActionUrl(result.action_url, result.state);
  if (result.action_url != null && !actionUrl) throw new Error("Tailscale 官方連結無效，請重新檢查。");
  return {...result, origin: result.origin ? safeSharingOrigin(result.origin, result.public_port) : null, action_url: actionUrl,
    message: typeof result.message === "string" ? result.message.slice(0, 500) : ""};
}

function sharingGuidance(status) {
  if (status.restart_required) return "請先重新執行 start.cmd，讓已保存的連接埠生效，再啟用 Funnel。";
  switch (status.state) {
    case "unsupported": return "目前系統或 Tailscale 版本不支援 Funnel。";
    case "cli_missing": return "按「安裝 Tailscale」會啟動官方安裝程序；安裝期間會每 3 秒檢查一次。";
    case "installing": return "Tailscale 正在安裝；完成後此區會自動重新檢查，不會顯示估算進度。";
    case "daemon_unavailable": return "請先啟動 Tailscale 用戶端，再重新檢查。";
    case "needs_login": return status.action_url
      ? "請在 Tailscale 官方頁面完成登入；完成後按「重新檢查狀態」。"
      : "按「登入 Tailscale」取得官方登入流程；登入完成後回到此頁重新檢查。";
    case "stopped": return "Tailscale 目前已停止；請啟動並登入 Tailscale，再重新開啟 Funnel。";
    case "disabled": return `Funnel 尚未啟用；啟用時會設定公開 ${status.public_port} 轉送至本機前台 ${status.local_port}。`;
    case "consent_required": return status.action_url
      ? "請在 Tailscale 官方頁面允許 Funnel；完成後回到此頁按「已完成，重新啟用」。"
      : "尚未完成 Tailscale 官方 Funnel 授權；完成授權後重新啟用。";
    case "conflict": return `公開埠 ${status.public_port} 已有其他服務設定；請在上方連接埠卡片選擇其他公開埠。`;
    case "configured": return "Funnel 已指向本機前台，但 HTTPS 檢查尚未通過；確認網址可用前，請勿交付外部使用者。";
    case "ready": return "電腦和本程式須保持運行；可用手機行動網路確認外部連線。";
    default: return "目前無法確認公開入口；重新檢查後再交付外部使用者。";
  }
}

function primarySharingAction() {
  if (!sharingSnapshot) return "check";
  if (sharingSnapshot.state === "cli_missing") return "install";
  if (["stopped", "needs_login"].includes(sharingSnapshot.state)) return "login";
  if (sharingSnapshot.state === "installing") return "install";
  if (["disabled", "consent_required"].includes(sharingSnapshot.state)) return "enable";
  return "check";
}

function updateSharingButtons() {
  const status = sharingSnapshot;
  const primary = $("#sharing-primary");
  const action = primarySharingAction();
  const label = action === "install" ? "安裝 Tailscale" : action === "login" ? status?.state === "stopped" ? "啟動 Tailscale" : "登入 Tailscale"
    : action === "enable" ? status?.state === "consent_required" ? "已完成，重新啟用" : "啟用 Funnel"
      : status?.state === "configured" ? "再次測試" : "重新檢查";
  primary.textContent = status?.state === "installing" ? "正在安裝…" : label;
  primary.disabled = sharingBusy || status?.state === "installing" || status?.restart_required === true;
  const refresh = $("#sharing-refresh");
  refresh.hidden = action === "check";
  refresh.disabled = sharingBusy;
  const disable = $("#sharing-disable");
  disable.hidden = status?.can_disable !== true;
  disable.disabled = sharingBusy;
  linkButtons.forEach(button => { button.disabled = sharingBusy; });
  $("#ports-save").disabled = sharingBusy || portsSnapshot?.editable !== true;
}

function stopSharingPoll() {
  clearTimeout(sharingPoll);
  sharingPoll = null;
}

function scheduleSharingPoll() {
  stopSharingPoll();
  if (sharingSnapshot?.state !== "installing" || !csrfToken) return;
  sharingPoll = setTimeout(() => {
    sharingPoll = null;
    refreshSharingStatus();
  }, 3000);
}

function renderSharingStatus(value) {
  const status = checkedSharingStatus(value);
  sharingSnapshot = status;
  const statusLine = $("#sharing-status");
  statusLine.className = status.state === "error" || status.state === "conflict" ? "notice error" : "notice";
  statusLine.textContent = status.message || `Tailscale 狀態：${status.state}`;
  $("#sharing-guidance").textContent = sharingGuidance(status);
  $("#sharing-ports").textContent = `公開埠 ${status.public_port} · 本機前台 proxy ${status.local_port}`;
  const originRow = $("#sharing-origin-row");
  const originLink = $("#sharing-origin");
  const showOrigin = Boolean(status.origin && ["configured", "ready"].includes(status.state));
  originRow.hidden = !showOrigin;
  if (showOrigin) { originLink.href = status.origin; originLink.textContent = status.origin; }
  else { originLink.removeAttribute("href"); originLink.textContent = ""; }
  const actionLink = $("#sharing-action-link");
  const actionLabel = status.state === "consent_required" ? "開啟 Tailscale 官方授權頁面"
    : status.state === "needs_login" ? "開啟 Tailscale 官方登入頁面"
      : "前往 Tailscale 官方下載頁面";
  actionLink.hidden = !status.action_url;
  if (status.action_url) { actionLink.href = status.action_url; actionLink.textContent = actionLabel; }
  else { actionLink.removeAttribute("href"); actionLink.textContent = ""; }
  $("#access-guidance").textContent = status.state === "ready"
    ? "接著建立專屬連結並交給使用者；對方不需安裝 Tailscale。"
    : status.state === "configured"
      ? "Funnel 的 HTTPS 檢查尚未通過；先完成外部連線，再交付連結給外部使用者。"
      : "先完成外部連線，再交付連結給外部使用者。";
  updateSharingButtons();
  scheduleSharingPoll();
}

function showSharingError(error) {
  const statusLine = $("#sharing-status");
  statusLine.className = "notice error";
  statusLine.textContent = `操作未完成：${error.message || "請重新檢查 Tailscale 狀態。"}`;
  $("#sharing-guidance").textContent = "請重新檢查狀態後再試。";
}

async function refreshSharingStatus() {
  if (!csrfToken) return;
  const token = csrfToken;
  const sequence = ++sharingSequence;
  stopSharingPoll();
  try {
    const result = checkedSharingStatus(await api("/api/admin/sharing"));
    if (token !== csrfToken || sequence !== sharingSequence) return;
    renderSharingStatus(result);
  } catch (error) {
    if (token !== csrfToken || sequence !== sharingSequence) return;
    showSharingError(error);
  }
}

async function performSharingAction(action) {
  if (!csrfToken || sharingBusy || !["install", "login", "enable", "disable", "check"].includes(action)) return;
  if (action === "disable" && sharingSnapshot?.can_disable !== true) return;
  const token = csrfToken;
  const sequence = ++sharingSequence;
  stopSharingPoll();
  sharingBusy = true;
  updateSharingButtons();
  const pending = {install: "正在啟動 Tailscale 官方安裝程序…", login: "正在檢查 Tailscale 登入狀態…",
    enable: "正在設定 Tailscale Funnel…", disable: "正在停用 Tailscale Funnel…", check: "正在重新檢查 Tailscale 狀態…"};
  $("#sharing-status").className = "notice";
  $("#sharing-status").textContent = pending[action];
  try {
    const result = checkedSharingStatus(await api("/api/admin/sharing", {method: "POST", body: JSON.stringify({action})}));
    if (token !== csrfToken || sequence !== sharingSequence) return;
    renderSharingStatus(result);
  } catch (error) {
    if (token !== csrfToken || sequence !== sharingSequence) return;
    showSharingError(error);
  } finally {
    if (token === csrfToken && sequence === sharingSequence) {
      sharingBusy = false;
      updateSharingButtons();
    }
  }
}

function invalidateSharing() {
  sharingSequence += 1;
  sharingBusy = false;
  sharingSnapshot = null;
  portsSnapshot = null;
  stopSharingPoll();
  $("#sharing-status").textContent = "登入後會檢查 Tailscale 狀態。";
  $("#sharing-status").className = "notice";
  $("#sharing-guidance").textContent = "登入後會顯示安裝、授權與公開入口狀態。";
  $("#sharing-origin-row").hidden = true;
  $("#sharing-origin").removeAttribute("href");
  $("#sharing-origin").textContent = "";
  $("#sharing-action-link").hidden = true;
  $("#sharing-action-link").removeAttribute("href");
  $("#sharing-action-link").textContent = "";
  updateSharingButtons();
}

function renderPorts(result) {
  const valid = value => value && ["admin_port", "local_port"].every(name =>
    Number.isInteger(value[name]) && value[name] >= 1 && value[name] <= 65535) &&
    value.admin_port !== value.local_port && [443, 8443, 10000].includes(value.public_port);
  if (!result || !valid(result.ports) || !valid(result.current) ||
    typeof result.editable !== "boolean" || typeof result.restart_required !== "boolean") {
    throw new Error("連接埠設定回應格式無效，請重新整理。");
  }
  portsSnapshot = result;
  const form = $("#ports-form");
  for (const name of ["admin_port", "local_port", "public_port"]) {
    form.elements[name].value = result.ports[name];
    form.elements[name].disabled = !result.editable;
  }
  const current = result.current;
  $("#ports-current").textContent = `目前使用：本機後台 ${current.admin_port} · 本機前台 ${current.local_port} · 公開 HTTPS ${current.public_port}`;
  const line = $("#ports-status");
  line.hidden = false; line.className = "notice";
  line.textContent = !result.editable ? "目前使用容器或其他部署方式，請在部署設定中修改連接埠。"
    : result.restart_required ? "已保存，尚未生效。請重新執行 start.cmd；更改前台或公開埠後，再到下方啟用 Funnel。"
      : "保存設定與目前使用的連接埠一致。";
  const next = $("#ports-next-admin");
  $("#ports-next-row").hidden = !result.editable || !result.restart_required;
  if (result.editable && result.restart_required) {
    next.href = `http://localhost:${result.ports.admin_port}/admin`; next.textContent = next.href;
  } else { next.removeAttribute("href"); next.textContent = ""; }
  updateSharingButtons();
}

async function loadPorts() {
  const token = csrfToken;
  try {
    const result = await api("/api/admin/ports");
    if (token === csrfToken) renderPorts(result);
  }
  catch (error) {
    if (token !== csrfToken) return;
    $("#ports-status").hidden = false; $("#ports-status").className = "notice error";
    $("#ports-status").textContent = error.message;
  }
}

async function savePorts() {
  if (!csrfToken || sharingBusy || portsSnapshot?.editable !== true) return;
  const form = $("#ports-form");
  if (!form.reportValidity()) return;
  const ports = Object.fromEntries(["admin_port", "local_port", "public_port"].map(name => [name, Number(form.elements[name].value)]));
  const token = csrfToken;
  sharingBusy = true; updateSharingButtons();
  try {
    const result = await api("/api/admin/ports", {method: "PUT", body: JSON.stringify(ports)});
    if (token !== csrfToken) return;
    renderPorts(result);
    hideAccess();
    await refreshSharingStatus();
  } catch (error) {
    if (token !== csrfToken) return;
    $("#ports-status").hidden = false; $("#ports-status").className = "notice error";
    $("#ports-status").textContent = error.message;
  } finally {
    if (token === csrfToken) { sharingBusy = false; updateSharingButtons(); }
  }
}

async function loadRuns() {
  const result = await api("/api/admin/search-runs");
  const list = $("#run-list"); list.replaceChildren();
  if (!result.items.length) list.append(node("p", "尚未執行每日搜尋。", "hint"));
  for (const run of result.items) {
    const card = node("article", undefined, "item");
    const labels = {running: "執行中", success: "完成", partial: "部分完成／範圍受限", failed: "未完成"};
    card.append(node("h3", `${run.local_date} · ${labels[run.status]}`), node("p", `${formatTaipeiDateTime(run.started_at)} · ${run.trigger} · 新增 ${run.new_count} 筆／初始 ${run.initial_count} 筆`));
    if (run.message) card.append(node("p", run.message));
    if (run.notifications && typeof run.notifications === "object") {
      const counts = ["accepted", "expired", "failed", "unknown", "blocked"]
        .map(name => `${name} ${Number.isFinite(run.notifications[name]) ? run.notifications[name] : 0}`).join(" · ");
      card.append(node("p", `推播結果：${counts}；accepted 僅代表伺服器接受，不代表裝置已顯示通知。`, "hint"));
    }
    for (const source of run.source_results) card.append(node("p", `${source.source} · ${source.completed ? "已保存" : "未保存"}${source.baseline ? " · 初始基準" : ""}${source.truncated ? " · 範圍受限" : ""}${source.message ? " · " + source.message : ""}`, "hint"));
    list.append(card);
  }
  clearTimeout(runPoll);
  if (result.items.some(item => item.status === "running") && csrfToken) runPoll = setTimeout(() => loadRuns().catch(error => message(error.message, true)), 1500);
}
async function loadSprites() {
  const token = csrfToken;
  const version = ++spriteListVersion;
  const result = await api("/api/admin/sprites");
  if (token !== csrfToken || version !== spriteListVersion) return;
  clearSpriteAnimations(spriteListAnimations);
  $("#sprite-list").replaceChildren();
  for (const sprite of result.items) {
    const row = node("article", undefined, "item");
    row.append(node("p", `${sprite.state} · ${sprite.frame_width}×${sprite.frame_height} · ${sprite.frame_count} 格 · ${sprite.fps} FPS`));
    const image = node("img"); image.src = `/api/admin/sprites/${sprite.state}/image?version=${Date.now()}`; image.alt = `${sprite.state} 精靈圖預覽`; image.className = "sprite-preview";
    row.append(image);
    try {
      await image.decode();
      if (token !== csrfToken || version !== spriteListVersion) return;
      row.append(spriteAnimationPreview(image, sprite, spriteListAnimations));
    } catch (_) {
      if (token !== csrfToken || version !== spriteListVersion) return;
      row.append(node("p", "動畫預覽載入失敗，請重新整理。", "hint"));
    }
    $("#sprite-list").append(row);
  }
}
function clearSpriteAnimations(animations) {
  animations.splice(0).forEach(cleanup => cleanup());
}
function spriteAnimationPreview(image, sprite, animations) {
  const box = node("div", undefined, "stack");
  const canvas = node("canvas", undefined, "sprite-preview");
  canvas.width = sprite.frame_width; canvas.height = sprite.frame_height;
  canvas.style.width = `${sprite.frame_width * Math.min(2, 128 / Math.max(sprite.frame_width, sprite.frame_height))}px`;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", `${SPRITE_STATE_LABELS[sprite.state]}動畫預覽`);
  const context = canvas.getContext("2d"); context.imageSmoothingEnabled = false;
  const media = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  const controller = window.CompanionSprite.create(canvas, [sprite], {
    paused: true, reducedMotion: media || false,
    renderFrame(value, index) {
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, index * value.frame_width, 0, value.frame_width, value.frame_height,
        0, 0, canvas.width, canvas.height);
    },
  });
  const button = node("button", "播放動畫"); button.type = "button";
  let playing = false;
  function motionChanged() {
    playing = false; controller.setPaused(true);
    button.disabled = Boolean(media?.matches) || sprite.frame_count <= 1;
    button.textContent = media?.matches ? "已減少動態效果" : sprite.frame_count <= 1 ? "單格素材" : "播放動畫";
    button.setAttribute("aria-pressed", "false");
  }
  button.addEventListener("click", () => {
    playing = !playing; controller.setPaused(!playing);
    button.textContent = playing ? "暫停動畫" : "播放動畫";
    button.setAttribute("aria-pressed", String(playing));
  });
  motionChanged();
  media?.addEventListener("change", motionChanged);
  animations.push(() => { controller.destroy(); media?.removeEventListener("change", motionChanged); image.close?.(); });
  box.append(canvas, button);
  if (sprite.state === "idle") box.append(node("p", "待機每輪回到第一格停留 2 秒；其餘影格依 FPS 播放。", "hint"));
  return box;
}
function renderPushTest(result) {
  if (!result || result.test !== true || !Array.isArray(result.items)) throw new Error("推播測試回應無效。");
  const list = $("#push-test-results");
  list.replaceChildren();
  $("#push-status").textContent = result.items.length
    ? "測試完成；伺服器接受不代表裝置已顯示通知。"
    : "目前沒有可測試的已授權裝置。";
  if (!result.items.length) {
    list.append(node("p", "目前沒有可測試的已授權裝置。", "hint"));
    return;
  }
  for (const item of result.items) {
    const row = node("article", undefined, "item");
    const resultKey = Object.prototype.hasOwnProperty.call(PUSH_RESULT_LABELS, item.result) ? item.result : "unknown";
    row.append(node("p", `${item.subscription_id || "未提供裝置識別"} · ${PUSH_RESULT_LABELS[resultKey]}`));
    list.append(row);
  }
}
async function loadBackups() {
  const result = await api("/api/admin/backups");
  const list = $("#backup-list");
  list.replaceChildren();
  const items = Array.isArray(result?.items) ? result.items : [];
  if (!items.length) {
    list.append(node("p", "尚未建立備份。", "hint"));
    return;
  }
  for (const backup of items) {
    const id = String(backup.id ?? "");
    const row = node("article", undefined, "item");
    row.append(node("h3", `備份 ${id || "未提供識別碼"}`),
      node("p", `${formatBytes(backup.size)} · ${formatTaipeiDateTime(backup.created_at)}`));
    const link = node("a", "下載備份", "button");
    link.href = `/api/admin/backups/${encodeURIComponent(id)}`;
    link.download = `backup-${id.replace(/[^A-Za-z0-9._-]/g, "_") || "archive"}.zip`;
    link.rel = "noopener noreferrer";
    row.append(link);
    list.append(row);
  }
}
function restoreConfirmation() {
  const dialog = $("#restore-confirm");
  dialog.returnValue = "cancel";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true});
    dialog.showModal();
  });
}
async function submitRestore() {
  const form = $("#restore-form");
  const restoreToken = csrfToken;
  if (!form.elements.confirm.checked) throw new Error("請先勾選還原確認。");
  const file = form.elements.file.files[0];
  if (!file) throw new Error("請選擇備份 ZIP。");
  if (file.size > MAX_BACKUP_BYTES) throw new Error("備份 ZIP 不可超過 128 MiB。");
  $("#restore-status").textContent = "請在下一個對話框再次確認還原。";
  if (!await restoreConfirmation()) {
    $("#restore-status").textContent = "已取消還原。";
    return;
  }
  if (csrfToken !== restoreToken || !form.elements.confirm.checked || form.elements.file.files[0] !== file) {
    throw new Error("登入狀態或還原檔案已變更，請重新選擇並重試。");
  }
  $("#restore-status").textContent = "正在驗證並還原，請稍候…";
  const result = await multipartApi("/api/admin/restore", new FormData(form));
  if (!result || result.restored !== true || result.login_required !== true || result.resubscribe_required !== true) {
    throw new Error("還原回應不完整。");
  }
  const backupId = String(result.pre_restore_backup_id ?? "未提供");
  signedOut();
  $("#restore-result").textContent = `還原成功。還原前備份 ID：${backupId}。請使用備份內的管理員密碼重新登入；通知需重新綁定。`;
  $("#restore-result").hidden = false;
  try {
    await loginContext();
    message("還原成功，請重新登入並重新綁定通知。");
  } catch (error) {
    message("還原成功，但登入狀態初始化失敗，請重新整理。", true);
    throw error;
  }
}

function spritePrompt(character, columns = 8) {
  return `請生成一張用於網頁陪伴角色的像素動畫精靈圖（sprite sheet）。
角色設定：${character.trim() || "請依附上的角色參考圖；若尚無參考圖，請設計一個溫柔可愛的小動物角色。"}
姿態優先：保持該物種自然的動物體態與肢體結構，以四足、坐姿或蹲伏為主；原本雙足的動物依其自然姿態。不要擬人化直立、人的手掌或人類手勢；參考圖或角色描述若有人的姿態或服裝，以自然動物體態優先，只保留可辨識的毛色、五官與簡單配件。
畫風：2D 全身像素小動物，以正面略偏側的固定視角呈現，清楚的方形像素、有限色盤、柔和配色、簡潔輪廓；不要抗鋸齒、模糊、漸層、寫實或 3D 效果。
版面：嚴格 ${columns} 欄×5 列，共 ${columns * 5} 格；每格 64×64 px，整張畫布實際尺寸 ${columns * 64}×320 px。格子等大、相鄰無空隙，沒有外邊框、格線或額外留白。
一致性：全部格子是同一個角色，毛色、五官、配件與身體比例一致，鏡頭、格位中心及地面基準線固定。每格角色的身體重心（軀幹）都畫在該格水平正中央、腳底落在同一條地面基準線上，同一列 ${columns} 格不得朝左或右逐格漂移，走路、探身等動作一律原地進行；允許呼吸、蹲伏和跳躍造成頭部、四肢與身體高度的小幅位移，但身體重心不離開格子中央。頭、耳、四肢與道具完整留在各自格內，四周留至少 4 px 透明空間。
動畫要求：每列是一個連續動作的 ${columns} 個時間點，不是獨立表情貼圖。每列必須畫滿 ${columns} 格；下方四段描述是動作階段，不是只畫四張圖，須在階段之間補足過渡影格，呈現起始、預備、加速、最大幅度、減速及恢復。相鄰影格的肢體位置漸進變化，最後一格自然接回第一格，不用重複貼同一張圖湊格數。以 64×64 成品計，呼吸或頭部位移約 1～2 px，跳躍或探身約 3～5 px，輪廓變化必須可辨識；耳朵、尾巴等依物種具備的部位自然跟隨，不添加不存在的肢體。待機列第 1 格適合停留 2 秒，維持睜眼放鬆姿態。
動作順序（由上到下，每列由左到右 ${columns} 格；按下列階段補足連續影格）：
第 1 列 idle／待機：自然坐著或蹲伏、睜眼放鬆 → 胸背隨吸氣微微抬起 → 呼氣回落並短暫眨眼 → 睜眼、身體回到接近第 1 格；不能只有眼睛變化。
第 2 列 thinking／思考：保持自然坐姿、專注看前方 → 頭向一側傾斜並稍微探出 → 鼻尖向前嗅探、耳朵依物種微動 → 頭收回接近起始姿態；四肢保持動物姿態。
第 3 列 happy／開心：四肢自然屈曲、壓低身體蓄力 → 以該物種自然跳躍姿態小幅離地 → 落地、四肢彎曲吸收衝擊 → 恢復接近起始姿態；身體有清楚的上下位移，不能變成人形舉手慶祝。
第 4 列 mail／拿信：自然站姿或坐姿，嘴輕叼同一封愛心信封 → 頭連同信封向前探出 → 再靠近一些、像把好消息送到眼前 → 頭與信封收回；全部影格都有同一封信，隨頭部移動，不能用人的手拿信。
第 5 列 encourage／鼓勵：自然坐姿、溫柔看前方 → 身體稍微前傾、頭向下輕點 → 頭抬起、恢復柔和眼神 → 身體收回接近起始姿態；以親近與點頭表達陪伴，不揮手或握拳。
邊緣：格子四周是完全透明的安全區，不畫地面線、投影橫線或分隔線；耳、尾、腳與信封不得跨格或碰到格緣，不能把上一列角色的腳底殘留在下一列。
輸出：一張非 APNG 的 PNG，真正的透明背景（alpha），${columns * 5} 格角色排在同一張圖中。不要白底、色塊背景或畫進圖片的透明棋盤格。
不要：文字、動作名稱、行列編號、尺寸標註、浮水印、場景、介面、表格、格線、說明海報或額外動作列。`;
}
async function copySpritePrompt(textarea, title) {
  const status = $("#sprite-prompt-status");
  try {
    await navigator.clipboard.writeText(textarea.value);
    status.textContent = `已複製「${title}」完整提示詞，可貼到圖片 AI。`;
  } catch (_) {
    textarea.focus(); textarea.select();
    status.textContent = "未能自動複製，已選取提示詞；請按 Ctrl+C，或長按文字複製。";
  }
}
function renderSpritePrompts() {
  const list = $("#sprite-prompt-list"); list.replaceChildren();
  const character = $("#sprite-prompt-character").value;
  const columns = Number($("#sprite-template").elements.columns.value);
  $("#sprite-prompt-status").textContent = "";
  const card = node("article", undefined, "item stack");
  card.append(node("h3", `整套角色動畫模板 · ${columns} 欄×5 列`));
  const label = node("label", "角色模板完整提示詞");
  const textarea = node("textarea"); textarea.readOnly = true; textarea.rows = 12;
  textarea.value = spritePrompt(character, columns); label.append(textarea);
  const button = node("button", "複製整套角色提示詞"); button.type = "button";
  action(button, () => copySpritePrompt(textarea, "角色模板"));
  card.append(label, button); list.append(card);
}
function applySpritePreset() {
  const description = $("#sprite-prompt-preset").selectedOptions[0].dataset.description;
  if (description) $("#sprite-prompt-character").value = description;
  renderSpritePrompts();
}
function clearSpriteTemplatePreview() {
  spriteTemplatePreviewVersion += 1;
  clearSpriteAnimations(spriteTemplateAnimations);
  spriteTemplatePreview = null;
  const form = $("#sprite-template");
  form.elements.confirm.checked = false; form.elements.confirm.disabled = true;
  $("button[type=submit]", form).disabled = true;
  $("#sprite-template-preview").replaceChildren();
  $("#sprite-template-status").textContent = "";
}
async function previewSpriteTemplate() {
  clearSpriteTemplatePreview();
  const version = spriteTemplatePreviewVersion;
  const animations = [];
  const form = $("#sprite-template");
  const file = form.elements.file.files[0];
  const fps = form.elements.fps.value;
  const columns = form.elements.columns.value;
  const cleanEdges = form.elements.clean_edges.checked;
  const token = csrfToken;
  if (!file) throw new Error("請選擇完整角色模板 PNG。");
  if (file.size > 5 * 1024 * 1024) throw new Error("PNG 檔案不可超過 5 MiB。");
  if (!form.elements.fps.reportValidity()) return;
  $("#sprite-template-status").textContent = "正在驗證並切圖預覽…";
  try {
    const result = await multipartApi("/api/admin/sprite-template/preview", new FormData(form));
    if (!Array.isArray(result?.items) || result.items.length !== 5 ||
        result.items.some((item, index) => item.state !== Object.keys(SPRITE_STATE_LABELS)[index]
          || item.frame_count !== Number(columns) || item.frame_width !== 64 || item.frame_height !== 64 || item.fps !== Number(fps))) {
      throw new Error("模板預覽回應不完整。");
    }
    const cards = [];
    for (const item of result.items) {
      const bytes = Uint8Array.from(atob(item.png_base64), char => char.charCodeAt(0));
      const image = await createImageBitmap(new Blob([bytes], {type: "image/png"}));
      const canvas = node("canvas"); canvas.width = item.frame_count * 64; canvas.height = 64; canvas.className = "sprite-preview";
      canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", `${SPRITE_STATE_LABELS[item.state]}的${item.frame_count}個影格`);
      canvas.getContext("2d").drawImage(image, 0, 0);
      const card = node("article", undefined, "item");
      card.append(node("h3", `${SPRITE_STATE_LABELS[item.state]} · ${item.state}`), canvas,
        spriteAnimationPreview(image, item, animations));
      cards.push(card);
    }
    if (version !== spriteTemplatePreviewVersion || token !== csrfToken || file !== form.elements.file.files[0]
        || fps !== form.elements.fps.value || columns !== form.elements.columns.value || cleanEdges !== form.elements.clean_edges.checked) {
      throw new Error("檔案、影格數、清理設定、FPS 或登入狀態已變更，請重新預覽。");
    }
    $("#sprite-template-preview").replaceChildren(...cards);
    spriteTemplateAnimations.push(...animations);
    spriteTemplatePreview = {file, fps, columns, cleanEdges, token};
    form.elements.confirm.disabled = false;
    $("#sprite-template-status").textContent = `預覽完成，尚未保存。可播放／暫停每列動畫，確認動作銜接及透明背景；匯入後每種狀態為64×64、${columns}影格，角色已自動對齊格子中央與地面。${cleanEdges ? "已啟用保守跨列殘線清理，無明確證據的線條保留。" : ""}`;
  } catch (error) {
    clearSpriteAnimations(animations);
    if (version === spriteTemplatePreviewVersion) $("#sprite-template-status").textContent = error.message;
    throw error;
  }
}
async function submitSpriteTemplate() {
  const form = $("#sprite-template");
  if (!spriteTemplatePreview || !form.elements.confirm.checked || spriteTemplatePreview.token !== csrfToken ||
      spriteTemplatePreview.file !== form.elements.file.files[0] || spriteTemplatePreview.fps !== form.elements.fps.value
      || spriteTemplatePreview.columns !== form.elements.columns.value || spriteTemplatePreview.cleanEdges !== form.elements.clean_edges.checked) {
    throw new Error("請先預覽目前檔案，再確認五種狀態的對應。");
  }
  await multipartApi("/api/admin/sprite-template", new FormData(form), "PUT");
  form.elements.file.value = ""; clearSpriteTemplatePreview();
  $("#sprite-template-status").textContent = "全部五種狀態已驗證並匯入。";
  await loadSprites(); message("角色模板已匯入五種狀態。");
}
formAction($("#schedule"), async () => {
  await api("/api/admin/schedule", {method: "PUT", body: JSON.stringify({time: $("#schedule [name=time]").value})});
  message("每日搜尋時間已保存。以台灣時間執行。");
});
action($("#run-daily"), async () => {
  await api("/api/admin/search-runs", {method: "POST"});
  $("#daily-admin-status").textContent = "這輪已開始，執行紀錄會更新。"; await loadRuns();
});
action($("#refresh-runs"), () => loadRuns());
applySpritePreset();
$("#sprite-prompt-preset").addEventListener("change", applySpritePreset);
$("#sprite-prompt-character").addEventListener("input", () => {
  $("#sprite-prompt-preset").value = "custom";
  renderSpritePrompts();
});
action($("#preview-sprite-template"), previewSpriteTemplate);
formAction($("#sprite-template"), submitSpriteTemplate);
$("#sprite-template [name=file]").addEventListener("change", clearSpriteTemplatePreview);
$("#sprite-template [name=fps]").addEventListener("input", clearSpriteTemplatePreview);
$("#sprite-template [name=columns]").addEventListener("change", () => { clearSpriteTemplatePreview(); renderSpritePrompts(); });
$("#sprite-template [name=clean_edges]").addEventListener("change", clearSpriteTemplatePreview);
$("#sprite-template [name=confirm]").addEventListener("change", () => {
  $("#sprite-template button[type=submit]").disabled = !spriteTemplatePreview || !$("#sprite-template [name=confirm]").checked;
});
formAction($("#sprite-upload"), async () => {
  const form = $("#sprite-upload");
  await multipartApi(`/api/admin/sprites/${encodeURIComponent(form.elements.state.value)}`, new FormData(form), "PUT");
  form.elements.file.value = ""; await loadSprites(); message("素材已驗證並保存。");
});
formAction($("#quotes"), async () => {
  const texts = $("#quotes [name=texts]").value.split(/\r?\n/).map(text => text.trim()).filter(Boolean);
  await api("/api/admin/quotes", {method: "PUT", body: JSON.stringify({texts})}); message("語錄已保存。");
});
action($("#push-test"), async () => {
  $("#push-status").textContent = "正在送出測試推播…";
  try {
    renderPushTest(await api("/api/admin/push-test", {method: "POST"}));
  } catch (error) {
    $("#push-status").textContent = error.message;
    throw error;
  }
});
action($("#create-backup"), async () => {
  $("#backup-status").textContent = "正在建立一致性備份…";
  try {
    const result = await api("/api/admin/backups", {method: "POST"});
    $("#backup-status").textContent = `備份已建立：${result.id}（${formatBytes(result.size)}）。`;
    await loadBackups();
  } catch (error) {
    $("#backup-status").textContent = error.message;
    throw error;
  }
});
action($("#refresh-backups"), async () => {
  $("#backup-status").textContent = "正在刷新備份清單…";
  try {
    await loadBackups();
    $("#backup-status").textContent = "備份清單已刷新。";
  } catch (error) {
    $("#backup-status").textContent = error.message;
    throw error;
  }
});
formAction($("#restore-form"), submitRestore);
action($("#search-test"), testSearchConnection);
async function copyAccess() {
  const input = $("#access-link");
  const link = input.value;
  if (!link) return;
  try {
    await navigator.clipboard.writeText(link);
    if (input.value === link) $("#access-copy-status").textContent = "已複製完整連結。";
  } catch (_) {
    if (input.value !== link) return;
    input.focus(); input.select();
    $("#access-copy-status").textContent = "未能自動複製，已選取連結；請按 Ctrl+C，或長按文字複製。";
  }
}
function hideAccess() {
  $("#access-link").value = "";
  $("#copy-access").disabled = true;
  $("#access-copy-status").textContent = "";
  $("#open-access").removeAttribute("href");
  $("#access-owner").textContent = "";
  $("#access-result").hidden = true;
}
// --- accounts ---
let pendingUserAction = null;
let userBusy = false;
const USER_CONFIRMS = {
  link: ["重設專屬連結？", name => `「${name}」的舊連結會失效，並登出此帳號的所有裝置。`, "確認重設"],
  "remove-link": ["移除專屬連結？", name => `「${name}」會被登出，建立新連結前無法再進入。`, "確認移除"],
  "sign-out": ["登出所有裝置？", name => `「${name}」的所有前台裝置會登出，專屬連結仍可使用。`, "確認登出"],
  disable: ["停用帳號？", name => `「${name}」會被登出且無法進入，資料保留；之後可再啟用。`, "確認停用"],
  delete: ["刪除帳號？", name => `會一併刪除「${name}」的所有對話、收藏、求職條件與每日職缺，無法復原。`, "確認刪除"],
};
function userStatus(user) {
  return [user.disabled ? "已停用" : null, user.has_link ? "已有專屬連結" : "尚無專屬連結",
    `${user.devices} 台裝置已登入`].filter(Boolean).join("・");
}
function renderUsers(items) {
  const list = $("#user-list");
  list.replaceChildren(); linkButtons = [];
  if (!items.length) { list.append(node("p", "尚無使用者帳號。", "hint")); return; }
  for (const user of items) {
    const row = node("article", undefined, "item");
    row.append(node("h3", user.username), node("p", userStatus(user), "hint"));
    const buttons = node("div", undefined, "row");
    const add = (kind, label, primary = false) => {
      const button = node("button", label, primary ? "primary" : undefined);
      button.type = "button";
      button.addEventListener("click", () => requestUserAction(kind, user));
      buttons.append(button);
      return button;
    };
    if (!user.disabled) {
      const link = add("link", user.has_link ? "重設專屬連結" : "建立專屬連結", !user.has_link);
      link.disabled = sharingBusy; linkButtons.push(link);
    }
    if (user.has_link) add("remove-link", "移除連結");
    if (user.devices) add("sign-out", "登出所有裝置");
    add(user.disabled ? "enable" : "disable", user.disabled ? "啟用" : "停用");
    add("delete", "刪除帳號");
    row.append(buttons); list.append(row);
  }
}
async function loadUsers() {
  const result = await api("/api/admin/users");
  renderUsers(Array.isArray(result?.items) ? result.items : []);
}
function showAccess(result, username, created) {
  const link = new URL(result.access_path, result.access_origin || window.location.origin).href;
  $("#access-link").value = link; $("#open-access").href = link;
  $("#copy-access").disabled = !link;
  $("#access-copy-status").textContent = "";
  $("#access-owner").textContent = `「${username}」的專屬連結`;
  $("#access-result").hidden = false;
  const funnelReady = sharingSnapshot?.state === "ready" && Boolean(sharingSnapshot.origin);
  const originMatches = !funnelReady || result.access_origin === sharingSnapshot.origin;
  message((created ? `已建立「${username}」的專屬連結。` : `「${username}」的新連結已建立；舊連結與該帳號的前台登入已撤銷。`) + (funnelReady
    ? originMatches ? "對方不需安裝 Tailscale；請透過這次畫面交付。" : "後台回傳網址與 Funnel 網址不同，請勿交付；重新檢查後再建立。"
    : result.access_origin ? "此連結使用目前已設定的公開網址；請先完成外部連線，再交付外部使用者。"
      : "尚未設定對外 HTTPS 網址，此連結僅供本機使用。"), funnelReady && !originMatches);
}
async function performUserAction(kind, user) {
  const base = `/api/admin/users/${encodeURIComponent(user.id)}`;
  hideAccess();
  if (kind === "link") {
    showAccess(await api(`${base}/access/reset`, {method: "POST"}), user.username, false);
  } else if (kind === "remove-link") {
    await api(`${base}/access`, {method: "DELETE"}); message(`已移除「${user.username}」的專屬連結，並登出其裝置。`);
  } else if (kind === "sign-out") {
    await api(`${base}/sign-out`, {method: "POST"}); message(`已登出「${user.username}」的所有裝置。`);
  } else if (kind === "disable" || kind === "enable") {
    await api(base, {method: "PATCH", body: JSON.stringify({disabled: kind === "disable"})});
    message(kind === "disable" ? `已停用「${user.username}」。` : `已啟用「${user.username}」。`);
  } else if (kind === "delete") {
    await api(`${base}?confirm=true`, {method: "DELETE"}); message(`已刪除「${user.username}」及其全部資料。`);
  } else throw new Error("不支援的操作。");
  await loadUsers();
}
async function runUserAction(kind, user) {
  if (userBusy) return;
  userBusy = true;
  try { await performUserAction(kind, user); } catch (error) { message(error.message, true); }
  finally { userBusy = false; }
}
function requestUserAction(kind, user) {
  const confirmation = USER_CONFIRMS[kind];
  if (!confirmation) { runUserAction(kind, user); return; }
  pendingUserAction = {kind, user};
  $("#user-confirm-title").textContent = confirmation[0];
  $("#user-confirm-text").textContent = confirmation[1](user.username);
  $("#user-confirm-ok").textContent = confirmation[2];
  $("#user-confirm").returnValue = "cancel";
  $("#user-confirm").showModal();
}
// --- end accounts ---
function signedOut() {
  resetModelProfiles();
  csrfToken = ""; invalidateSharing(); hideAccess();
  $("#user-list").replaceChildren(); linkButtons = []; pendingUserAction = null;
  spriteListVersion += 1; clearSpriteAnimations(spriteListAnimations);
  $("#sprite-template").reset(); clearSpriteTemplatePreview(); renderSpritePrompts();
  clearTimeout(runPoll); $("#run-list").replaceChildren(); $("#sprite-list").replaceChildren();
  $("#quotes").reset(); $("#sprite-upload").reset(); $("#daily-admin-status").textContent = "";
  $("#settings-panel").hidden = true; $("#logout").hidden = true;
  $("#login-panel").hidden = false;
  $("#services").reset(); $("#default-list").replaceChildren();
  $("#backup-list").replaceChildren(); $("#push-test-results").replaceChildren();
  $("#push-status").textContent = ""; $("#backup-status").textContent = "";
  $("#restore-form").reset(); $("#restore-status").textContent = "";
  $("#restore-result").textContent = ""; $("#restore-result").hidden = true;
  $("#llm-status").textContent = "";
  $("#search-engine-name").textContent = ""; $("#search-status").textContent = "";
}
function setSourceSwitch(button, enabled) {
  button.setAttribute("aria-checked", String(enabled));
  button.textContent = enabled ? "開啟" : "關閉";
}
function renderLLMSettings(settings) {
  const form = $("#services");
  for (const key of ["llm_base_url", "llm_model"]) form.elements[key].value = settings[key] || "";
  form.elements.llm_protocol.value = settings.llm_protocol || "auto";
  form.elements.llm_context_window.value = settings.llm_context_window || "";
  form.elements.llm_api_key.value = "";
  form.elements.clear_llm_api_key.checked = false;
  $('[data-status=llm_api_key]').textContent = settings.llm_api_key.configured ? "已設定 ••••••••" : "尚未設定";
  $("#llm-status").textContent = settings.connection_test_status === "passed" ? `文字與工具往返測試通過 · ${settings.detected_protocol}` : "尚未通過目前設定的連線測試。";
}
async function loadSettings() {
  const settings = await api("/api/admin/settings");
  const form = $("#services");
  renderLLMSettings(settings);
  form.elements.vapid_subject.value = settings.vapid_subject || "";
  form.elements.search_provider.value = settings.search_provider || "";
  form.elements.admin_local_only.checked = Boolean(settings.admin_local_only);
  setSourceSwitch(form.elements.source_518_enabled, settings.source_518_enabled !== false);
  setSourceSwitch(form.elements.source_1111_enabled, settings.source_1111_enabled === true);
  for (const key of secretKeys) {
    form.elements[key].value = ""; form.elements[`clear_${key}`].checked = false;
    $(`[data-status=${key}]`).textContent = settings[key].configured ? "已設定 ••••••••" : "尚未設定";
  }
  const provider = settings.active_search_provider;
  const configured = Boolean(provider && settings[`${provider.toLowerCase()}_api_key`]?.configured);
  $("#search-engine-name").textContent = provider ? `目前搜尋服務：${provider}` : "網路搜尋尚未啟用。";
  $("#search-test").disabled = !configured;
  $("#search-status").textContent = configured ? searchTestStatus(settings.search_connection_test) : "請選擇搜尋引擎、設定金鑰並保存服務設定。";
}
function addDefault(criteria) {
  const item = node("div", undefined, "item stack");
  item.append(criteriaFields(criteria));
  const remove = node("button", "移除這組預設"); remove.type = "button";
  remove.addEventListener("click", () => item.remove());
  item.append(remove); $("#default-list").append(item);
}
async function signedIn() {
  await loadSettings();
  await loadModelProfiles();
  const schedule = await api("/api/admin/schedule"); $("#schedule [name=time]").value = schedule.time;
  const quotes = await api("/api/admin/quotes"); $("#quotes [name=texts]").value = quotes.texts.join("\n");
  await loadSprites(); await loadRuns(); await loadBackups(); await loadUsers();
  try { regionCatalog = (await api("/api/admin/areas")).items; } catch (error) { message(error.message, true); }
  const defaults = await api("/api/admin/default-preferences");
  $("#default-list").replaceChildren(); defaults.preferences.forEach(addDefault);
  $("#login-panel").hidden = true; $("#settings-panel").hidden = false; $("#logout").hidden = false;
  await refreshSharingStatus();
  await loadPorts();
}
async function loginContext() {
  const context = await api("/api/admin/login-context");
  csrfToken = context.csrf_token;
  $("#setup").hidden = context.initialized;
  $("#login button").disabled = !context.initialized;
}
formAction($("#login"), async () => {
  const input = $("#login [name=password]");
  const password = input.value;
  input.value = "";
  const result = await api("/api/admin/login", {method: "POST", body: JSON.stringify({password})});
  csrfToken = result.csrf_token;
  await signedIn(); message("已登入管理後台。");
});
action($("#logout"), async () => {
  await api("/api/admin/logout", {method: "POST"}); signedOut();
  await loginContext(); message("已登出。");
});
for (const name of ["source_518_enabled", "source_1111_enabled"]) {
  const button = $("#services").elements[name];
  button.addEventListener("click", () => setSourceSwitch(button, button.getAttribute("aria-checked") !== "true"));
}
$("#services").addEventListener("reset", () => {
  setSourceSwitch($("#services").elements.source_518_enabled, true);
  setSourceSwitch($("#services").elements.source_1111_enabled, false);
});
formAction($("#services"), async () => {
  const form = $("#services");
  const vapid_subject = form.elements.vapid_subject.value.trim();
  if (!validVapidSubject(vapid_subject)) throw new Error("VAPID 聯絡 URI 必須是 mailto 有效 email 或 HTTPS URL。");
  const values = {llm_base_url: form.elements.llm_base_url.value.trim(), llm_model: form.elements.llm_model.value.trim(),
    llm_protocol: form.elements.llm_protocol.value, llm_context_window: Number(form.elements.llm_context_window.value) || 0, admin_local_only: form.elements.admin_local_only.checked,
    source_518_enabled: form.elements.source_518_enabled.getAttribute("aria-checked") === "true",
    source_1111_enabled: form.elements.source_1111_enabled.getAttribute("aria-checked") === "true", vapid_subject,
    search_provider: form.elements.search_provider.value || null,
    clear_secrets: secretKeys.filter(key => form.elements[`clear_${key}`].checked)};
  for (const key of secretKeys) values[key] = form.elements[key].value;
  await api("/api/admin/settings", {method: "PUT", body: JSON.stringify(values)});
  await loadSettings(); await loadModelProfiles(); message("設定已保存，可測試模型與搜尋連線。");
});
action($("#llm-test"), async () => {
  $("#llm-status").textContent = "正在測試文字與工具往返，推理模型可能需要較久，整體最多等候 10 分鐘…";
  try {
    const result = await api("/api/admin/llm-test", {method: "POST"});
    $("#llm-status").textContent = `文字與工具往返測試通過 · ${result.protocol}`;
    message("模型文字與工具測試通過，可以回到前台聊天。");
  } catch (error) {
    $("#llm-status").textContent = [error.code && `錯誤代碼：${error.code}`, error.message, error.diagnostic].filter(Boolean).join(" · ");
    throw error;
  }
});
action($("#add-default"), async () => {
  if ($("#default-list").children.length >= 20) throw new Error("最多 20 組預設條件。");
  addDefault();
});
formAction($("#defaults"), async () => {
  const preferences = [...$("#default-list").children].map(readCriteria);
  await api("/api/admin/default-preferences", {method: "PUT", body: JSON.stringify({preferences})});
  message("預設條件已保存；使用者已確認的條件不受影響。");
});
$("#sharing-primary").addEventListener("click", () => performSharingAction(primarySharingAction()));
$("#ports-form").addEventListener("submit", event => { event.preventDefault(); savePorts(); });
$("#sharing-refresh").addEventListener("click", () => performSharingAction("check"));
$("#sharing-disable").addEventListener("click", () => {
  if (sharingSnapshot?.can_disable !== true) return;
  $("#sharing-disable-confirm").returnValue = "cancel";
  $("#sharing-disable-confirm").showModal();
});
$("#sharing-disable-confirm").addEventListener("close", () => {
  if ($("#sharing-disable-confirm").returnValue === "confirm") performSharingAction("disable");
});
$("#user-confirm").addEventListener("close", async () => {
  const pending = pendingUserAction; pendingUserAction = null;
  if (!pending || $("#user-confirm").returnValue !== "confirm") return;
  await runUserAction(pending.kind, pending.user);
});
formAction($("#user-form"), async () => {
  const input = $("#user-form [name=username]");
  const result = await api("/api/admin/users", {method: "POST", body: JSON.stringify({username: input.value.trim()})});
  input.value = "";
  hideAccess();
  showAccess(result, result.username, true);
  await loadUsers();
});
$("#copy-access").addEventListener("click", copyAccess);
$("#hide-access").addEventListener("click", hideAccess);
window.addEventListener("pagehide", () => {
  resetModelProfiles();
  invalidateSharing();
  spriteListVersion += 1; clearSpriteAnimations(spriteListAnimations);
  hideAccess(); $("#services").reset(); $("#login").reset(); $("#restore-form").reset();
  $("#sprite-template").reset(); clearSpriteTemplatePreview();
});
window.addEventListener("pageshow", event => { if (event.persisted) window.location.reload(); });
window.addEventListener("session-expired", () => { signedOut(); loginContext().catch(error => message(error.message, true)); });
(async () => {
  try {
    const auth = await api("/api/admin/session"); csrfToken = auth.csrf_token; await signedIn();
  } catch (error) {
    if (error.status === 401) { try { await loginContext(); } catch (failure) { message(failure.message, true); } }
    else message(error.message, true);
  }
})();
