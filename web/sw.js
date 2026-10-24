"use strict";

const CACHE_NAME = "pocket-static-v74";
const SCOPE_PATH = "/pocket/";
const API_PATH = "/pocket/api/";
const STATIC_ASSETS = [
  "/pocket/",
  "/pocket/index.html",
  "/pocket/app.js",
  "/pocket/pad.js",
  "/pocket/style.css",
  "/pocket/strings.ja.js",
  "/pocket/manifest.json",
  "/pocket/icon-180.png",
  "/pocket/icon-512.png",
];
const STATIC_PATHS = new Set(STATIC_ASSETS.map((value) => new URL(value, self.location.origin).pathname));

function cacheableResponse(response) {
  return response && response.ok && !response.redirected && response.type !== "opaque";
}

async function refreshStatic(request) {
  const response = await fetch(request, { cache: "no-cache" });
  if (cacheableResponse(response)) {
    const cache = await caches.open(CACHE_NAME);
    await cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("install", (event) => {
  const freshAssets = STATIC_ASSETS.map((path) => new Request(path, { cache: "reload" }));
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(freshAssets))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((name) => name.startsWith("pocket-static-") && name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin ||
      !url.pathname.startsWith(SCOPE_PATH) || url.pathname.startsWith(API_PATH) ||
      !STATIC_PATHS.has(url.pathname)) {
    return;
  }
  event.respondWith(
    caches.open(CACHE_NAME).then((cache) => cache.match(request)).then((cached) => {
      const update = refreshStatic(request).catch(() => null);
      if (cached) {
        event.waitUntil(update);
        return cached;
      }
      return update.then((response) => response || Response.error());
    })
  );
});
