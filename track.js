/* 隐形浏览量埋点：不上报任何展示，只 POST 一条 {path, q, ref}。
   页面无感知；统计视图仅站长可见。
   跨站部署支持（如 nasdaq.zekes.top 引用前设置）：
     window.TRACK_URL  —— 上报地址（默认同源 /api/track/view）
     window.TRACK_PATH —— 归属路径（默认 location.pathname，供统计按站点分组） */
(function () {
  try {
    var payload = JSON.stringify({
      path: window.TRACK_PATH || (location.pathname || '/'),
      q: (location.search || '').slice(0, 60),
      ref: document.referrer || ''
    });
    var url = window.TRACK_URL || '/api/track/view';
    if (navigator.sendBeacon) {
      navigator.sendBeacon(url,
        new Blob([payload], { type: 'application/json' }));
    } else {
      fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: payload, keepalive: true, mode: 'cors'
      }).catch(function () {});
    }
  } catch (e) { /* 埋点失败不影响页面 */ }
})();
