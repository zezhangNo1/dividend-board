/* 广告位组件：拉取生效素材渲染到 .ad-slot 容器，曝光/点击 sendBeacon 上报。
   全部内联样式（用户 webview 对 JS 注入 <style> 不生效，历史教训）；
   素材固定带「广告」标签（互联网广告管理办法可识别性要求）；不做任何弹窗。 */
(function () {
  function beacon(id, type) {
    try {
      var payload = JSON.stringify({ id: id, type: type });
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/api/ads/track',
          new Blob([payload], { type: 'application/json' }));
      } else {
        fetch('/api/ads/track', { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: payload, keepalive: true }).catch(function () {});
      }
    } catch (e) {}
  }

  function render(el, s) {
    var a = document.createElement('a');
    a.href = s.link || '#';
    if (s.link) { a.target = '_blank'; a.rel = 'noopener nofollow sponsor'; }
    a.style.cssText = 'display:flex;align-items:center;gap:10px;margin:12px 0;' +
      'background:var(--card,#fff);border:1px solid var(--line,#e5e7eb);border-radius:12px;' +
      'padding:10px 14px;text-decoration:none;color:var(--t1,#171a20);' +
      'font-size:12.5px;box-shadow:0 1px 2px rgba(16,24,40,.05)';
    a.addEventListener('mouseenter', function () {
      a.style.borderColor = 'var(--brand,#0e9f6e)'; });
    a.addEventListener('mouseleave', function () {
      a.style.borderColor = 'var(--line,#e5e7eb)'; });

    var tag = document.createElement('span');
    tag.textContent = '广告';
    tag.style.cssText = 'flex:none;font-size:10px;font-weight:600;padding:1px 6px;' +
      'border-radius:5px;background:#fdf3e3;color:#b45309;border:1px solid #f3dfc0';
    a.appendChild(tag);

    var mid = document.createElement('span');
    mid.style.cssText = 'flex:1;min-width:0;display:flex;flex-direction:column;gap:2px';
    var b = document.createElement('span');
    b.textContent = s.title;
    b.style.cssText = 'font-size:13px;font-weight:600';
    mid.appendChild(b);
    if (s.body) {
      var i = document.createElement('span');
      i.textContent = s.body;
      i.style.cssText = 'color:var(--t2,#5a6070);font-size:11.5px;' +
        'white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
      mid.appendChild(i);
    }
    a.appendChild(mid);

    var sp = document.createElement('span');
    sp.textContent = s.sponsor;
    sp.style.cssText = 'flex:none;color:var(--t3,#98a0ae);font-size:11px';
    a.appendChild(sp);

    a.addEventListener('click', function () { beacon(s.id, 'click'); });
    el.appendChild(a);
    beacon(s.id, 'imp');
  }

  var slots = document.querySelectorAll('.ad-slot');
  if (!slots.length) return;
  fetch('/api/ads/slots').then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (!d || !d.slots) return;
      slots.forEach(function (el) {
        var s = d.slots[el.dataset.ad];
        if (s) render(el, s);
      });
    }).catch(function () {});
})();
