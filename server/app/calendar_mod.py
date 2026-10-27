"""除权除息日历：东财全市场未来除息计划 → 过滤本站标的池，缓存 12 小时。

数据源：datacenter-web RPT_SHAREBONUS_DET，按 EX_DIVIDEND_DATE 区间过滤；
同日多笔按日聚合（与 dividends 表口径一致）。
"""
import json
import os
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .features import require_feature
from .models import DivCalendar, User

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

SITE_DATA_PATH = os.environ.get("SITE_DATA_PATH", "/srv/site_data.json")
CACHE_HOURS = 12
_last_fetch: datetime | None = None


def _universe() -> dict[str, str]:
    try:
        with open(SITE_DATA_PATH, encoding="utf-8") as f:
            return {r["code"]: r["name"] for r in json.load(f).get("rows", [])}
    except Exception:
        return {}


def refresh_calendar(db: Session, force: bool = False) -> bool:
    """拉未来 120 天全市场除息，落库本站池内的记录。成功返回 True。"""
    global _last_fetch
    if not force and _last_fetch and (datetime.utcnow() - _last_fetch) < timedelta(hours=CACHE_HOURS):
        return True
    uni = _universe()
    if not uni:
        return False
    today = date.today()
    beg, end = today.isoformat(), (today + timedelta(days=120)).isoformat()
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_SHAREBONUS_DET&columns=SECURITY_CODE,SECURITY_NAME_ABBR,EX_DIVIDEND_DATE,PRETAX_BONUS_RMB,ASSIGN_PROGRESS"
           f"&filter=(EX_DIVIDEND_DATE%3E%3D%27{beg}%27)(EX_DIVIDEND_DATE%3C%3D%27{end}%27)"
           "&pageSize=1000&sortColumns=EX_DIVIDEND_DATE&sortTypes=1&source=WEB&client=WEB")
    agg: dict[tuple[str, str], dict] = {}
    try:
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0",
                                   "Referer": "https://data.eastmoney.com/"}, timeout=25) as cli:
            r = cli.get(url)
            rows = ((r.json().get("result") or {}).get("data")) or []
        for x in rows:
            code = str(x.get("SECURITY_CODE") or "")
            exd = (x.get("EX_DIVIDEND_DATE") or "")[:10]
            if code not in uni or not exd:
                continue
            try:
                per = float(x.get("PRETAX_BONUS_RMB") or 0) / 10
            except Exception:
                continue
            if per <= 0:
                continue
            key = (code, exd)
            item = agg.setdefault(key, {"code": code, "name": uni[code], "ex_date": exd, "per": 0.0})
            item["per"] += per
    except Exception:
        return False
    db.query(DivCalendar).delete()
    db.add_all([DivCalendar(**v) for v in agg.values()])
    db.commit()
    _last_fetch = datetime.utcnow()
    return True


@router.get("")
def calendar(days: int = 90, user: User | None = Depends(require_feature("board.calendar")),
             db: Session = Depends(get_db)):
    days = max(7, min(days, 120))
    today = date.today()
    horizon = (today + timedelta(days=days)).isoformat()
    have = db.scalar(select(DivCalendar.id).limit(1))
    if have is None or not refresh_calendar(db):
        pass  # 拉不到就返回库里旧数据
    rows = db.scalars(select(DivCalendar).where(
        DivCalendar.ex_date >= today.isoformat(),
        DivCalendar.ex_date <= horizon).order_by(DivCalendar.ex_date)).all()
    return {"ok": True, "days": days, "today": today.isoformat(),
            "items": [{"code": r.code, "name": r.name, "ex_date": r.ex_date,
                       "per": round(r.per, 4),
                       "in_days": (datetime.strptime(r.ex_date, "%Y-%m-%d").date() - today).days}
                      for r in rows]}
