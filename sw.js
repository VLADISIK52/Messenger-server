// sw.js — сервис-воркер: ТОЛЬКО push-уведомления.
// Кэширования нет вообще — страница всегда свежая с сервера.

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    // Уничтожаем ВСЕ старые кэши прошлых версий воркера
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(keys.map(k => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

// ── PUSH: уведомление от сервера ──
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
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

// ── Клик по уведомлению → открыть приложение ──
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