"""四维规则评分（后端口径 v2）——与 M1 焦点报告页（/report/600519/ §1）的规则同源。

三条设计原则：
1. 评分是「规则 × 输入值」的确定性计算，AI 不参与打分；每条规则的输入值随明细输出，可复算；
2. 外部接口数据带进程内 TTL 缓存；任一接口失败只影响对应子项/维度（标记 missing），不硬凑、不编造；
3. 资金流接口有字段错位前科（M1 勘误日志 #1），解析后必须过恒等式校验，不通过即弃用。

与 M1 页面口径的差异（全部因"后端可自动取得的数据"限制，规则明细里注明）：
- 负债子项：「零有息负债 +1」需资产负债表明细，改为「资产负债率 <15% 再 +1」阶梯；
- ROE 子项：年化 ROE(TTM) 需四季滚动，改用「最近年报加权 ROE」；
- 前瞻扣分：一致预期 EPS 需卖方预测接口，改用「近两年归母净利 CAGR」的历史口径。
"""
import json
import re
import time
from datetime import date

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from sqlalchemy.orm import Session

from .db import get_db
from .features import require_feature
from .models import Kline, User

score_router = APIRouter(prefix="/api/score", tags=["score"])

# ---------------------------------------------------------------- 派生计算（纯函数）

def _sma(xs: list[float], n: int) -> float | None:
    if len(xs) < n:
        return None
    return sum(xs[-n:]) / n


def _ema_series(xs: list[float], n: int) -> list[float]:
    if not xs:
        return []
    k = 2 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def derive_tech(closes: list[float], vols: list[float]) -> dict:
    """从收盘价/成交量序列派生技术指标。输入按日期升序，长度不足返回可算部分。"""
    out: dict = {}
    if len(closes) >= 5:
        out["MA5"] = _sma(closes, 5)
        out["MA10"] = _sma(closes, 10)
        out["MA20"] = _sma(closes, 20)
        out["MA60"] = _sma(closes, 60)
        out["MA120"] = _sma(closes, 120)
        out["MA250"] = _sma(closes, 250)
    # MACD(12,26,9)：DIF=EMA12-EMA26，DEA=EMA(DIF,9)，柱=2*(DIF-DEA)
    if len(closes) >= 35:
        e12, e26 = _ema_series(closes, 12), _ema_series(closes, 26)
        dif = [a - b for a, b in zip(e12, e26)]
        dea = _ema_series(dif, 9)
        out["DIF"], out["DEA"] = dif[-1], dea[-1]
        out["MACD_HIST"] = [round(2 * (d - e), 3) for d, e in zip(dif, dea)][-6:]
    # RSI14 Wilder 平滑
    if len(closes) >= 15:
        gains, losses = [], []
        for a, b in zip(closes, closes[1:]):
            ch = b - a
            gains.append(max(ch, 0))
            losses.append(max(-ch, 0))
        ag, al = sum(gains[:14]) / 14, sum(losses[:14]) / 14
        for g, l in zip(gains[14:], losses[14:]):
            ag = (ag * 13 + g) / 14
            al = (al * 13 + l) / 14
        out["RSI14"] = round(100 - 100 / (1 + ag / al), 2) if al > 0 else 100.0
    # BOLL(20,2) 带宽 = (上-下)/中；带宽分位取近 250 日
    if len(closes) >= 20:
        bws = []
        for i in range(20, len(closes) + 1):
            win = closes[i - 20:i]
            mid = sum(win) / 20
            sd = (sum((x - mid) ** 2 for x in win) / 20) ** 0.5
            bws.append((mid + 2 * sd - (mid - 2 * sd)) / mid * 100 if mid > 0 else 0)
        out["BOLL_BW"] = round(bws[-1], 2)
        ref = bws[-250:]
        out["BOLL_BW_PCT"] = round(sum(1 for b in ref if b <= bws[-1]) / len(ref) * 100, 1)
    # 量能：最新成交量在近 250 日的分位；20/60 日均量
    if len(vols) >= 20 and vols[-1] > 0:
        ref = vols[-250:]
        out["VOL_PCT"] = round(sum(1 for v in ref if v <= vols[-1]) / len(ref) * 100, 1)
        ma20v, ma60v = _sma(vols, 20), _sma(vols, 60)
        if ma20v and ma60v:
            out["MA20VOL"], out["MA60VOL"] = ma20v, ma60v
    return out


