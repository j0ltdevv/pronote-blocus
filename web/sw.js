/* Pronote Blocus — Service Worker */
/* v4 : design v0 (espace élève) porté sur l'app branchée */

const CACHE_NAME = 'pronote-blocus-v4';
const API_CACHE = 'pronote-blocus-api-v4';
const OFFLINE_PAGE = '/offline.html';

const APP_SHELL_URLS = [
  '/',
  '/index.html',
  '/app.js',
  '/styles.css',
  '/manifest.json',
  '/offline.html',
  '/fonts/bricolage.woff2',
  '/icons/icon.svg'
];

/* Icons & static assets handled cache-first */
const STATIC_ASSET_PATTERN = /\.(png|jpg|jpeg|gif|webp|ico|woff|woff2|ttf|eot|svg)$/;

/* --- Install: cache app shell --- */
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL_URLS))
      .then(() => self.skipWaiting())
  );
});

/* --- Activate: clean old caches --- */
self.addEventListener('activate', (event) => {
  const currentCaches = [CACHE_NAME, API_CACHE];

  event.waitUntil(
    caches.keys()
      .then((cacheNames) => {
        return Promise.all(
          cacheNames.map((cacheName) => {
            if (!currentCaches.includes(cacheName)) {
              return caches.delete(cacheName);
            }
            return null;
          })
        );
      })
      .then(() => self.clients.claim())
  );
});

/* --- Fetch: routing strategies --- */
self.addEventListener('fetch', (event) => {
  const { request } = event;

  /* Ignorer les requêtes non GET */
  if (request.method !== 'GET') {
    return;
  }

  const url = new URL(request.url);

  /* Network-first for API calls (*never* cache responses persistently) */
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          /* Pas de mise en cache des réponses API */
          return response;
        })
        .catch(() => {
          /* Fallback API offline — renvoie un JSON minimal */
          return new Response(
            JSON.stringify({ error: 'offline', message: 'Pas de connexion réseau.' }),
            {
              status: 503,
              headers: { 'Content-Type': 'application/json' }
            }
          );
        })
    );
    return;
  }

  /* Cache-first for static assets (images, fonts, etc.) */
  if (STATIC_ASSET_PATTERN.test(url.pathname)) {
    event.respondWith(
      caches.match(request)
        .then((cached) => {
          if (cached) {
            return cached;
          }
          return fetch(request)
            .then((response) => {
              if (!response.ok) {
                throw new Error('Network response was not ok');
              }
              const responseToCache = response.clone();
              caches.open(CACHE_NAME)
                .then((cache) => cache.put(request, responseToCache));
              return response;
            })
            .catch(() => {
              /* Option: favicon fallback ou image placeholder */
              if (url.pathname.includes('png') || url.pathname.includes('jpg') || url.pathname.includes('svg')) {
                return caches.match('/icons/icon.svg');
              }
              return new Response('', { status: 404 });
            });
        })
    );
    return;
  }

  /* App shell: network-first then fallback cache */
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (!response.ok) {
          throw new Error('Network response was not ok');
        }
        const responseToCache = response.clone();
        caches.open(CACHE_NAME)
          .then((cache) => cache.put(request, responseToCache));
        return response;
      })
      .catch(() => caches.match(request))
      .then((cached) => {
        return cached;
      })
      .catch(() => {
        /* Fallback: page offline */
        if (request.destination === 'document') {
          return caches.match(OFFLINE_PAGE);
        }
        return new Response('', { status: 404 });
      })
  );
});

/* --- Offline page (generated at runtime si absente) --- */
self.addEventListener('message', (event) => {
  if (event.data === 'skipWaiting') {
    self.skipWaiting();
  }
});
