# -*- coding: utf-8 -*-
"""高股息看板数据层 —— 抓行情 + 分红，输出 site_data.js / site_data.json

用法：  python3 tools/fetch_data.py     （在 web/ 目录下，或任意位置均可）

标的来源（两级，自动合并）：
  1. web/universe.json     本仓库自带 {代码: 板块} 映射，单独 clone 也能跑
  2. 上级项目目录（可选）   存在则扫描板块文件 + NNNNNN-*.html 卡片文件，
                           新发布的标的自动进入下一次更新，并回写 universe.json

数据源（免费、免密钥、无需服务端）：
  行情 / PE / PB / 52周高低   qt.gtimg.cn       腾讯，批量 60 只一次，GBK
  分红明细                    datacenter-web.eastmoney.com   UTF-8

口径（沿用吃息研究所既有规则）：
  DPS   = 最近一个「年度分红已实施」财年的（年度 + 中期）每股派息合计
  dy    = DPS / 现价
  参考价 = DPS / 目标股息率
  区间位置 = (现价 - 近一年最低) / (近一年最高 - 近一年最低)

踩坑记录（改代码前先读）：
  1. 东财 push2 行情接口会限流（批量跑一轮就可能被封），故行情一律走腾讯。
  2. 编码必须分开：腾讯行情 GBK、东财接口 UTF-8。统一按一个编码解会把中文打乱，
     JSON 解析随之失败（实测 118 个标的分红全部落空）。
  3. 腾讯 web.ifzq.gtimg.cn 的日线接口已被 WAF 拦（返回 501 页），
     52 周高低改用行情返回里的 [47]/[48] 字段，省掉 118 次请求。
"""
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

WEB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(WEB_ROOT)

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

for k in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
    os.environ.pop(k, None)


def curl(url, referer=None, tries=3, enc='utf-8'):
    for _ in range(tries):
        cmd = ['curl', '-s', '--noproxy', '*', '-m', '25', '-A', UA]
        if referer:
            cmd += ['-e', referer]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True)
            if r.stdout and r.stdout.strip():
                return r.stdout.decode(enc, 'ignore')
        except Exception:
            pass
    return ''


# ---------------------------------------------------------------- 标的与板块
# 上级项目目录里的板块归类文件（存在才读，单独 clone 时跳过）
BOARD_FILES = {
    '_bank_ui.json': '银行',
    '_appl.json': '家电',
    '_baijiu.json': '白酒',
    '_baijiu_scan.json': '白酒',
}

# 兜底手工归档（universe.json 已覆盖大部分，这里只留历史映射做保险）
MANUAL = {
    '000423': '医药',     '000538': '医药',
    '000429': '交运基建',  '600377': '交运基建',  '601006': '交运基建',
    '000543': '能源电力', '002128': '能源电力', '600011': '能源电力', '600023': '能源电力',
    '600028': '能源电力', '600188': '能源电力', '600642': '能源电力', '600795': '能源电力',
    '600938': '能源电力', '601088': '能源电力', '601225': '能源电力', '601857': '能源电力',
    '000568': '白酒',     '600519': '白酒',
    '000807': '有色化工', '002233': '有色化工', '600019': '有色化工', '600096': '有色化工',
    '600309': '有色化工', '601600': '有色化工', '601899': '有色化工',
    '000895': '食品饮料',
    '002415': '电子',     '300750': '电力设备',
    '600066': '汽车',     '600741': '汽车',
    '600177': '消费其他', '600398': '消费其他', '002867': '消费其他',
    '601928': '消费其他', '603365': '消费其他',
    '600941': '通信',
    '601318': '非银金融', '601336': '非银金融',
    '601668': '建筑基建',
    # 多次派息标的（近12个月派息4次扫描，2026-10 国庆Day3 口径）
    '300770': '传媒',     '002555': '传媒',     '002027': '传媒',
    '603165': '环保',     '002223': '医药',     '301023': '电力设备',
    '002532': '有色化工', '600742': '汽车',     '000905': '交运基建',
    '300741': '消费其他',
}

SECTOR_ORDER = ['银行', '家电', '白酒', '食品饮料', '能源电力', '医药', '交运基建',
                '有色化工', '非银金融', '建筑基建', '消费其他', '汽车', '电子',
                '电力设备', '通信', '传媒', '环保', '其他']

SKIP_PREFIX = ('51', '15', '56', '58', '16', '18', '50', '52')


