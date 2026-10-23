"""策略实验室：K线/分红缓存 + 纯 Python 日线回测引擎 + 红利主题预设策略。

数据口径：
  行情  东财 push2his 日线，fqt=1 前复权（总收益口径）与 fqt=0 不复权（价格口径）各存一份
  分红  东财 datacenter RPT_SHAREBONUS_DET，已实施、按除息日，PRETAX_BONUS_RMB/10=每股派息
  历史股息率 = 过去 365 天每股派息合计（除息日口径）÷ 当日不复权收盘

引擎原则：
  再平衡时点用当期及之前的数据选股（无前视）；前复权序列算总收益，
  同一组合再用不复权序列跑一遍，两者差 = 分红再投入贡献（股息分解）。
  费用按换手率近似：fee = Σ|Δw| × fee_bps/10000。
"""
import json
import math
import os
import time
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .features import require_admin, require_feature
from .models import Kline, StockDividend, User

router = APIRouter(prefix="/api/lab", tags=["lab"])

SITE_DATA_PATH = os.environ.get("SITE_DATA_PATH", "/srv/site_data.json")
DATA_BEG = "20150101"
FEE_BPS = 10          # 单边费率（佣金+印花税近似）
TRADING_DAYS = 244
RISK_FREE = 0.02

BIG_BANKS = ["601398", "601939", "601288", "600036", "601328"]
ETF_DIVIDEND = "510880"   # 红利ETF
ETF_HS300 = "510300"      # 沪深300ETF
IDX_HS300 = "000300"      # 沪深300指数（基准曲线）


# ---------------------------------------------------------------- 数据拉取
def _secid(code: str) -> str:
    if code == IDX_HS300:
        return "1." + code  # 沪深300 是上证指数段，不能按股票规则映射
    return ("1." if code.startswith(("6", "5")) else "0.") + code


def _tx_symbol(code: str) -> str:
    if code == IDX_HS300:
        return "sh000300"  # 沪深300 指数在上证段
    return ("sh" if code.startswith(("6", "5")) else "sz") + code


