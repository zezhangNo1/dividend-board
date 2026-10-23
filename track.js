/* 隐形浏览量埋点：不上报任何展示，只 POST 一条 {path, q, ref}。
   页面无感知；统计视图仅站长可见。 */
(function () {
  try {
    var payload = JSON.stringify({
      path: location.pathname || '/',
      q: (location.search || '').slice(0, 60),
      ref: document.referrer || ''
    });
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/track/view',
        new Blob([payload], { type: 'application/json' }));
    } else {
      fetch('/api/track/view', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: payload, keepalive: true
      }).catch(function () {});
    }
  } catch (e) { /* 埋点失败不影响页面 */ }
})();
