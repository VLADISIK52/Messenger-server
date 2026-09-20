// sw.js — service worker Messenger: пуши + офлайн-кэш статики
const CACHE = 'messenger-cache-v2';

self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys()
            .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

// ГЛАВНОЕ: пуш при закрытом/свёрнутом сайте → системное уведомление
self.addEventListener('push', (event) => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
    const title = data.title || 'Messenger';
    const body = data.body || 'Новое сообщение';
    const options = {
        body: body,
        icon: '/icon-192.png',
        badge: '/icon-192.png',
        tag: data.tag || 'messenger',
        renotify: true,
        data: { url: data.url || '/' }
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

// Тап по уведомлению → открыть сайт/чат
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || '/';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
            for (const client of list) {
                if ('focus' in client) return client.focus();
            }
            return clients.openWindow(url);
        })
    );
});

// Сеть: статику кэшируем, страницы и API не кэшируем (чтобы не ломать приложение)
self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (event.request.method !== 'GET' || url.origin !== location.origin) return;
    if (url.pathname.startsWith('/uploads/') || url.pathname.startsWith('/static/') || url.pathname.endsWith('.png')) {
        event.respondWith(
            caches.open(CACHE).then(cache =>
                cache.match(event.request).then(hit =>
                    hit || fetch(event.request).then(resp => {
                        if (resp.ok) cache.put(event.request, resp.clone());
                        return resp;
                    }).catch(() => hit)
                )
            )
        );
    }
});