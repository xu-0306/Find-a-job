"use strict";

const SHELL_CACHE = "job-companion-shell-v25";
const OFFLINE_URL = "/static/offline.html";
const ICON_URL = "/static/icon.svg";
const STYLE_URL = "/static/style.css";
// The stylesheet is a fixed shell dependency of offline.html; no page/API response is cached.
const PRECACHE_URLS = [OFFLINE_URL, ICON_URL, STYLE_URL];
const SAFE_HOME = "/";
const SAFE_DAILY = "/?page=daily";
const MAX_PAYLOAD_CHARS = 4096;
const MAX_TITLE_CHARS = 80;
const MAX_BODY_CHARS = 240;

function sameOrigin(url) {
  return url.origin === self.location.origin;
}

function safeNavigation(url) {
  if (!sameOrigin(url) || url.pathname !== "/") return false;
  const entries = [...url.searchParams.entries()];
  return !entries.length || (entries.length === 1 && entries[0][0] === "page" && entries[0][1] === "daily");
}

function safeTarget(value) {
  if (typeof value !== "string") return SAFE_DAILY;
  try {
    const url = new URL(value, self.location.origin);
    if (!safeNavigation(url)) return SAFE_DAILY;
    return url.search ? SAFE_DAILY : SAFE_HOME;
  } catch (_) {
    return SAFE_DAILY;
  }
}

function textValue(value, limit) {
  return typeof value === "string" && value.trim() && value.length <= limit ? value : null;
}

const neutralNotification = Object.freeze({
  title: "找工作小幫手",
  body: "今天有新的提醒，點開查看今日職缺。",
  url: SAFE_DAILY,
});

async function notificationPayload(data) {
  if (!data || typeof data.text !== "function") return neutralNotification;
  try {
    const raw = data.text();
    if (typeof raw !== "string" || raw.length > MAX_PAYLOAD_CHARS) return neutralNotification;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return neutralNotification;
    const title = textValue(parsed.title, MAX_TITLE_CHARS);
    const body = textValue(parsed.body, MAX_BODY_CHARS);
    if (!title || !body) return neutralNotification;
    return {title, body, url: safeTarget(parsed.url)};
  } catch (_) {
    return neutralNotification;
  }
}

async function handleNavigation(request) {
  const url = new URL(request.url);
  if (!safeNavigation(url)) return fetch(request);
  try {
    // Do not put the authenticated shell or any query response into Cache Storage.
    return await fetch(request);
  } catch (_) {
    return caches.match(OFFLINE_URL);
  }
}

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then(cache => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(key => key.startsWith("job-companion-shell-") && key !== SHELL_CACHE)
        .map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  let url;
  try { url = new URL(request.url); } catch (_) { return; }
  if (!sameOrigin(url)) return;

  if (request.mode === "navigate") {
    if (!safeNavigation(url)) return;
    event.respondWith(handleNavigation(request));
    return;
  }

  if (PRECACHE_URLS.includes(url.pathname) && !url.search && !url.hash) {
    event.respondWith(url.pathname === STYLE_URL
      ? fetch(request).catch(() => caches.match(request))
      : caches.match(request).then(cached => cached || fetch(request)));
  }
});

self.addEventListener("push", event => {
  event.waitUntil(notificationPayload(event.data).then(payload => self.registration.showNotification(payload.title, {
    body: payload.body,
    icon: ICON_URL,
    badge: ICON_URL,
    tag: "job-companion-daily",
    data: {url: payload.url},
  })));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = safeTarget(event.notification.data && event.notification.data.url);
  event.waitUntil((async () => {
    const targetUrl = new URL(target, self.location.origin).href;
    const clients = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    for (const client of clients) {
      let clientUrl;
      try { clientUrl = new URL(client.url); } catch (_) { continue; }
      if (!sameOrigin(clientUrl)) continue;
      try {
        if (typeof client.navigate === "function") await client.navigate(targetUrl);
        if (typeof client.focus === "function") return client.focus();
      } catch (_) {
        // A stale window can disappear between matchAll and navigate; try the next one.
      }
    }
    if (typeof self.clients.openWindow === "function") return self.clients.openWindow(targetUrl);
    return undefined;
  })());
});
