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
