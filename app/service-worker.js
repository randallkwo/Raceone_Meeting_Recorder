/* RaceOne Meeting Recorder — service worker
   重點：絕不快取 '/'，因為它內含「是否已驗證」的狀態（伺服器以 no-store 送出）。
   只快取靜態資產，讓 App 能啟動；API 一律直連。 */
const SHELL = 'raceone-meeting-shell-v3';
const SHELL_FILES = ['/manifest.json', '/icon-192.png', '/icon-512.png',
                     '/raceone-logo.jpg'];

const OFFLINE_HTML = `<!DOCTYPE html><html lang="zh-TW"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>離線中</title><style>body{font-family:-apple-system,'PingFang TC',sans-serif;
background:#F9F9F6;color:#1F2937;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;text-align:center;line-height:1.7}h1{font-size:17px;margin-bottom:8px}
p{font-size:13px;color:#6B7280}</style></head><body><div>
<h1>目前離線</h1><p>恢復連線後重新開啟即可。<br>錄音中的切片會先排隊等待上傳。</p>
</div></body></html>`;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // 只處理同源 GET
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  // API 與 /health 一律直連
  if (url.pathname.startsWith('/upload') ||
      url.pathname.startsWith('/end-meeting') ||
      url.pathname.startsWith('/health')) return;

  // '/' 內含驗證狀態 → 只用網路，離線時給靜態提示頁
  if (url.pathname === '/') {
    e.respondWith(fetch(e.request).catch(() =>
      new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
    return;
  }

  // 其餘靜態資產：網路優先，失敗回快取
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(SHELL).then(c => c.put(e.request, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
