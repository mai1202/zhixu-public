// 只缓存公开离线说明，不缓存账户页面、聊天、附件或API响应；更新时删除本应用旧缓存。
const CACHE = 'zhixu-public-offline-v1';
self.addEventListener('install', event => { event.waitUntil(caches.open(CACHE).then(cache => cache.add('/offline.html'))); });
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('zhixu-public-offline-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  // 仅导航失败时提供离线说明；非导航请求完全交由浏览器正常处理。
  if (event.request.mode === 'navigate' && event.request.method === 'GET') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/offline.html')));
  }
});
