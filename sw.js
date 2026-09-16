const CACHE_NAME = 'messenger-v2';

self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
    // Пропускаем API и WebSocket — они не кэшируются
    if (event.request.url.includes('/ws/') || event.request.method !== 'GET') return;
    event.respondWith(
        caches.match(event.request).then(cached => {
            return cached || fetch(event.request).catch(() => cached);
        })
    );
});

// ── PUSH: получение уведомления от сервера ──
self.addEventListener('push', (event) => {
    const data = event.data ? event.data.json() : {};
    const title = data.title || '💬 Новое сообщение';
    const options = {
        body: data.body || '',
        icon: '/static/icon-192.png',
        badge: '/static/icon-96.png',
        tag: 'nexus-message',
        renotify: true,
        data: { url: self.location.origin },
        actions: [
            { action: 'open', title: 'Открыть' },
        ],
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

// ── Клик по push-уведомлению → открыть приложение ──
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
            for (const client of clients) {
                if (client.url && client.url.startsWith(event.notification.data.url) && 'focus' in client) {
                    return client.focus();
                }
            }
            return self.clients.openWindow(event.notification.data.url);
        })
    );
});