/* 个股股息档案卡组件 —— 在任意容器内直接渲染。
   CSS 已内联在各页面 <style>（.scard 前缀），本文件只负责渲染。
   用法：renderStockCard(el, row, quote, generated) */
(function(){
'use strict';
var TARGETS = [{rate:5.5,label:'5.5%'},{rate:5.0,label:'5.0%'},{rate:4.5,label:'4.5%'},{rate:4.0,label:'4.0%'}];
function fmt(v, d){ d = d || 2; return (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(d); }
function tierOf(dy){
  if (dy === null || dy === undefined) return ['t-low', '数据待补'];
  if (dy >= 5.5) return ['t-high', '深度价值区'];
  if (dy >= 5.0) return ['t-mid', '进入关注区'];
  return ['t-low', '观察区'];
}
window.renderStockCard = function(el, r, q, generated){
  q = q || {};
  var price = q.price !== undefined && q.price !== null ? q.price : r.price;
  var chg = q.chg !== undefined && q.chg !== null ? q.chg : r.chg;
  var pe = q.pe !== undefined && q.pe !== null ? q.pe : r.pe;
  var pb = q.pb !== undefined && q.pb !== null ? q.pb : r.pb;
  var lo = q.lo !== undefined && q.lo !== null ? q.lo : r.lo;
  var hi = q.hi !== undefined && q.hi !== null ? q.hi : r.hi;
  var dps = r.dps;
  var dy = (dps && price) ? dps / price * 100 : null;
  var pct = (lo && hi && hi > lo && price) ? (price - lo) / (hi - lo) * 100 : null;
  var tc = tierOf(dy);
  var sign = chg > 0 ? '+' : '';
  var gen = generated || '';
  var qtime = (q && q.price) ? ' · 行情 ' + new Date().toLocaleTimeString('zh-CN', {hour12: false}) : '';
  var tps = '';
  for (var i = 0; i < TARGETS.length; i++){
    var t = TARGETS[i];
    var p = dps ? dps / (t.rate / 100) : null;
    if (p === null){ tps += '<div class="tp"><div class="r">' + t.label + '</div><div class="p">—</div></div>'; continue; }
    var g = price ? (price - p) / price * 100 : null;
    var hit = g !== null && g <= 0;
    var gl = g === null ? '' : (hit ? '已低 ' + Math.abs(g).toFixed(1) + '%' : '还要跌 ' + g.toFixed(1) + '%');
    var now = (dy !== null && Math.abs(dy - t.rate) < 0.25) ? ' now' : '';
    tps += '<div class="tp' + now + '"><div class="r">' + t.label + '</div><div class="p">' + fmt(p) + '</div><span class="g' + (hit ? ' hit' : '') + '">' + gl + '</span></div>';
  }
  var divs = '';
  var dl = r.divs || [];
  for (var j = 0; j < dl.length; j++){
    var d = dl[j];
    divs += '<tr><td><span class="tag">' + (d.mmdd === '12-31' ? '年度' : '中期') + '</span>除息 ' + d.exd + '</td><td>' + d.per.toFixed(4) + ' 元/股</td></tr>';
  }
  if (!divs) divs = '<tr><td>暂无已实施的派息记录</td><td>—</td></tr>';
  // 距 5.0% 档偏离（客观换算，非建议）
  var p50 = dps ? dps / 0.05 : null;
  var dev50 = (p50 && price) ? (price - p50) / price * 100 : null;
  var devLine = dev50 === null ? '' :
    '当前股息率 ' + dy.toFixed(2) + '%（' + tc[1] + '）；现价较 5.0% 档参考价 ' + fmt(p50) + ' 元' +
    (dev50 <= 0 ? ' 已低 ' + Math.abs(dev50).toFixed(1) + '%' : ' 高 ' + dev50.toFixed(1) + '%') + '。';
  var sh = price ? Math.floor(100000 / price / 100) * 100 : null;
  var inc = (sh && dps) ? sh * dps : null;
  el.innerHTML = '<div class="dossier">' +
    '<div class="dhead"><div class="mark">息</div>' +
    '<div><div class="t1">个股股息档案</div><div class="t2">按最新数据实时计算 · 高股息看板</div></div>' +
    '<div class="ddate">数据更新<b>' + gen + '</b>' + qtime + '</div></div>' +
    '<div class="dbody">' +
    /* ---- 左列 · 行情账页（事实） ---- */
    '<div class="dcol dcol-a">' +
    '<div class="dhero"><div class="nm">' + r.name + '</div>' +
    '<div class="dmeta"><i>' + r.code + '</i><i>' + r.sector + '</i>市值 ' + (r.mktcap ? r.mktcap.toFixed(0) + ' 亿' : '—') + '</div>' +
    '<div class="dybox"><div class="k">股息率（现价 × FY' + (r.fy || '—') + ' 分红）</div>' +
    '<div class="v">' + (dy === null ? '—' : dy.toFixed(2) + '%') + '</div>' +
    '<span class="tier ' + tc[0] + '">' + tc[1] + '</span></div></div>' +
    '<div class="dsec"><h5>行情速览</h5><div class="qgrid">' +
    '<div class="q"><div class="k">现价</div><div class="v">' + fmt(price) + '<small>' + (chg === null || chg === undefined ? '' : sign + fmt(chg) + '%') + '</small></div></div>' +
    '<div class="q"><div class="k">每股分红</div><div class="v">' + fmt(dps, 3) + '<small>FY' + (r.fy || '—') + '</small></div></div>' +
    '<div class="q"><div class="k">PE / PB</div><div class="v">' + fmt(pe, 1) + '<small>/ ' + fmt(pb, 2) + '</small></div></div>' +
    '<div class="q"><div class="k">近 12 月派息</div><div class="v">' + (r.times12 || 0) + '<small>次</small></div></div>' +
    '</div></div>' +
    '<div class="dsec"><h5>近一年位置</h5><div class="poswrap">' +
    '<span class="k">' + (lo ? lo.toFixed(2) : '—') + '</span>' +
    '<div class="bar2"><i style="width:' + (pct === null ? 0 : Math.max(1.5, Math.min(100, pct))) + '%"></i></div>' +
    '<span class="k">' + (hi ? hi.toFixed(2) : '—') + '</span>' +
    '<span class="v">' + (pct === null ? '—' : pct.toFixed(0) + '%') + '</span></div></div>' +
    '<div class="dsec"><h5>分红明细（FY' + (r.fy || '—') + '，已实施）</h5><table>' + divs + '</table></div>' +
    '</div>' +
    /* ---- 右列 · 推演账页（换算） ---- */
    '<div class="dcol dcol-b">' +
    '<div class="dsec"><h5>股息率参考价</h5><div class="tiers">' + tps + '</div>' +
    '<div class="calc" style="margin-top:8px">' + devLine + '参考价 ＝ 每股分红 ÷ 目标股息率。金框为当前股息率所在档；绿色「已低」＝ 现价已低于该档参考价。</div></div>' +
    '<div class="dsec"><h5>历史股息率分位</h5><div class="calc" id="yhBox_' + r.code + '">计算中…</div>' +
    '<div class="calc note" style="margin-top:6px;font-size:10.5px">口径：近12个月已实施派息 ÷ 月末收盘（不复权），按月采样。分位为客观统计描述，不构成任何操作提示。</div></div>' +
    '<div class="dsec"><h5>技术位置（布林带 20,2）</h5><div class="calc" id="techBox_' + r.code + '">计算中…</div>' +
    '<div class="calc note" style="margin-top:6px;font-size:10.5px">周/月线由日线重采样；位置为客观状态描述，不构成任何操作提示。</div></div>' +
    '<div class="dsec"><h5>10 万元收息测算</h5><div class="calc">' +
    '按现价 ' + fmt(price) + ' 元可买入约 <b>' + (sh ? sh.toLocaleString() : '—') + '</b> 股，' +
    '以 FY' + (r.fy || '—') + ' 每股分红 ' + fmt(dps, 3) + ' 元计，一年预期到账 <b>' + (inc ? '¥ ' + inc.toFixed(0) : '—') + '</b>' +
    (inc ? '（' + (inc / 100000 * 100).toFixed(2) + '% 本金）' : '') + '。' +
    '<div class="note">按最近已完成财年的分红静态推算，分红政策可能变化，不代表未来收益。</div></div></div>' +
    '</div>' +
    '</div>' +
    '<div class="dfoot">数据来自公开行情与分红接口，打开/刷新时实时抓取现价，分红快照每日更新' + (gen ? '（' + gen + '）' : '') +
    '。所有价格档位仅为股息率换算的数学参考，<b>不构成任何投资建议</b>；历史分红不代表未来，投资决策需独立判断。<div style="margin-top:6px">board.zekes.top · 高股息研究账本</div></div><div style="text-align:center;margin-top:8px"><a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener" style="color:inherit">皖ICP备2026036657号</a></div></div>';
  fillTech(r.code);
  fillYield(r.code);
};

function fillTech(code){
  try {
    fetch('/api/stock/tech?code=' + encodeURIComponent(code))
      .then(function(res){ return res.ok ? res.json() : null; })
      .then(function(d){
        var box = document.getElementById('techBox_' + code);
        if (!box) return;
        if (!d || !d.ok){ box.textContent = (d && d.message) || '暂无数据'; return; }
        var rows = [];
        [['daily', '日线'], ['weekly', '周线'], ['monthly', '月线']].forEach(function(p){
          var b = d.bands[p[0]];
          if (b) rows.push('<b>' + p[1] + '</b>：' + b.label + '（带宽位置 ' + b.pb + '%）');
        });
        box.innerHTML = rows.join('　·　') + '<div class="note" style="font-size:10.5px;color:inherit;opacity:.75;margin-top:4px">数据截至 ' + d.asof + '</div>';
      })
      .catch(function(){ var box = document.getElementById('techBox_' + code); if (box) box.textContent = '暂无数据'; });
  } catch (e) {}
}

function fillYield(code){
  try {
    fetch('/api/stock/yieldhist?code=' + encodeURIComponent(code))
      .then(function(res){ return res.ok ? res.json() : null; })
      .then(function(d){
        var box = document.getElementById('yhBox_' + code);
        if (!box) return;
        if (!d || !d.ok){ box.textContent = (d && d.message) || '历史数据积累中'; return; }
        var yrs = d.years >= 9.5 ? '近10年' : '近' + d.years + '年';
        var hi = d.pct >= 80, lo = d.pct <= 20;
        var dotColor = hi ? 'var(--gold,#e8b04b)' : (lo ? '#7fd8a5' : '#fff');
        box.innerHTML =
          '<div style="font-size:12px;color:inherit">当前股息率 <b style="font-size:16px">' + d.cur.toFixed(2) + '%</b>，' + yrs + '分位 <b style="font-size:16px">' + d.pct + '%</b>（高于 ' + d.pct + '% 的历史月份）</div>' +
          '<div style="position:relative;height:26px;margin:10px 2px 2px">' +
            '<div style="position:absolute;top:9px;left:0;right:0;height:9px;border-radius:5px;background:linear-gradient(90deg,#7fd8a5,#e8b04b,#ff8f96)"></div>' +
            '<div style="position:absolute;top:4px;width:2px;height:19px;background:rgba(255,255,255,.55);left:25%"></div>' +
            '<div style="position:absolute;top:4px;width:2px;height:19px;background:rgba(255,255,255,.55);left:50%"></div>' +
            '<div style="position:absolute;top:4px;width:2px;height:19px;background:rgba(255,255,255,.55);left:75%"></div>' +
            '<div style="position:absolute;top:0;width:13px;height:13px;border-radius:50%;background:' + dotColor + ';border:2px solid rgba(0,0,0,.35);left:calc(' + Math.max(0, Math.min(97, d.pct)) + '% - 6px)"></div>' +
          '</div>' +
          '<div style="display:flex;justify-content:space-between;font-size:10.5px;color:inherit;opacity:.75;margin-top:2px">' +
            '<span>低（' + d.min + '%）</span><span>P25 ' + d.p25 + '%</span><span>中位 ' + d.median + '%</span><span>P75 ' + d.p75 + '%</span><span>高（' + d.max + '%）</span></div>' +
          '<div style="font-size:10.5px;color:inherit;opacity:.75;margin-top:6px">样本 ' + d.months + ' 个月（' + d.from + ' ~ ' + d.to + '）；股息率高通常对应价格低，反之亦然</div>';
      })
      .catch(function(){ var box = document.getElementById('yhBox_' + code); if (box) box.textContent = '历史数据积累中'; });
  } catch (e) {}
}
})();