def _em_kline(client: httpx.Client, code: str, fqt: int) -> list[dict]:
    """东财日线：返回 [{date, close, volume}]，失败返回 []。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={_secid(code)}&klt=101&fqt={fqt}&beg={DATA_BEG}&end=20500101"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f53,f56")
    for _ in range(2):
        try:
            r = client.get(url, timeout=20)
            data = (r.json().get("data") or {})
            out = []
            for row in data.get("klines") or []:
                p = row.split(",")
                if len(p) >= 2:
                    out.append({"date": p[0], "close": float(p[1]), "volume": float(p[2] if len(p) > 2 else 0)})
            if out:
                return out
        except Exception:
            pass
        time.sleep(0.5)
    return []


def _tx_kline(client: httpx.Client, code: str, fqt: int) -> list[dict]:
    """腾讯日线兜底源（proxy.finance.qq.com）。fqt=1 前复权 / 0 不复权。

    接口行为（实测 2026-10）：单次最多 800 核，返回「以区间 end 为终点」的最近 N 根，
    因此倒序分页拼接；指数等无复权标的键为 day，其余 qfqday。
    行格式：[日期, 开盘, 收盘, 最高, 最低, 成交量, ...]
    """
    s = _tx_symbol(code)
    beg = f"{DATA_BEG[:4]}-{DATA_BEG[4:6]}-{DATA_BEG[6:]}"
    merged: dict[str, dict] = {}
    end = date.today().isoformat()
    for _ in range(6):
        if fqt == 1:
            url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?"
                   f"param={s},day,{beg},{end},800,qfq")
        else:
            url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/kline?"
                   f"param={s},day,{beg},{end},800")
        try:
            data = (client.get(url, timeout=20).json().get("data") or {}).get(s) or {}
            rows = data.get("qfqday") or data.get("day") or []
        except Exception:
            break
        if not rows:
            break
        new = 0
        for p in rows:
            if len(p) >= 3 and p[0] not in merged:
                merged[p[0]] = {"date": p[0], "close": float(p[2]),
                                "volume": float(p[5]) if len(p) > 5 else 0}
                new += 1
        first = min(p[0] for p in rows)
        if new == 0 or first <= beg:
            break
        end = first
        time.sleep(1.4)
    return [merged[d] for d in sorted(merged)]


def fetch_kline(client: httpx.Client, code: str, fqt: int) -> list[dict]:
    """东财优先，空结果回落腾讯（两个源都会被限流，互为备份）。"""
    rows = _em_kline(client, code, fqt)
    if rows:
        return rows
    return _tx_kline(client, code, fqt)


def _em_dividends(client: httpx.Client, code: str) -> list[tuple[str, float]]:
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_SHAREBONUS_DET&columns=ALL"
           f"&filter=(SECURITY_CODE%3D%22{code}%22)"
           "&pageSize=120&sortColumns=EX_DIVIDEND_DATE&sortTypes=1&source=WEB&client=WEB")
    # 同一除息日可能有多条记录（如同日两笔方案），按日聚合为合计派息
    agg: dict[str, float] = {}
    for _ in range(2):
        try:
            rows = ((client.get(url, timeout=20).json().get("result") or {}).get("data")) or []
            for r in rows:
                if "实施" not in (r.get("ASSIGN_PROGRESS") or ""):
                    continue
                exd = (r.get("EX_DIVIDEND_DATE") or "")[:10]
                amt = r.get("PRETAX_BONUS_RMB")
                if not exd or amt in (None, ""):
                    continue
                try:
                    per = float(amt) / 10
                except Exception:
                    continue
                if per > 0:
                    agg[exd] = agg.get(exd, 0) + per
            return sorted(agg.items())
        except Exception:
            pass
        time.sleep(0.5)
    return sorted(agg.items())


def lab_codes() -> tuple[list[str], list[str], list[str]]:
    """返回 (银行池, 板块代表股, 全部代码)。板块代表 = 每板块市值最大。"""
    banks, reps, allc = [], [], [ETF_DIVIDEND, ETF_HS300, IDX_HS300]
    try:
        with open(SITE_DATA_PATH, encoding="utf-8") as f:
            rows = json.load(f).get("rows", [])
    except Exception:
        rows = []
    by_sector: dict[str, dict] = {}
    for r in rows:
        c, s = r.get("code"), r.get("sector")
        if not c or not s:
            continue
        if s == "银行":
            banks.append(c)
        best = by_sector.get(s)
        if not best or (r.get("mktcap") or 0) > (best.get("mktcap") or 0):
            by_sector[s] = r
    banks = sorted(set(banks)) or sorted(BIG_BANKS)
    reps = sorted(r["code"] for r in by_sector.values() if r.get("mktcap"))
    allc += sorted(set(banks + reps + BIG_BANKS))
    return banks, reps, allc


def refresh_data(db: Session, force: bool = False) -> dict:
    """增量刷新缓存：K 线落后 3 天以上才重拉。返回统计。"""
    banks, reps, codes = lab_codes()
    fresh_deadline = (date.today() - timedelta(days=3)).isoformat()
    gap = float(os.environ.get("LAB_FETCH_GAP", "1.5"))
    stats = {"codes": 0, "kline_fetched": 0, "kline_skipped": 0,
             "div_fetched": 0, "errors": []}
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}) as cli:
        for code in codes:
            stats["codes"] += 1
            last = db.scalar(select(Kline.date).where(Kline.code == code)
                             .order_by(Kline.date.desc()).limit(1))
            if not force and last and last >= fresh_deadline:
                stats["kline_skipped"] += 1
            else:
                adj = fetch_kline(cli, code, 1)
                raw = fetch_kline(cli, code, 0)
                raw_map = {r["date"]: r["close"] for r in raw}
                if not adj:
                    stats["errors"].append(code)
                else:
                    db.query(Kline).filter(Kline.code == code).delete()
                    db.add_all([Kline(code=code, date=a["date"], adj=a["close"],
                                      raw=raw_map.get(a["date"], a["close"]),
                                      volume=a.get("volume", 0))
                                for a in adj if a["close"] > 0])
                    stats["kline_fetched"] += 1
                time.sleep(gap)
            # 分红只拉股票（ETF/指数没有）
            if not code.startswith(("5",)):
                has = db.scalar(select(StockDividend.id).where(
                    StockDividend.code == code).limit(1))
                if force or not has:
                    divs = _em_dividends(cli, code)
                    if divs:
                        db.query(StockDividend).filter(StockDividend.code == code).delete()
                        db.add_all([StockDividend(code=code, ex_date=d, per=p) for d, p in divs])
                    stats["div_fetched"] += 1
                    time.sleep(0.25)
            db.commit()
    return stats


# ---------------------------------------------------------------- 数据装载
def load_klines(db: Session, code: str) -> list[Kline]:
    return db.scalars(select(Kline).where(Kline.code == code)
                      .order_by(Kline.date)).all()


def load_dividends(db: Session, codes: list[str]) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list] = {c: [] for c in codes}
    rows = db.scalars(select(StockDividend).where(
        StockDividend.code.in_(codes)).order_by(StockDividend.ex_date)).all()
    for r in rows:
        out.setdefault(r.code, []).append((r.ex_date, r.per))
    return out


def _align(dates: list[str], series: dict[str, list[tuple[str, float]]]):
    """按日期并集对齐每只标的，停牌日前向填充。返回 {code: [price]}。"""
    idx = {d: i for i, d in enumerate(dates)}
    out = {}
    for code, pts in series.items():
        col = [None] * len(dates)
        for d, v in pts:
            i = idx.get(d)
            if i is not None:
                col[i] = v
        last = None
        for i in range(len(col)):
            if col[i] is None:
                col[i] = last
            else:
                last = col[i]
        out[code] = col
    return out


def trailing_dps(divs: list[tuple[str, float]], d: str) -> float:
    """过去 365 天每股派息合计（除息日 ≤ d）。divs 已按 ex_date 升序。"""
    lo = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
    return sum(p for e, p in divs if lo < e <= d)


def total_return_series(rows: list[Kline], divs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """用不复权价 + 除息日每股派息自建总收益序列（不依赖数据商 qfq 质量）。

    TR_t = TR_{t-1} × (P_t + D_t) / P_{t-1}，D_t 为当日除息的每股派息。
    仅用于无份额折算的股票；ETF/指数走数据商 qfq。
    """
    div_map = dict(divs or [])
    out, tr, prev = [], 1.0, None
    for r in rows:
        if prev and prev > 0 and r.raw > 0:  # 0 价格是坏行，当缺失跳过
            tr *= (r.raw + div_map.get(r.date, 0.0)) / prev
            out.append((r.date, tr))
        elif not out:
            out.append((r.date, 1.0))
        else:
            out.append((r.date, tr))  # 坏价日沿用前值
        prev = r.raw if r.raw > 0 else prev
    return out


# ---------------------------------------------------------------- 回测引擎
def run_engine(dates: list[str], px: dict[str, list[float]], held_timeline: dict[int, list[str]],
               fee_bps: float = FEE_BPS) -> tuple[list[float], list[dict]]:
    """等权组合模拟。held_timeline: {再平衡日期下标: 持仓代码列表}。

    返回 (净值序列, 换手记录[{date, held, turnover}])。
    """
    n = len(dates)
    value, weights, trades = 1.0, {}, []
    held: list[str] = []
    values = [1.0] * n
    reb = set(held_timeline.keys())
    for t in range(n):
        if t > 0:
            # 当日收益（用昨日收盘时的权重）
            r = 0.0
            for c, w in weights.items():
                p, q = px[c][t], px[c][t - 1]
                if p and q:
                    r += w * (p / q - 1)
            value *= 1 + r
            # 权重漂移
            if 1 + r != 0:
                weights = {c: w * ((px[c][t] / px[c][t - 1]) if px[c][t] and px[c][t - 1] else 1) / (1 + r)
                           for c, w in weights.items()}
        values[t] = value
        # 收盘后按计划再平衡：决策用 t 日收盘，新权重自 t+1 日起生效（无前视）
        if t in reb:
            new_held = held_timeline[t]
            target = {c: 1 / len(new_held) for c in new_held} if new_held else {}
            codes = set(weights) | set(target)
            turnover = sum(abs(target.get(c, 0) - weights.get(c, 0)) for c in codes)
            if turnover > 0:
                value *= max(0.0, 1 - turnover * fee_bps / 10000)
                values[t] = value
            weights = target
            held = new_held
            trades.append({"date": dates[t], "held": held, "turnover": round(turnover, 3)})
    return values, trades


def metrics(dates: list[str], values: list[float]) -> dict:
    if len(values) < 2:
        return {}
    total = values[-1] / values[0] - 1
    years = max((datetime.strptime(dates[-1], "%Y-%m-%d") -
                 datetime.strptime(dates[0], "%Y-%m-%d")).days / 365, 1e-9)
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if years > 0.02 else total
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0
    vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 * math.sqrt(TRADING_DAYS) if rets else 0
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    sharpe = (cagr - RISK_FREE) / vol if vol > 0 else 0
    return {"total": round(total * 100, 2), "cagr": round(cagr * 100, 2),
            "vol": round(vol * 100, 2), "mdd": round(mdd * 100, 2),
            "sharpe": round(sharpe, 2), "years": round(years, 1)}


def _downsample(dates: list[str], values: list[float], cap: int = 400):
    step = max(1, len(dates) // cap)
    return [[dates[i], round(values[i], 4)] for i in range(0, len(dates), step)] + \
           ([[dates[-1], round(values[-1], 4)]] if (len(dates) - 1) % step else [])


# ---------------------------------------------------------------- 策略注册表
STRATEGIES = {
    "bank.dividend": {
        "name": "五大行吃息组合",
        "desc": "工农中建交五大行等权持有，每年 1 月再平衡、分红再投入。基准：沪深300。",
        "params": [],
    },
    "dy.rotate": {
        "name": "股息率门槛轮动（银行池）",
        "desc": "银行池内选近 12 个月股息率 ≥ 门槛的前 8 只等权，每月检查调仓；无标的则持币。基准：沪深300。",
        "params": [{"key": "threshold", "label": "股息率门槛", "options": ["4.5", "5.0", "5.5"], "default": "5.0"}],
    },
    "etf.compare": {
        "name": "红利ETF vs 沪深300ETF",
        "desc": "同期各买入持有红利ETF(510880)与沪深300ETF(510300)，对比长期含分红总收益。",
        "params": [],
    },
    "ma.timing": {
        "name": "双均线择时红利ETF",
        "desc": "红利ETF 上 MA20 在 MA60 上方则持有、否则持币，每月判定一次。基准：红利ETF 一直持有。",
        "params": [],
    },
    "sector.balance": {
        "name": "板块代表股等权年度再平衡",
        "desc": "15 个红利板块各取市值最大一只，等权持有、每年 1 月再平衡。基准：沪深300。",
        "params": [],
    },
}

DEFAULT_START = "2018-01-01"


def _rebalance_idx(dates: list[str], start_i: int, mode: str) -> list[int]:
    """mode: Y=每年首个交易日 / M=每月首个交易日。返回再平衡日期下标（含 start_i）。"""
    out, seen = [start_i], None
    for i in range(start_i + 1, len(dates)):
        d = dates[i]
        key = d[:7] if mode == "M" else d[:4]
        if key != seen:
            out.append(i)
            seen = key
    return out


def run_strategy(db: Session, key: str, params: dict) -> dict:
    spec = STRATEGIES[key]
    banks, reps, _ = lab_codes()
    start = params.get("start", DEFAULT_START)
    bench = IDX_HS300

    # ---- 数据就绪检查
    need = {"bank.dividend": BIG_BANKS, "dy.rotate": banks, "sector.balance": reps,
            "etf.compare": [ETF_DIVIDEND, ETF_HS300], "ma.timing": [ETF_DIVIDEND]}[key] + [bench]
    cached = {c: load_klines(db, c) for c in need}
    missing = [c for c, v in cached.items() if not v]
    if missing:
        return {"ok": False, "message": f"缺少行情缓存：{','.join(missing)}，请先刷新数据"}

    dates_all = sorted({r.date for c in need for r in cached[c] if r.date >= start})
    start_i = 0 if dates_all else -1
    if start_i < 0 or len(dates_all) < 60:
        return {"ok": False, "message": "区间数据不足"}

    def series(code, field):
        return [(r.date, getattr(r, field)) for r in cached[code] if r.date >= start]

    # ---- 对比类：两条买入持有曲线
    if key == "etf.compare":
        common = set(d for d, _ in series(ETF_DIVIDEND, "adj")) & set(d for d, _ in series(ETF_HS300, "adj"))
        dates_all = sorted(d for d in dates_all if d in common)
        out = {"ok": True, "strategy": key, "name": spec["name"], "start": dates_all[0], "end": dates_all[-1],
               "curves": {}, "metrics": {}, "mode": "compare"}
        for c, label in [(ETF_DIVIDEND, "红利ETF"), (ETF_HS300, "沪深300ETF")]:
            px = dict(_align(dates_all, {c: series(c, "adj")}).items())
            v0 = px[c][0]
            vals = [p / v0 for p in px[c]]
            out["curves"][label] = _downsample(dates_all, vals)
            out["metrics"][label] = metrics(dates_all, vals)
        return out

    # ---- 组合类
    if key == "bank.dividend":
        universe, mode = BIG_BANKS, "Y"
    elif key == "dy.rotate":
        universe, mode = banks, "M"
    elif key == "sector.balance":
        universe, mode = reps, "Y"
    else:  # ma.timing
        universe, mode = [ETF_DIVIDEND], "M"

    divs = load_dividends(db, [c for c in universe if not c.startswith("5")])
    raw_series = {c: series(c, "raw") for c in universe}
    # 股票：不复权价+分红自建总收益（不依赖数据商 qfq）；ETF/指数：数据商 qfq
    adj_series = {
        c: (total_return_series(cached[c], divs.get(c, [])) if not c.startswith("5")
            else series(c, "adj"))
        for c in universe
    }
    raw_px = _align(dates_all, raw_series)
    adj_px = _align(dates_all, adj_series)
    bench_px = dict(_align(dates_all, {bench: series(bench, "adj")}).items())[bench]

    first_valid = max(next((i for i, p in enumerate(raw_px[c]) if p), len(dates_all))
                      for c in universe)
    reb_dates = [i for i in _rebalance_idx(dates_all, first_valid, mode) if i < len(dates_all) - 1]

    held_timeline: dict[int, list[str]] = {}
    ma_cache: dict[str, list[float]] = {}
    for i in reb_dates:
        d = dates_all[i]
        if key == "dy.rotate":
            scored = []
            for c in universe:
                col = raw_px[c]
                if not col[i]:
                    continue
                dy = trailing_dps(divs.get(c, []), d) / col[i] * 100
                if dy >= float(params.get("threshold", "5.0")):
                    scored.append((dy, c))
            scored.sort(reverse=True)
            held_timeline[i] = [c for _, c in scored[:8]]
        elif key == "ma.timing":
            col = adj_px[ETF_DIVIDEND]
            if ETF_DIVIDEND not in ma_cache:
                ma_cache[ETF_DIVIDEND] = col
            c = ma_cache[ETF_DIVIDEND]
            win_f = [p for p in c[max(0, i - 19):i + 1] if p]
            win_s = [p for p in c[max(0, i - 59):i + 1] if p]
            ma20 = sum(win_f) / len(win_f) if len(win_f) >= 15 else None
            ma60 = sum(win_s) / len(win_s) if len(win_s) >= 45 else None
            held_timeline[i] = [ETF_DIVIDEND] if (ma20 and ma60 and ma20 > ma60) else []
        else:
            held_timeline[i] = [c for c in universe if raw_px[c][i]]

    if not held_timeline:
        return {"ok": False, "message": "无有效再平衡时点"}

    vals_adj, trades = run_engine(dates_all, adj_px, held_timeline)
    vals_raw, _ = run_engine(dates_all, raw_px, held_timeline)
    b0 = bench_px[first_valid] or next(p for p in bench_px if p)
    bench_vals = [p / b0 for p in bench_px]

    m_p, m_b = metrics(dates_all, vals_adj), metrics(dates_all, bench_vals)
    m_price = metrics(dates_all, vals_raw)
    last_held = trades[-1]["held"] if trades else []
    names = {}
    try:
        with open(SITE_DATA_PATH, encoding="utf-8") as f:
            names = {r["code"]: r["name"] for r in json.load(f).get("rows", [])}
    except Exception:
        pass
    names.update({ETF_DIVIDEND: "红利ETF", ETF_HS300: "沪深300ETF", IDX_HS300: "沪深300"})

    return {
        "ok": True, "strategy": key, "name": spec["name"],
        "start": dates_all[0], "end": dates_all[-1],
        "params": {k: params.get(k) for k in ("threshold",) if params.get(k)},
        "curves": {
            "portfolio": _downsample(dates_all, vals_adj),
            "benchmark": _downsample(dates_all, bench_vals),
        },
        "metrics": {"portfolio": m_p, "benchmark": m_b},
        "decomposition": {
            "total": m_p.get("total"), "price_only": m_price.get("total"),
            "div_reinvest": round((m_p.get("total", 0)) - (m_price.get("total", 0)), 2),
        },
        "last_held": [{"code": c, "name": names.get(c, c)} for c in last_held],
        "rebalances": len(trades),
        "mode": "portfolio",
    }


# ---------------------------------------------------------------- API
class RunReq(BaseModel):
    strategy: str
    start: str | None = None
    threshold: str | None = None


@router.get("/strategies")
def list_strategies(user: User | None = Depends(require_feature("lab.backtest"))):
    return {"strategies": [{"key": k, **v} for k, v in STRATEGIES.items()]}


@router.post("/backtest")
def run(req: RunReq, user: User | None = Depends(require_feature("lab.backtest")),
        db: Session = Depends(get_db)):
    if req.strategy not in STRATEGIES:
        return {"ok": False, "message": "未知策略"}
    params = {"start": req.start or DEFAULT_START}
    if req.threshold:
        params["threshold"] = req.threshold
    try:
        return run_strategy(db, req.strategy, params)
    except Exception as e:
        return {"ok": False, "message": f"回测失败：{e}"}


@router.get("/data/status")
def data_status(user: User | None = Depends(require_feature("lab.backtest")),
                db: Session = Depends(get_db)):
    banks, reps, codes = lab_codes()
    n_kline = len(db.scalars(select(Kline.code).distinct()).all())
    n_div = len(db.scalars(select(StockDividend.code).distinct()).all())
    last = db.scalar(select(Kline.date).order_by(Kline.date.desc()).limit(1))
    return {"codes_planned": len(codes), "kline_cached": n_kline,
            "div_cached": n_div, "last_bar": last}


@router.post("/data/refresh")
def data_refresh(admin: User = Depends(require_admin), db: Session = Depends(get_db),
                 force: bool = False):
    return {"ok": True, **refresh_data(db, force=force)}


def import_klines(db: Session, payload: dict) -> dict:
    """导入外部拉取的 K 线缓存：{code: [[date, adj, raw, volume], ...]}。"""
    for code, rows in payload.items():
        db.query(Kline).filter(Kline.code == code).delete()
        db.add_all([Kline(code=code, date=r[0], adj=float(r[1]), raw=float(r[2]),
                          volume=float(r[3] or 0))
                    for r in rows if float(r[1]) > 0 and float(r[2]) > 0])
    db.commit()
    return {"codes": len(payload), "bars": sum(len(v) for v in payload.values())}


if __name__ == "__main__":
    # CLI：python -m app.lab refresh[-f]|test <策略key>|status|codes|import(<stdin JSON)
    from .db import SessionLocal
    db = SessionLocal()
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "refresh":
        print(json.dumps(refresh_data(db, force="-f" in sys.argv), ensure_ascii=False, indent=1))
    elif cmd == "test":
        r = run_strategy(db, sys.argv[2] if len(sys.argv) > 2 else "bank.dividend", {})
        print(json.dumps(r, ensure_ascii=False, indent=1)[:2500])
    elif cmd == "codes":
        banks, reps, codes = lab_codes()
        print(json.dumps({"banks": banks, "reps": reps, "all": codes}, ensure_ascii=False))
    elif cmd == "import":
        payload = json.load(sys.stdin)
        print(json.dumps(import_klines(db, payload), ensure_ascii=False))
    else:
        banks, reps, codes = lab_codes()
        n_k = len(db.scalars(select(Kline.code).distinct()).all())
        n_d = len(db.scalars(select(StockDividend.code).distinct()).all())
        last = db.scalar(select(Kline.date).order_by(Kline.date.desc()).limit(1))
        print({"codes_planned": len(codes), "kline_cached": n_k,
               "div_cached": n_d, "last_bar": last})
    db.close()
