/* 社媒关注入口：小红书主页外链 + 公众号二维码弹层。
   配置留空时对应入口自动隐藏，不影响页面。
   素材就位方式：FOLLOW.xhs 填主页链接；二维码图放 web/gzh_qr.png。 */
(function () {
  var FOLLOW = {
    xhs: 'https://xhslink.cn/o/9tvA8ec3WIn',  // 小红书主页分享短链（失效时换成新的分享链接即可）
    xhsName: '小红书',
    gzhQR: 'gzh_qr.jpg', // 公众号「泽克的数据笔记」关注二维码
    gzhName: '公众号'
  };

  var xhsEls = document.querySelectorAll('[data-follow="xhs"]');
  var gzhEls = document.querySelectorAll('[data-follow="gzh"]');

  if (FOLLOW.xhs) {
    xhsEls.forEach(function (a) {
      a.href = FOLLOW.xhs;
      a.target = '_blank';
      a.rel = 'noopener';
      a.style.display = '';
      if (!a.textContent.trim()) a.textContent = FOLLOW.xhsName;
    });
  } else {
    xhsEls.forEach(function (a) { a.style.display = 'none'; });
  }

  function probe(url, ok) {
    var img = new Image();
    img.onload = ok;
    img.onerror = function () { gzhEls.forEach(function (a) { a.style.display = 'none'; }); };
    img.src = url;
  }

  function openQr() {
    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;z-index:999;background:rgba(0,0,0,.55);' +
      'display:flex;align-items:center;justify-content:center;padding:24px';
    var card = document.createElement('div');
    card.style.cssText = 'background:var(--card,#fff);border-radius:16px;padding:22px 24px;' +
      'max-width:300px;width:100%;text-align:center;box-shadow:0 12px 40px rgba(0,0,0,.3)';
    var t = document.createElement('div');
    t.textContent = '关注' + FOLLOW.gzhName;
    t.style.cssText = 'font-size:15px;font-weight:700;color:var(--t1,#171a20);margin-bottom:12px';
    var img = document.createElement('img');
    img.src = FOLLOW.gzhQR + '?v=1';
    img.style.cssText = 'width:100%;max-width:240px;border-radius:10px;display:block;margin:0 auto';
    var tip = document.createElement('div');
    tip.textContent = '长按识别二维码关注';
    tip.style.cssText = 'font-size:12px;color:var(--t2,#5a6070);margin-top:12px';
    var close = document.createElement('div');
    close.textContent = '关闭';
    close.style.cssText = 'font-size:12.5px;color:var(--t3,#98a0ae);margin-top:14px;cursor:pointer';
    close.onclick = function () { document.body.removeChild(ov); };
    ov.onclick = function (e) { if (e.target === ov) document.body.removeChild(ov); };
    card.appendChild(t); card.appendChild(img); card.appendChild(tip); card.appendChild(close);
    ov.appendChild(card);
    document.body.appendChild(ov);
  }

  probe(FOLLOW.gzhQR, function () {
    gzhEls.forEach(function (a) {
      a.style.display = '';
      if (!a.textContent.trim()) a.textContent = FOLLOW.gzhName;
      a.addEventListener('click', function (e) { e.preventDefault(); openQr(); });
    });
  });
})();