def load_universe():
    p = os.path.join(WEB_ROOT, 'universe.json')
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding='utf-8')).get('map') or {}
        except Exception:
            return {}
    return {}


def load_board_map():
    m = {}
    for f, sec in BOARD_FILES.items():
        p = os.path.join(PROJECT_ROOT, f)
        if not os.path.exists(p):
            continue
        txt = open(p, encoding='utf-8', errors='ignore').read()
        for c in re.findall(r'"(?:code|f12)"\s*:\s*"?([036]\d{5})"?', txt):
            m[c] = sec
    return m


def scan_project_codes():
    """上级目录存在时，从卡片文件名里捞新增标的；不存在返回空。"""
    codes = set()
    if os.path.isdir(PROJECT_ROOT):
        for f in os.listdir(PROJECT_ROOT):
            m = re.match(r'^(\d{6})[-_]', f)
            if m:
                codes.add(m.group(1))
    return codes


def collect_codes(universe):
    codes = set(universe.keys()) | scan_project_codes()
    return [c for c in sorted(codes)
            if c[0] in '036' and c[:2] not in SKIP_PREFIX]


def sector_of(code, universe, board_map):
    return universe.get(code) or board_map.get(code) or MANUAL.get(code) or '其他'


# ---------------------------------------------------------------- 行情（腾讯，GBK）
# 字段位置实测确认：[3]现价 [32]涨跌幅 [39]PE [45]总市值(亿) [46]PB [47]52周高 [48]52周低
IDX = {'name': 1, 'code': 2, 'price': 3, 'chg': 32, 'pe': 39,
       'mktcap': 45, 'pb': 46, 'hi': 47, 'lo': 48}


def fetch_quotes(codes):
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        pre = [('sh' + c if c[0] == '6' else 'sz' + c) for c in batch]
        txt = curl('https://qt.gtimg.cn/q=' + ','.join(pre),
                   referer='https://gu.qq.com/', enc='gbk')
        for line in txt.strip().split('\n'):
            if '="' not in line:
                continue
            p = line.split('="')[1].rstrip('";\r\n').split('~')
            if len(p) < 49:
                continue

            def f(k):
                try:
                    return float(p[IDX[k]])
                except Exception:
                    return None

            price = f('price')
            if not price:
                continue
            # 除息/除权日腾讯会给名字加 XD/XR/DR 前缀，去掉保持名称干净
            nm = re.sub(r'^(XD|XR|DR)(?=[\u4e00-\u9fa5A-Za-z])', '', p[IDX['name']].replace(' ', ''))
            out[p[IDX['code']]] = {
                'name': nm,
                'price': price,
                'chg': f('chg'),
                'pe': f('pe') or None,
                'pb': f('pb') or None,
                'mktcap': f('mktcap') or None,
                'hi': f('hi') or None,
                'lo': f('lo') or None,
                'ts': p[30],
            }
    return out


# ---------------------------------------------------------------- 分红（东财，UTF-8）
def fetch_div(code):
    url = ('https://datacenter-web.eastmoney.com/api/data/v1/get?'
           'reportName=RPT_SHAREBONUS_DET&columns=ALL'
           '&filter=(SECURITY_CODE%3D%22' + code + '%22)'
           '&pageSize=80&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB')
    try:
        return ((json.loads(curl(url)).get('result') or {}).get('data')) or []
    except Exception:
        return []


def calc_dps(rows):
    """返回 (dps, fy, items, bvps)"""
    byfy, bvps = {}, None
    for r in rows:
        if bvps is None and r.get('BVPS'):
            try:
                bvps = float(r['BVPS'])
            except Exception:
                pass
        if '实施' not in (r.get('ASSIGN_PROGRESS') or ''):
            continue
        exd = (r.get('EX_DIVIDEND_DATE') or '')[:10]
        rd = (r.get('REPORT_DATE') or '')[:10]
        amt = r.get('PRETAX_BONUS_RMB')
        if not exd or not rd or amt in (None, ''):
            continue
        try:
            amt = float(amt)
        except Exception:
            continue
        if amt <= 0:
            continue
        byfy.setdefault(rd[:4], []).append(
            {'mmdd': rd[5:10], 'per': round(amt / 10, 6), 'exd': exd})

    if not byfy:
        return None, None, [], bvps
    pick = None
    for fy in sorted(byfy, reverse=True):
        if any(x['mmdd'] == '12-31' for x in byfy[fy]):
            pick = fy
            break
    if pick is None:
        pick = sorted(byfy, reverse=True)[0]
    items = sorted(byfy[pick], key=lambda x: x['mmdd'])
    return round(sum(x['per'] for x in items), 6), pick, items, bvps