def score_tech(t: dict) -> dict:
    """技术形态 0-10：趋势5 + 动量3 + 量价2 + 波动扣分。子项缺数据按可得项计，status 标 partial。"""
    det, pts, missing = [], 0, 0

    def add(rule: str, ok: bool | None, inp: str, p: int = 1):
        nonlocal pts, missing
        if ok is None:
            missing += 1
            det.append({"rule": rule, "input": inp, "points": None})
        else:
            pts += p if ok else 0
            det.append({"rule": rule, "input": inp, "points": p if ok else 0})

    c = t.get
    for n in (20, 60, 120, 250):
        ma = c(f"MA{n}")
        add(f"收盘价站上 MA{n}", None if ma is None else c("_close", 0) > ma,
            f"MA{n}={_f(ma)}" + (f"，收盘={_f(c('_close'))}" if ma is not None else ""))
    add("MA5>MA10>MA20 多头排列",
        None if None in (c("MA5"), c("MA10"), c("MA20")) else c("MA5") > c("MA10") > c("MA20"),
        f"MA5={_f(c('MA5'))} / MA10={_f(c('MA10'))} / MA20={_f(c('MA20'))}")
    add("MACD DIF>DEA",
        None if None in (c("DIF"), c("DEA")) else c("DIF") > c("DEA"),
        f"DIF={_f(c('DIF'))} / DEA={_f(c('DEA'))}")
    rsi = c("RSI14")
    add("RSI14 处 40–70 中性区", None if rsi is None else 40 <= rsi <= 70, f"RSI14={_f(rsi)}")
    hist = c("MACD_HIST")
    if hist is not None and len(hist) >= 5:
        shrink = all(abs(hist[-1 - i]) < abs(hist[-2 - i]) for i in range(4))
        add("MACD 柱状线未连续 5 日收缩", not shrink, "近5日柱: " + "→".join(_f(h) for h in hist[-5:]))
    else:
        add("MACD 柱状线未连续 5 日收缩", None, "数据不足")
    vp = c("VOL_PCT")
    add("量能分位处 20–80% 常态区", None if vp is None else 20 <= vp <= 80, f"量能分位={_f(vp)}%")
    add("20 日均量 > 60 日均量",
        None if None in (c("MA20VOL"), c("MA60VOL")) else c("MA20VOL") > c("MA60VOL"),
        f"20日均量={_f(c('MA20VOL'))} / 60日均量={_f(c('MA60VOL'))}")
    bp = c("BOLL_BW_PCT")
    if bp is not None and bp > 90:
        pts -= 1
        det.append({"rule": "BOLL 带宽分位 >90%（极端扩张）", "input": f"带宽分位={_f(bp)}%", "points": -1})
    else:
        det.append({"rule": "BOLL 带宽分位 >90%（极端扩张）", "input": f"带宽分位={_f(bp)}%", "points": 0})
    return _dim(pts, det, missing)


def _f(x) -> str:
    return "—" if x is None else f"{x:,.2f}"


def _dim(points: int, details: list, missing_rules: int) -> dict:
    """维度打分。缺失子项按 0 分计、不折算放大（保守口径）；status 标 partial 提示复算方。"""
    return {
        "score": points,
        "max": 10,
        "status": "partial" if missing_rules else "ok",
        "missing_rules": missing_rules,
        "details": details,
        "note": "评分=可得子项得分（缺失子项按 0 分计，不折算放大）" if missing_rules else "",
    }


# ---------------------------------------------------------------- 数据抓取层（TTL 缓存）

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
      "Referer": "https://data.eastmoney.com/"}
_CACHE: dict[str, tuple[float, object]] = {}
_TTL = {"fin": 86400, "val": 7 * 86400, "flow": 3600, "margin": 86400, "holders": 86400, "kline": 3600}


