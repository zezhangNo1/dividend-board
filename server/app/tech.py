"""技术位置：日/周/月布林带（20,2）客观状态，供个股档案页展示。

口径：用不复权收盘价计算（短期带宽下分红跳空影响可忽略）；
周线=按自然周取最后收盘，月线=按自然月取最后收盘（由日线重采样，无需额外数据）。
状态词为客观位置描述（下轨下方/中轨下方/中轨上方/上轨上方），不构成任何操作提示。
"""
import json
import os
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Kline, User
from .features import require_feature

router = APIRouter(prefix="/api/stock", tags=["tech"])

SITE_DATA_PATH = os.environ.get("SITE_DATA_PATH", "/srv/site_data.json")


def _bands(closes: list[float], n: int = 20, k: float = 2.0):
    if len(closes) < n:
        return None
    win = closes[-n:]
    mid = sum(win) / n
    var = sum((x - mid) ** 2 for x in win) / n
    sd = var ** 0.5
    return mid, mid - k * sd, mid + k * sd


def _position(last: float, b) -> tuple[str, float]:
    mid, lo, hi = b
    span = hi - lo
    pb = (last - lo) / span if span > 0 else 0.5
    if last < lo:
        label = "下轨下方"
    elif last < mid:
        label = "中轨下方"
    elif last <= hi:
        label = "中轨上方"
    else:
        label = "上轨上方"
    return label, pb


def _resample(rows: list[Kline], mode: str) -> list[float]:
    """mode: W=自然周 / M=自然月，各组取最后收盘。"""
    groups: dict[str, float] = {}
    for r in rows:
        key = r.date[:7] if mode == "M" else datetime.strptime(r.date, "%Y-%m-%d").strftime("%G-%V")
        groups[key] = r.raw  # 日期升序，后写覆盖=组内最后
    return [groups[k] for k in sorted(groups)]


@router.get("/tech")
def stock_tech(code: str, user: User | None = Depends(require_feature("board.stock")),
               db: Session = Depends(get_db)):
    rows = db.scalars(select(Kline).where(Kline.code == code).order_by(Kline.date)).all()
    closes = [r.raw for r in rows if r.raw and r.raw > 0]
    if len(closes) < 25:
        return {"ok": False, "message": "技术数据准备中"}
    last = closes[-1]

    out = {}
    b = _bands(closes[-260:])
    if b:
        label, pb = _position(last, b)
        out["daily"] = {"label": label, "pb": round(pb * 100), "mid": round(b[0], 2),
                        "lower": round(b[1], 2), "upper": round(b[2], 2)}
    weekly = _resample(rows, "W")
    b = _bands(weekly)
    if b:
        label, pb = _position(weekly[-1], b)
        out["weekly"] = {"label": label, "pb": round(pb * 100)}
    monthly = _resample(rows, "M")
    b = _bands(monthly)
    if b:
        label, pb = _position(monthly[-1], b)
        out["monthly"] = {"label": label, "pb": round(pb * 100)}

    if not out:
        return {"ok": False, "message": "技术数据准备中"}

    names = {}
    try:
        with open(SITE_DATA_PATH, encoding="utf-8") as f:
            names = {r["code"]: r["name"] for r in json.load(f).get("rows", [])}
    except Exception:
        pass
    return {"ok": True, "code": code, "name": names.get(code, code),
            "asof": rows[-1].date, "bands": out,
            "note": "布林带(20,2)不复权日线口径；周/月线由日线重采样"}


# ---------------------------------------------------------------- 历史股息率分位
def _month_ends(rows: list[Kline]) -> list[Kline]:
    out, cur = [], None
    for r in rows:
        if cur is None or r.date[:7] != cur.date[:7]:
            out.append(cur) if cur else None
            cur = r
        else:
            cur = r
    if cur:
        out.append(cur)
    return out


@router.get("/yieldhist")
def yield_hist(code: str, user: User | None = Depends(require_feature("board.stock")),
               db: Session = Depends(get_db)):
    """月度股息率序列 + 当前分位（理杏仁式）。dy = 过去365天每股派息(除息口径) ÷ 月末不复权收盘。"""
    from datetime import timedelta
    from .models import StockDividend
    from .lab import trailing_dps

    krows = db.scalars(select(Kline).where(Kline.code == code).order_by(Kline.date)).all()
    divs = db.scalars(select(StockDividend).where(
        StockDividend.code == code).order_by(StockDividend.ex_date)).all()
    pairs = [(d.ex_date, d.per) for d in divs]
    if not krows or not pairs:
        return {"ok": False, "message": "分红历史数据积累中"}

    pts = []
    for r in _month_ends(krows):
        if not r.raw or r.raw <= 0:
            continue
        dps = trailing_dps(pairs, r.date)
        if dps > 0:
            pts.append((r.date[:7], round(dps / r.raw * 100, 3)))
    if len(pts) < 24:
        return {"ok": False, "message": "历史数据积累中（不足两年）"}

    # 稳健过滤：剔除中位数 1/3~3 倍之外的异常月（防历史脏价/错配干扰统计）
    import statistics
    med0 = statistics.median(v for _, v in pts)
    raw_n = len(pts)
    pts = [p for p in pts if med0 / 3 <= p[1] <= med0 * 3]
    if len(pts) < 24:
        return {"ok": False, "message": "历史数据积累中（不足两年）"}

    vals = sorted(v for _, v in pts)
    cur = pts[-1][1]
    pct = round(sum(1 for v in vals if v <= cur) / len(vals) * 100)

    def q(p):
        return round(vals[min(len(vals) - 1, int(len(vals) * p))], 2)

    # 降采样到 ≤150 点
    step = max(1, len(pts) // 150)
    samp = pts[::step]
    if samp[-1] != pts[-1]:
        samp.append(pts[-1])
    names = {}
    try:
        with open(SITE_DATA_PATH, encoding="utf-8") as f:
            names = {r["code"]: r["name"] for r in json.load(f).get("rows", [])}
    except Exception:
        pass
    years = round((len(pts)) / 12, 1)
    return {"ok": True, "code": code, "name": names.get(code, code),
            "months": len(pts), "years": years,
            "from": pts[0][0], "to": pts[-1][0],
            "cur": cur, "pct": pct,
            "min": q(0.0), "p25": q(0.25), "median": q(0.5), "p75": q(0.75), "max": q(1.0),
            "points": samp,
            "note": "月度采样：近12个月已实施派息 ÷ 月末收盘（不复权）；分位=当前值高于历史样本的比例"}
