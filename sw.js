// Минимальный service worker — нужен только для того, чтобы браузер
// разрешил "установить" сайт на телефон как приложение.
// Кеширование не делаем осознанно: мессенджеру всегда нужны свежие
// данные с сервера, а не старые закэшированные ответы.

self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    // Просто пропускаем все запросы напрямую в сеть, без кэша
    event.respondWith(fetch(event.request));
});