def _cached(key: str, kind: str, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL[kind]:
        return hit[1]
    val = fn()
    if val is not None:
        _CACHE[key] = (time.time(), val)
    return val


def _dc_get(client: httpx.Client, params: dict, pages: int = 1) -> list[dict]:
    """东财 datacenter 通用分页 GET，返回合并后的 rows。"""
    rows, page = [], 1
    while page <= pages:
        p = dict(params, pageNumber=page, pageSize=500,
                 source="WEB", client="WEB")
        r = client.get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                       params=p, headers=UA, timeout=20)
        r.raise_for_status()
        d = r.json().get("result") or {}
        batch = d.get("data") or []
        rows.extend(batch)
        pages = min(pages, (int(d.get("pages") or 1)) if d.get("pages") else 1)
        if not batch or page >= int(d.get("pages") or 1):
            break
        page += 1
    return rows


def fetch_fin_main(code: str, client: httpx.Client | None = None) -> dict | None:
    """F10 主要指标（RPT_F10_FINANCE_MAINFINADATA，近 10 个报告期）→ 财务维度输入。"""
    def fn():
        own = client or httpx.Client()
        try:
            rows = _dc_get(own, {
                "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                "columns": "ALL",
                "filter": f'(SECUCODE="{_secu(code)}")',
                "sortColumns": "REPORT_DATE", "sortTypes": "-1",
            }, pages=1)
            if not rows:
                return None
            keep = rows[:14]
            return keep
        except Exception:
            return None
        finally:
            if own is not client:
                own.close()
    return _cached(f"fin:{code}", "fin", fn)


def _secu(code: str) -> str:
    return f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.SZ"


def _secid(code: str) -> str:
    return ("1." if code.startswith(("6", "5", "9")) else "0.") + code


def fetch_valuation_hist(code: str, client: httpx.Client | None = None) -> dict | None:
    """估值历史（RPT_VALUEANALYSIS_DET 全量分页）→ PE/PB 序列与分位。"""
    def fn():
        own = client or httpx.Client()
        try:
            rows = _dc_get(own, {
                "reportName": "RPT_VALUEANALYSIS_DET",
                "columns": "ALL",
                "filter": f'(SECUCODE="{_secu(code)}")',
                "sortColumns": "TRADE_DATE", "sortTypes": "1",
            }, pages=30)
            pe = [(r.get("TRADE_DATE", "")[:10], r.get("PE_TTM")) for r in rows if r.get("PE_TTM")]
            pb = [r.get("PB_MRQ") for r in rows if r.get("PB_MRQ")]
            if len(pe) < 250:
                return None
            return {"dates": len(pe), "from": pe[0][0], "to": pe[-1][0],
                    "pe": [v for _, v in pe], "pe_cur": pe[-1][1], "pb": pb, "pb_cur": pb[-1]}
        except Exception:
            return None
        finally:
            if own is not client:
                own.close()
    return _cached(f"val:{code}", "val", fn)


def _parse_fflow(kl: list[str]) -> list[dict]:
    """fflow daykline 行 → dict（单位：亿元）。push2 倒序 / push2his 正序，调用方负责排序。"""
    days = []
    for row in kl:
        p = row.split(",")
        if len(p) < 13:
            continue
        e8 = 1e8
        days.append({"date": p[0],
                     "main": float(p[1]) / e8, "small": float(p[2]) / e8, "mid": float(p[3]) / e8,
                     "big": float(p[4]) / e8, "super": float(p[5]) / e8,
                     "main_pct": float(p[6]), "close": float(p[11]) if p[11] else 0.0,
                     "chg": float(p[12]) if p[12] else 0.0})
    return days


