/* 个股股息档案卡组件 —— 在任意容器内直接渲染（无 iframe）。
   用法：renderStockCard(el, row, quote, generated)
   el: 容器 DOM；row: site_data 里的一行；quote: 腾讯行情（可空）；generated: 数据日期字符串 */
(function(){
'use strict';

if (!document.getElementById('scard-style')){
  var st = document.createElement('style');
  st.id = 'scard-style';
  st.textContent = [
    '.scard{--red-deep:#6e0f12;--red:#8f161b;--red-hi:#a51e24;--gold:#e8b04b;',
    '--gold-soft:rgba(232,176,75,.18);--cream:#fdf6ec;--cream2:rgba(253,246,236,.72);',
    '--cream3:rgba(253,246,236,.45);--panel:rgba(255,255,255,.07);--panel-line:rgba(255,255,255,.14);',
    '--up2:#ff8f96;--down2:#7fd8a5;',
    'border-radius:16px;overflow:hidden;color:var(--cream);font-size:13px;line-height:1.5;',
    'background:linear-gradient(165deg,var(--red-hi) 0%,var(--red) 45%,var(--red-deep) 100%);',
    'box-shadow:inset 0 1px 0 rgba(255,255,255,.08);font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}',
    '.scard *{box-sizing:border-box;margin:0;padding:0}',
    '.scard .hd{display:flex;align-items:center;gap:10px;padding:14px 18px 11px;border-bottom:1px solid var(--panel-line)}',
    '.scard .mark{width:34px;height:34px;border-radius:10px;flex:none;background:linear-gradient(135deg,var(--gold),#c9922f);color:#4a2408;display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:800}',
    '.scard .hd .t1{font-size:14px;font-weight:700;letter-spacing:1px}',
    '.scard .hd .t2{font-size:11px;color:var(--cream3);margin-top:1px}',
    '.scard .hd .date{margin-left:auto;text-align:right;font-size:11px;color:var(--cream3)}',
    '.scard .hd .date b{display:block;color:var(--cream2);font-weight:600;font-size:12px}',
    '.scard .hero{display:flex;align-items:center;gap:18px;padding:18px 18px 16px;flex-wrap:wrap}',
    '.scard .hero .nm{font-size:28px;font-weight:800;letter-spacing:1px;line-height:1.2}',
    '.scard .hero .meta{font-size:12px;color:var(--cream3);margin-top:4px}',
    '.scard .hero .meta i{font-style:normal;border:1px solid var(--panel-line);border-radius:9px;padding:1px 8px;margin-right:6px;color:var(--cream2)}',
    '.scard .dybox{margin-left:auto;text-align:right}',
    '.scard .dybox .k{font-size:11px;color:var(--cream3)}',
    '.scard .dybox .v{font-size:36px;font-weight:800;color:var(--gold);line-height:1.05;font-variant-numeric:tabular-nums;text-shadow:0 2px 14px rgba(232,176,75,.35)}',
    '.scard .tier{display:inline-block;margin-top:5px;font-size:11px;font-weight:600;border-radius:9px;padding:2px 10px}',
    '.scard .t-high{background:var(--gold);color:#4a2408}',
    '.scard .t-mid{background:rgba(255,255,255,.16);color:var(--cream)}',
    '.scard .t-low{background:rgba(255,255,255,.09);color:var(--cream2)}',
    '.scard .pxrow{display:flex;gap:8px;padding:0 18px 14px;flex-wrap:wrap}',
    '.scard .px{flex:1;min-width:130px;background:var(--panel);border:1px solid var(--panel-line);border-radius:12px;padding:9px 13px}',
    '.scard .px .k{font-size:11px;color:var(--cream3)}',
    '.scard .px .v{font-size:18px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}',
    '.scard .px .v small{font-size:11px;font-weight:400;color:var(--cream3);margin-left:2px}',
    '.scard .sec{padding:2px 18px 6px}',
    '.scard .sec h4{font-size:12px;color:var(--gold);letter-spacing:2px;margin:11px 0 7px;font-weight:700}',
    '.scard .tiers{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}',
    '.scard .tp{background:var(--panel);border:1px solid var(--panel-line);border-radius:10px;padding:7px 9px;text-align:center}',
    '.scard .tp .r{font-size:12px;font-weight:700;color:var(--gold)}',
    '.scard .tp .p{font-size:14px;font-weight:800;margin-top:2px;font-variant-numeric:tabular-nums}',
    '.scard .tp .g{font-size:10.5px;margin-top:3px;color:var(--cream3);border-radius:7px;padding:1px 6px;display:inline-block}',
    '.scard .tp .g.hit{background:var(--gold-soft);color:var(--gold);font-weight:700}',
    '.scard .tp.now{outline:2px solid var(--gold);outline-offset:-2px}',
    '.scard .poswrap{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--panel-line);border-radius:12px;padding:9px 13px}',
    '.scard .poswrap .k{font-size:11px;color:var(--cream3);flex:none}',
    '.scard .bar2{flex:1;height:7px;border-radius:4px;background:rgba(0,0,0,.28);position:relative}',
    '.scard .bar2 i{position:absolute;top:0;bottom:0;left:0;border-radius:4px;background:linear-gradient(90deg,#7fd8a5,#e8b04b,#ff8f96)}',
    '.scard .poswrap .v{font-size:13px;font-weight:700;flex:none;font-variant-numeric:tabular-nums}',
    '.scard table{width:100%;border-collapse:collapse}',
    '.scard td{padding:5px 4px;font-size:12px;border-bottom:1px dashed rgba(255,255,255,.09)}',
    '.scard tr:last-child td{border-bottom:none}',
    '.scard td:last-child{text-align:right;font-variant-numeric:tabular-nums;color:var(--cream)}',
    '.scard td:first-child{color:var(--cream2)}',
    '.scard td .tag{font-size:10px;border:1px solid var(--panel-line);border-radius:7px;padding:0 6px;margin-right:6px;color:var(--cream3)}',
    '.scard .calc{background:var(--panel);border:1px solid var(--panel-line);border-radius:12px;padding:11px 14px;font-size:12px;color:var(--cream2);line-height:1.8}',
    '.scard .calc b{color:var(--gold);font-weight:700;font-size:14px}',
    '.scard .calc .note{font-size:10.5px;color:var(--cream3)}',
    '.scard .ft{padding:12px 18px 16px;font-size:10.5px;color:var(--cream3);line-height:1.7;border-top:1px solid var(--panel-line);margin-top:11px}',
    '@media (max-width:560px){.scard .hero .nm{font-size:22px}.scard .dybox .v{font-size:28px}.scard .tiers{grid-template-columns:repeat(2,1fr)}.scard .pxrow .px{min-width:calc(50% - 4px)}}'
  ].join('');
  document.head.appendChild(st);
}

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
    if (p === null){
      tps += '<div class="tp"><div class="r">' + t.label + '</div><div class="p">—</div></div>';
      continue;
    }
    var g = price ? (price - p) / price * 100 : null;
    var hit = g !== null && g <= 0;
    var gl = g === null ? '' : (hit ? '已低 ' + Math.abs(g).toFixed(1) + '%' : '还要跌 ' + g.toFixed(1) + '%');
    var now = (dy !== null && Math.abs(dy - t.rate) < 0.25) ? ' now' : '';
    tps += '<div class="tp' + now + '"><div class="r">' + t.label + '</div><div class="p">' + fmt(p) +
           '</div><span class="g' + (hit ? ' hit' : '') + '">' + gl + '</span></div>';
  }

  var divs = '';
  var dl = r.divs || [];
  for (var j = 0; j < dl.length; j++){
    var d = dl[j];
    divs += '<tr><td><span class="tag">' + (d.mmdd === '12-31' ? '年度' : '中期') + '</span>除息 ' + d.exd +
            '</td><td>' + d.per.toFixed(4) + ' 元/股</td></tr>';
  }
  if (!divs) divs = '<tr><td>暂无已实施的派息记录</td><td>—</td></tr>';

  var sh = price ? Math.floor(100000 / price / 100) * 100 : null;
  var inc = (sh && dps) ? sh * dps : null;

  el.innerHTML =
    '<div class="hd"><div class="mark">息</div>' +
    '<div><div class="t1">个股股息档案</div><div class="t2">按最新数据实时计算 · 高股息看板</div></div>' +
    '<div class="date">数据更新<b>' + gen + '</b>' + qtime + '</div></div>' +
    '<div class="hero"><div><div class="nm">' + r.name + '</div>' +
    '<div class="meta"><i>' + r.code + '</i><i>' + r.sector + '</i>市值 ' + (r.mktcap ? r.mktcap.toFixed(0) + ' 亿' : '—') + '</div></div>' +
    '<div class="dybox"><div class="k">股息率（现价 × FY' + (r.fy || '—') + ' 分红）</div>' +
    '<div class="v">' + (dy === null ? '—' : dy.toFixed(2) + '%') + '</div>' +
    '<span class="tier ' + tc[0] + '">' + tc[1] + '</span></div></div>' +
    '<div class="pxrow">' +
    '<div class="px"><div class="k">现价</div><div class="v">' + fmt(price) + '<small>' + (chg === null || chg === undefined ? '' : sign + fmt(chg) + '%') + '</small></div></div>' +
    '<div class="px"><div class="k">每股分红</div><div class="v">' + fmt(dps, 3) + '<small>FY' + (r.fy || '—') + '</small></div></div>' +
    '<div class="px"><div class="k">PE / PB</div><div class="v">' + fmt(pe, 1) + '<small>/ ' + fmt(pb, 2) + '</small></div></div>' +
    '<div class="px"><div class="k">近 12 月派息</div><div class="v">' + (r.times12 || 0) + '<small>次</small></div></div></div>' +
    '<div class="sec"><h4>股息率参考价</h4><div class="tiers">' + tps + '</div>' +
    '<div class="calc" style="margin-top:8px">参考价 ＝ 每股分红 ÷ 目标股息率。金框为当前股息率所在档；绿色「已低」＝ 现价已低于该档参考价。</div></div>' +
    '<div class="sec"><h4>近一年位置</h4><div class="poswrap">' +
    '<span class="k">' + (lo ? lo.toFixed(2) : '—') + '</span>' +
    '<div class="bar2"><i style="width:' + (pct === null ? 0 : Math.max(1.5, Math.min(100, pct))) + '%"></i></div>' +
    '<span class="k">' + (hi ? hi.toFixed(2) : '—') + '</span>' +
    '<span class="v">' + (pct === null ? '—' : pct.toFixed(0) + '%') + '</span></div></div>' +
    '<div class="sec"><h4>分红明细（FY' + (r.fy || '—') + '，已实施）</h4><table>' + divs + '</table></div>' +
    '<div class="sec"><h4>10 万元收息测算</h4><div class="calc">' +
    '按现价 ' + fmt(price) + ' 元可买入约 <b>' + (sh ? sh.toLocaleString() : '—') + '</b> 股，' +
    '以 FY' + (r.fy || '—') + ' 每股分红 ' + fmt(dps, 3) + ' 元计，一年预期到账 <b>' + (inc ? '¥ ' + inc.toFixed(0) : '—') + '</b>' +
    (inc ? '（' + (inc / 100000 * 100).toFixed(2) + '% 本金）' : '') + '。' +
    '<div class="note">按最近已完成财年的分红静态推算，分红政策可能变化，不代表未来收益。</div></div></div>' +
    '<div class="ft">数据来自公开行情与分红接口，打开/刷新时实时抓取现价，分红快照每日更新' + (gen ? '（' + gen + '）' : '') +
    '。所有价格档位仅为股息率换算的数学参考，<b>不构成任何投资建议</b>；历史分红不代表未来，投资决策需独立判断。</div>';
};
})();