def calc_times12(rows):
    """近 12 个月派息次数：已实施、除息日落在最近 365 天、按除息日去重。"""
    lo = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    exds = set()
    for r in rows:
        if '实施' not in (r.get('ASSIGN_PROGRESS') or ''):
            continue
        exd = (r.get('EX_DIVIDEND_DATE') or '')[:10]
        amt = r.get('PRETAX_BONUS_RMB')
        if not exd or amt in (None, ''):
            continue
        try:
            if float(amt) <= 0:
                continue
        except Exception:
            continue
        if exd >= lo:
            exds.add(exd)
    return len(exds)


# ---------------------------------------------------------------- 主流程
def main():
    universe = load_universe()
    board_map = load_board_map()
    codes = collect_codes(universe)
    print('候选代码 %d 个（universe.json %d / 上级目录扫描新增 %d）' % (
        len(codes), len(universe), max(0, len(codes) - len(universe))))

    quotes = fetch_quotes(codes)
    valid = sorted(quotes.keys())
    print('行情验证通过 %d 个' % len(valid))
    dropped = [c for c in codes if c not in quotes]
    if dropped:
        print('  剔除无效代码：', ' '.join(dropped))

    with ThreadPoolExecutor(max_workers=8) as ex:
        divs = dict(zip(valid, ex.map(fetch_div, valid)))

    rows, warn = [], []
    for c in valid:
        q = quotes[c]
        dps, fy, items, bvps = calc_dps(divs.get(c) or [])
        times12 = calc_times12(divs.get(c) or [])
        price = q['price']
        dy = round(dps / price * 100, 2) if (dps and price) else None
        pb = q['pb'] or (round(price / bvps, 2) if (bvps and bvps > 0) else None)
        lo, hi = q['lo'], q['hi']
        pct = round((price - lo) / (hi - lo) * 100, 1) if (lo and hi and hi > lo) else None

        if dy and dy > 10:
            warn.append('%s(%s) 股息率 %s%% 超常识上限，必须核' % (q['name'], c, dy))

        rows.append({
            'code': c, 'name': q['name'], 'sector': sector_of(c, universe, board_map),
            'price': price, 'chg': q['chg'], 'pe': q['pe'], 'pb': pb,
            'mktcap': q['mktcap'], 'dps': dps, 'fy': fy, 'dy': dy,
            'divs': items, 'lo': lo, 'hi': hi, 'pct': pct, 'times12': times12,
        })

    rows.sort(key=lambda r: (SECTOR_ORDER.index(r['sector'])
                             if r['sector'] in SECTOR_ORDER else 99,
                             -(r['dy'] or 0)))
    sectors = []
    for r in rows:
        if r['sector'] not in sectors:
            sectors.append(r['sector'])

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    payload = {'generated': now, 'sectors': sectors, 'rows': rows}
    txt = json.dumps(payload, ensure_ascii=False, indent=1)
    with open(os.path.join(WEB_ROOT, 'site_data.json'), 'w', encoding='utf-8') as f:
        f.write(txt)
    # 同目录的 js 版本供页面用 <script src> 直接引入（file:// 下也能加载）
    with open(os.path.join(WEB_ROOT, 'site_data.js'), 'w', encoding='utf-8') as f:
        f.write('window.SITE_DATA = ' + txt + ';\n')

    # 回写 universe.json：把本次实际拿到的标的与板块固化，单独 clone 也能复现
    newmap = {r['code']: r['sector'] for r in rows}
    if newmap != universe:
        json.dump({'generated': now, 'map': newmap},
                  open(os.path.join(WEB_ROOT, 'universe.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        print('universe.json 已更新：%d 个标的' % len(newmap))

    print('已写出 site_data.js：%d 个标的，%d 个板块' % (len(rows), len(sectors)))
    for s in sectors:
        print('  %-6s %d' % (s, sum(1 for r in rows if r['sector'] == s)))
    nodiv = ['%s(%s)' % (r['name'], r['code']) for r in rows if not r['dps']]
    print('拿到分红数据 %d / %d' % (len(rows) - len(nodiv), len(rows)))
    if nodiv:
        print('  缺分红：', ' '.join(nodiv[:12]))
    if warn:
        print('必须复核：')
        for w in warn:
            print('  ', w)


if __name__ == '__main__':
    main()