def fetch_fund_flow(code: str, client: httpx.Client | None = None) -> dict | None:
    """逐日资金分层（push2 fflow，121 日）→ 恒等式校验后聚合。

    字段顺序坑（M1 勘误 #1）：fields2 按位映射 f51日期 f52主力 f53小单 f54中单 f55大单 f56超大单
    f57主力占比 f58小单占比 f59中单占比 f60大单占比 f61超大单占比 f62收盘 f63涨跌。
    校验不过整个数据弃用（返回 None），宁可缺维度不可错符号。
    """
    def fn():
        own = client or httpx.Client()
        try:
            # push2 主机不稳定（M1 经验：多主机故障转移）
            # push2his 系最稳（push2 系对 lmt>1 会间歇限流为仅当天 1 行，2026-11-12 实测）
            hosts = ["push2his.eastmoney.com", "48.push2his.eastmoney.com",
                     "82.push2his.eastmoney.com", "push2.eastmoney.com", "1.push2.eastmoney.com"]
            last_err = None
            r = None
            for host in hosts:
                try:
                    # push2 会掐 keep-alive 连接：一次性连接请求，不复用
                    with httpx.Client() as once:
                        r = once.get(
                            f"https://{host}/api/qt/stock/fflow/daykline/get",
                            params={"lmt": 121, "klt": 101, "secid": _secid(code),
                                    "fields1": "f1,f2,f3,f7",
                                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"},
                            headers=UA, timeout=20)
                    r.raise_for_status()
                    if (r.json().get("data") or {}).get("klines"):
                        break
                except Exception as e:
                    last_err = e
                    r = None
            if r is None:
                # httpx 会被 push2 按 TLS 指纹掐连接（本机代理网络实测），降级 requests（M1 已验证可通）
                import requests
                kl = None
                for host in hosts:
                    try:
                        rr = requests.get(f"https://{host}/api/qt/stock/fflow/daykline/get",
                                          params={"lmt": 121, "klt": 101, "secid": _secid(code),
                                                  "fields1": "f1,f2,f3,f7",
                                                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"},
                                          headers=UA, timeout=20)
                        kl = (rr.json().get("data") or {}).get("klines") or []
                        if kl:
                            break
                    except Exception as e2:
                        last_err = e2
                if not kl:
                    raise last_err or RuntimeError("fflow 全通道失败")
                days = _parse_fflow(kl)
            else:
                kl = (r.json().get("data") or {}).get("klines") or []
                days = _parse_fflow(kl)
            if len(days) < 60:
                return None
            days.sort(key=lambda d: d["date"])  # push2 倒序 / push2his 正序，统一为升序
            # 恒等式校验：① 主力=超大+大单 ② 主+中+小=0 ③ 收盘价/涨跌合理性
            bad = 0
            for d in days[-121:]:
                if abs(d["main"] - (d["super"] + d["big"])) > 0.05:
                    bad += 1
                if abs(d["main"] + d["mid"] + d["small"]) > 0.05:
                    bad += 1
                if d["close"] <= 0 or abs(d["chg"]) > 21:
                    bad += 1
            if bad > len(days) * 0.05:
                return None
            return days[-121:]
        except Exception:
            return None
        finally:
            if own is not client:
                own.close()
    return _cached(f"flow:{code}", "flow", fn)


def fetch_margin(code: str, client: httpx.Client | None = None) -> dict | None:
    """融资余额明细（RPTA_WEB_RZRQ_GGMX）→ 近区间净变化。"""
    def fn():
        own = client or httpx.Client()
        try:
            rows = _dc_get(own, {
                "reportName": "RPTA_WEB_RZRQ_GGMX",
                "columns": "ALL",
                "filter": f'(scode="{code}")',
                "sortColumns": "DATE", "sortTypes": "-1",
            }, pages=1)
            rows = [r for r in rows if r.get("RZYE")][:40]
            if len(rows) < 25:
                return None
            rows.reverse()  # 升序
            return {"dates": [r.get("DATE", "")[:10] for r in rows],
                    "rzye": [float(r["RZYE"]) for r in rows]}
        except Exception:
            return None
        finally:
            if own is not client:
                own.close()
    return _cached(f"margin:{code}", "margin", fn)


def fetch_holders(code: str, client: httpx.Client | None = None) -> dict | None:
    """股东户数（RPT_HOLDERNUM_DET）→ 最新环比与同比。"""
    def fn():
        own = client or httpx.Client()
        try:
            rows = _dc_get(own, {
                "reportName": "RPT_HOLDERNUM_DET",
                "columns": "ALL",
                "filter": f'(SECUCODE="{_secu(code)}")',
                "sortColumns": "END_DATE", "sortTypes": "-1",
            }, pages=1)
            rows = [r for r in rows if r.get("HOLDER_NUM")][:8]
            if len(rows) < 5:
                return None
            cur, prev, yoy_base = rows[0], rows[1], rows[4]
            def pct(a, b):
                return round((a - b) / b * 100, 2) if b else None
            return {"end_date": cur.get("END_DATE", "")[:10],
                    "num": int(cur["HOLDER_NUM"]),
                    "mom_pct": pct(float(cur["HOLDER_NUM"]), float(prev["HOLDER_NUM"])),
                    "yoy_pct": pct(float(cur["HOLDER_NUM"]), float(yoy_base["HOLDER_NUM"]))}
        except Exception:
            return None
        finally:
            if own is not client:
                own.close()
    return _cached(f"holders:{code}", "holders", fn)


def fetch_kline_pub(code: str, client: httpx.Client | None = None) -> tuple[list[float], list[float]] | None:
    """公开日 K（不复权收盘+量，万手→手口径不统一不影响分位/均量比较）→ 供无库环境的技术维度。"""
    def fn():
        own = client or httpx.Client()
        try:
            r = own.get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={"secid": _secid(code), "klt": 101, "fqt": 0,
                        "beg": "0", "end": "20500101",
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f53,f56"},
                headers=UA, timeout=20)
            r.raise_for_status()
            kl = (r.json().get("data") or {}).get("klines") or []
            closes = [float(p.split(",")[1]) for p in kl if len(p.split(",")) >= 2]
            vols = [float(p.split(",")[2]) for p in kl if len(p.split(",")) >= 3]
            return (closes, vols) if len(closes) > 260 else None
        except Exception:
            return None
        finally:
            if own is not client:
                own.close()
    return _cached(f"kline:{code}", "kline", fn)


# ---------------------------------------------------------------- 财务 / 估值 / 资金评分

def _q_net(rows: list[dict]) -> float | None:
    """最近报告期单季归母净利 = 本期累计 − 上期累计（需两期相邻年报期内）。"""
    def find(pred):
        for r in rows:
            if pred(r.get("REPORT_DATE", "")):
                return float(r["PARENTNETPROFIT"]) if r.get("PARENTNETPROFIT") else None
        return None
    d = rows[0].get("REPORT_DATE", "")
    if not re.match(r"\d{4}-09-30", d):
        return None
    q3, h1 = float(rows[0]["PARENTNETPROFIT"]), find(lambda x: x[:4] == d[:4] and x[5:7] == "06")
    if q3 is None or h1 is None:
        return None
    return q3 - h1


def score_finance(rows: list[dict] | None) -> dict:
    """财务质量 0-10：盈利3 + 现金流2 + 负债2 + 增长3。"""
    if not rows:
        return _missing_dim("F10 主要指标接口无数据")
    annual = next((r for r in rows if re.match(r"\d{4}-12-31", r.get("REPORT_DATE", ""))), rows[0])
    cur = rows[0]
    det, pts = [], 0

    def one(rule, val, thr, fmt="{:.2f}"):
        nonlocal pts
        if val is None:
            det.append({"rule": rule, "input": "缺数据", "points": None})
            return
        ok = val > thr
        pts += 1 if ok else 0
        det.append({"rule": rule, "input": fmt.format(val), "points": 1 if ok else 0})


    gml = (annual.get("XSMLL") or annual.get("MLL"))
    one("毛利率 > 60%（最近年报）", _num(gml), 60, "{:.2f}%")
    njl = (annual.get("XSJLL") or annual.get("JLL"))
    one("净利率 > 30%（最近年报）", _num(njl), 30, "{:.2f}%")
    one("加权 ROE > 25%（最近年报）", _num(annual.get("ROEJQ")), 25, "{:.2f}%")
    eps, mgjyxj = _num(cur.get("EPSJB")), _num(cur.get("MGJYXJJE"))
    if eps and mgjyxj is not None:
        ratio = mgjyxj / eps
        ok1, ok2 = ratio > 1.0, ratio > 1.2
        pts += (1 if ok1 else 0) + (1 if ok2 else 0)
        det.append({"rule": "经营现金流/净利润 > 1.0（最新报告期每股口径）", "input": f"{ratio:.3f}", "points": 1 if ok1 else 0})
        det.append({"rule": "经营现金流/净利润 > 1.2（最新报告期每股口径）", "input": f"{ratio:.3f}", "points": 1 if ok2 else 0})
    else:
        det.append({"rule": "经营现金流/净利润 > 1.0（最新报告期每股口径）", "input": "缺数据", "points": None})
        det.append({"rule": "经营现金流/净利润 > 1.2（最新报告期每股口径）", "input": "缺数据", "points": None})
    zcfzl = _num(cur.get("ZCFZL"))
    if zcfzl is not None:
        ok30, ok15 = zcfzl < 30, zcfzl < 15
        pts += (1 if ok30 else 0) + (1 if ok15 else 0)
        det.append({"rule": "资产负债率 < 30%（最新报告期）", "input": f"{zcfzl:.2f}%", "points": 1 if ok30 else 0})
        det.append({"rule": "资产负债率 < 15%（最新报告期）", "input": f"{zcfzl:.2f}%", "points": 1 if ok15 else 0})
    else:
        det.append({"rule": "资产负债率 < 30%（最新报告期）", "input": "缺数据", "points": None})
        det.append({"rule": "资产负债率 < 15%（最新报告期）", "input": "缺数据", "points": None})
    one("累计营收同比 > 0（最新报告期）", _num(cur.get("TOTALOPERATEREVETZ")), 0, "{:+.2f}%")
    one("累计归母净利同比 > 0（最新报告期）", _num(cur.get("PARENTNETPROFITTZ")), 0, "{:+.2f}%")
    q = _q_net(rows)
    if q is not None and len(rows) >= 5:
        # 单季同比：本期 Q3 单季 vs 去年 Q3 单季（累计相减）
        yq3 = _num(rows[4].get("PARENTNETPROFIT")) if len(rows) > 4 else None
        yh1 = next((_num(r.get("PARENTNETPROFIT")) for r in rows
                    if r.get("REPORT_DATE", "")[5:7] == "06" and r.get("REPORT_DATE", "")[:4] == rows[4].get("REPORT_DATE", "")[:4]), None)
        if yq3 is not None and yh1 is not None:
            yq = yq3 - yh1
            ok = q > yq
            pts += 1 if ok else 0
            det.append({"rule": "单季归母净利同比 > 0（Q3 累计相减）",
                        "input": f"本期 {q:.1f} vs 去年同期 {yq:.1f}", "points": 1 if ok else 0})
        else:
            det.append({"rule": "单季归母净利同比 > 0", "input": "缺数据", "points": None})
    else:
        det.append({"rule": "单季归母净利同比 > 0", "input": "非三季报或缺历史期", "points": None})
    return _dim(pts, det, sum(1 for d in det if d["points"] is None))


def _num(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _pct_rank(series: list[float], cur: float) -> float:
    return round(sum(1 for v in series if v <= cur) / len(series) * 100, 2)


def score_valuation(val: dict | None, dy_pct: float | None,
                    pe_cur: float | None, np_cagr2y: float | None) -> dict:
    """估值水位 0-10（分位越低越高分）：PE 档3 + PB 档3 + 股息率档3 + 增长估值扣分2。"""
    det, pts = [], 0
    pe = val.get("pe_cur") if val else pe_cur
    pe_pct = _pct_rank(val["pe"], val["pe_cur"]) if val else None
    pb = val.get("pb_cur") if val else None
    pb_pct = _pct_rank(val["pb"], val["pb_cur"]) if val else None

    def band(v, table, label, fmt):
        nonlocal pts
        if v is None:
            det.append({"rule": label, "input": "缺数据", "points": None})
            return
        for thr, p in table:
            if v < thr:
                pts += p
                det.append({"rule": label, "input": fmt.format(v), "points": p})
                return
        det.append({"rule": label, "input": fmt.format(v), "points": 0})

    band(pe_pct, [(10, 4), (25, 3), (50, 2), (75, 1)], "PE_TTM 历史分位（低→高分）", "{:.2f}%")
    band(pb_pct, [(10, 3), (30, 2), (60, 1)], "PB 历史分位（低→高分）", "{:.2f}%")
    band(dy_pct, [(2.0, 0), (3.0, 1), (3.5, 2), (1e9, 3)], "滚动股息率 ≥3.5%→3 / 3–3.5%→2 / 2–3%→1", "{:.2f}%")
    # 股息率档的严格口径：≥3.5→3, 3–3.5→2, 2–3→1, <2→0（band 的表写法见上行顺序）
    if pe is not None and np_cagr2y is not None:
        if pe > 18 and np_cagr2y < 8:
            pts -= 2
            det.append({"rule": "PE>18 且近两年净利 CAGR<8%（增长-估值不匹配）",
                        "input": f"PE={pe:.2f} / CAGR={np_cagr2y:+.2f}%", "points": -2})
        else:
            det.append({"rule": "PE>18 且近两年净利 CAGR<8%（增长-估值不匹配）",
                        "input": f"PE={pe:.2f} / CAGR={np_cagr2y:+.2f}%", "points": 0})
    else:
        det.append({"rule": "PE>18 且近两年净利 CAGR<8%", "input": "缺数据", "points": None})
    return _dim(pts, det, sum(1 for d in det if d["points"] is None))


def _np_cagr2y(fin_rows: list[dict] | None) -> float | None:
    """近两年归母净利 CAGR：最新年报 vs 两年前年报。"""
    if not fin_rows:
        return None
    annuals = [r for r in fin_rows if re.match(r"\d{4}-12-31", r.get("REPORT_DATE", ""))]
    annuals.sort(key=lambda r: r.get("REPORT_DATE", ""), reverse=True)
    if len(annuals) < 3:
        return None
    a0, a2 = _num(annuals[0].get("PARENTNETPROFIT")), _num(annuals[2].get("PARENTNETPROFIT"))
    if not a0 or not a2 or a2 <= 0:
        return None
    return ((a0 / a2) ** 0.5 - 1) * 100


def score_fund(flow: list[dict] | None, margin: dict | None, holders: dict | None) -> dict:
    """资金筹码 0-10：主力结构3 + 方向性2 + 杠杆1 + 筹码2。"""
    det, pts = [], 0
    if not flow:
        return _missing_dim("资金流接口无数据或恒等式校验未通过")

    def add(rule, ok, inp):
        nonlocal pts
        if ok is None:
            det.append({"rule": rule, "input": inp, "points": None})
        else:
            pts += 1 if ok else 0
            det.append({"rule": rule, "input": inp, "points": 1 if ok else 0})

    sup20 = sum(d["super"] for d in flow[-20:])
    big20 = sum(d["big"] for d in flow[-20:])
    main5 = sum(d["main"] for d in flow[-5:])
    if sup20 < -10:
        p = 0
    elif sup20 <= 0:
        p = 1
        pts += 1
    else:
        p = 2
        pts += 2
    det.append({"rule": "超大单近20日净额 >0→2 / −10~0亿→1 / <−10亿→0",
                "input": f"{sup20:+.2f}亿", "points": p})
    add("大单近 20 日净额 > 0", big20 > 0 if big20 is not None else None, f"{big20:+.2f}亿")
    add("主力近 5 日净额 > 0", main5 > 0 if main5 is not None else None, f"{main5:+.2f}亿")
    up = [d for d in flow[-20:] if d["chg"] > 0]
    dn = [d for d in flow[-20:] if d["chg"] <= 0]
    up_avg = sum(d["super"] for d in up) / len(up) if up else None
    dn_avg = sum(d["super"] for d in dn) / len(dn) if dn else None
    add("涨日超大单日均净额 > 0", (up_avg > 0) if up_avg is not None else None,
        f"{up_avg:+.2f}亿/日" if up_avg is not None else "数据不足")
    add("涨日净流出弱于跌日", (up_avg > dn_avg) if (up_avg is not None and dn_avg is not None) else None,
        f"涨日 {up_avg:+.2f} vs 跌日 {dn_avg:+.2f}" if up_avg is not None and dn_avg is not None else "数据不足")
    if margin and len(margin["rzye"]) >= 25:
        m_chg = (margin["rzye"][-1] - margin["rzye"][0]) / 1e8
        add("区间两融净流入", m_chg > 0, f"{margin['dates'][0]}以来 {m_chg:+.2f}亿")
    else:
        add("区间两融净流入", None, "两融数据不足")
    if holders and holders.get("mom_pct") is not None:
        add("股东户数环比下降", holders["mom_pct"] < 0, f"环比 {holders['mom_pct']:+.2f}%（{holders['end_date']}）")
    else:
        add("股东户数环比下降", None, "户数数据不足")
    if holders and holders.get("yoy_pct") is not None:
        add("股东户数同比下降", holders["yoy_pct"] < 0, f"同比 {holders['yoy_pct']:+.2f}%")
    else:
        add("股东户数同比下降", None, "户数数据不足")
    return _dim(pts, det, sum(1 for d in det if d["points"] is None))


def _missing_dim(reason: str) -> dict:
    return {"score": None, "max": 10, "status": "missing", "missing_rules": 0,
            "details": [], "note": reason}


# ---------------------------------------------------------------- 组装入口

def compute_scores(code: str, price: float | None, dps: float | None,
                   closes: list[float] | None = None, vols: list[float] | None = None,
                   client: httpx.Client | None = None) -> dict:
    """四维评分总入口。closes/vols 可由调用方从 Kline 表传入；缺省时抓公开日 K。"""
    out: dict = {"code": code, "asof": str(date.today()), "version": "score-v2"}
    own = client or httpx.Client()
    try:
        if closes is None or vols is None:
            kv = fetch_kline_pub(code, own)
            if kv:
                closes, vols = kv
        t = derive_tech(closes or [], vols or [])
        t["_close"] = closes[-1] if closes else None
        out["技术形态"] = score_tech(t) if closes and len(closes) > 60 else _missing_dim("K线数据不足")

        fin_rows = fetch_fin_main(code, own)
        out["财务质量"] = score_finance(fin_rows)

        val = fetch_valuation_hist(code, own)
        dy = round(dps / price * 100, 2) if (dps and price) else None
        out["估值水位"] = score_valuation(val, dy, None, _np_cagr2y(fin_rows))

        flow = fetch_fund_flow(code, own)
        out["资金筹码"] = score_fund(flow, fetch_margin(code, own), fetch_holders(code, own))
        return out
    finally:
        if own is not client:
            own.close()


def scores_compact(res: dict) -> dict:
    """给 AI 数据包用的紧凑版：每维一行分数 + top 明细。"""
    def one(k):
        d = res.get(k) or {}
        return {"score": d.get("score"), "max": d.get("max"), "status": d.get("status"),
                "rule": "；".join(
                    f"{x['rule']}[{x['input']}]={x['points']}" for x in (d.get("details") or [])[:12])}
    return {k: one(k) for k in ("技术形态", "财务质量", "估值水位", "资金筹码")}


# ---------------------------------------------------------------- 公开接口（纯计算零 token）

def _load_universe_row(code: str) -> dict | None:
    """site_data.json → 标的行（dps/price 兜底）。"""
    import os
    path = os.environ.get("SITE_DATA_PATH", "/srv/site_data.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        for r in raw.get("rows", []):
            if r.get("code") == code:
                return r
    except Exception:
        pass
    return None


@score_router.get("/{code}")
def get_score(code: str, user: User | None = Depends(require_feature("board.score")),
              db: Session = Depends(get_db)):
    """四维规则评分（公开层）：纯确定性计算，不调用任何模型，接口失败只降级对应维度。"""
    if not (code.isdigit() and len(code) == 6):
        raise HTTPException(400, "股票代码格式不正确")
    r = _load_universe_row(code)
    price, dps = None, None
    if r:
        price, dps = r.get("price"), r.get("dps")
    # 现价尽量取实时（腾讯行情走 alerts.fetch_quotes，失败用快照价）
    try:
        from .alerts import fetch_quotes
        q = fetch_quotes([code]).get(code, {})
        price = q.get("price") or price
    except Exception:
        pass
    rows = db.scalars(select(Kline).where(Kline.code == code).order_by(Kline.date)).all()
    closes = [x.raw for x in rows if x.raw and x.raw > 0]
    vols = [x.volume or 0 for x in rows if x.raw and x.raw > 0]
    # K 线库由定时任务刷新，可能滞后；超过 4 天（跨周末）就回退公开接口（带 1h TTL 缓存）
    from datetime import timedelta
    stale = (not closes) or (rows[-1].date < str(date.today() - timedelta(days=4)))
    if stale:
        kv = fetch_kline_pub(code)
        if kv:
            closes, vols = kv
    res = compute_scores(code, price, dps, closes=closes or None, vols=vols or None)
    return {"ok": True, **res}
