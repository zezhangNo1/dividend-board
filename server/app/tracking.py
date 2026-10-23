"""浏览量记录：页面 JS 埋点上报 → page_views 表；统计接口走 stats.views 功能开关。

设计：
  - 上报端点公开（所有访客都要能记），但同一访客每小时上限 120 条，超了静默丢弃
  - 不存原始 IP，只存 IP+UA 的匿名哈希（UV 口径）；UA 只存前 120 字符供事后分类
  - 来源只存域名；站内跳转记为 internal
  - 统计聚合：按日 PV/UV、热门页面、热门个股、来源、设备；仅授权账号可见
"""
import hashlib
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .features import optional_user, require_feature
from .models import PageView, User

router = APIRouter(prefix="/api", tags=["tracking"])

SALT = os.environ.get("JWT_SECRET", "board")
PATH_RE = re.compile(r"^/[A-Za-z0-9._\-/]*$")


class ViewReq(BaseModel):
    path: str = "/"
    q: str = ""
    ref: str = ""


def _device(ua: str) -> str:
    return "mobile" if re.search(r"Mobile|Android|iPhone|iPad", ua) else "desktop"


def _ref_domain(ref: str) -> str:
    if not ref:
        return "direct"
    try:
        host = urlsplit(ref).netloc.lower()
        return host.split("@")[-1].split(":")[0][:64] if host else "direct"
    except Exception:
        return "direct"


@router.post("/track/view")
def track_view(req: ViewReq, request: Request, db: Session = Depends(get_db)):
    path = req.path if PATH_RE.match(req.path or "") and len(req.path) <= 120 else "/"
    q = (req.q or "")[:60]
    ua = request.headers.get("user-agent", "")[:120]
    ip = (request.headers.get("x-forwarded-for", "") or
          (request.client.host if request.client else ""))[:64]
    vh = hashlib.sha256(f"{ip}|{ua}|{SALT}".encode()).hexdigest()[:16]

    # 频控：同访客 1 小时超 120 条视为异常，静默丢弃
    now = datetime.now(timezone.utc)
    recent = db.scalar(select(func.count()).select_from(PageView).where(
        PageView.visitor_hash == vh, PageView.ts > now - timedelta(hours=1)))
    if recent and recent > 120:
        return {"ok": False}

    user = optional_user(request, db)
    ref = _ref_domain(req.ref)
    if ref and (ref == request.headers.get("host", "___").split(":")[0]):
        ref = "internal"
    db.add(PageView(day=date.today().isoformat(), path=path, q=q,
                    ref_domain=ref, device=_device(ua), ua_short=ua,
                    visitor_hash=vh, user_id=user.id if user else None))
    db.commit()
    return {"ok": True}


@router.get("/stats/summary")
def stats_summary(days: int = 30, user: User | None = Depends(require_feature("stats.views")),
                  db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    since = (date.today() - timedelta(days=days - 1)).isoformat()

    daily = db.execute(
        select(PageView.day, func.count().label("pv"),
               func.count(func.distinct(PageView.visitor_hash)).label("uv"))
        .where(PageView.day >= since).group_by(PageView.day)
        .order_by(PageView.day.desc())).all()

    top_pages = db.execute(
        select(PageView.path, func.count().label("pv"),
               func.count(func.distinct(PageView.visitor_hash)).label("uv"))
        .where(PageView.day >= since).group_by(PageView.path)
        .order_by(func.count().desc()).limit(12)).all()

    top_stocks = db.execute(
        select(PageView.q, func.count().label("pv"))
        .where(PageView.day >= since, PageView.path == "/stock.html", PageView.q != "")
        .group_by(PageView.q).order_by(func.count().desc()).limit(12)).all()

    refs = db.execute(
        select(PageView.ref_domain, func.count().label("pv"))
        .where(PageView.day >= since).group_by(PageView.ref_domain)
        .order_by(func.count().desc()).limit(12)).all()

    devices = db.execute(
        select(PageView.device, func.count().label("pv"))
        .where(PageView.day >= since).group_by(PageView.device)).all()

    logged_in = db.scalar(select(func.count()).select_from(PageView).where(
        PageView.day >= since, PageView.user_id.isnot(None))) or 0
    total_pv = sum(d.pv for d in daily)

    return {
        "days": days,
        "total_pv": total_pv,
        "total_uv": sum(d.uv for d in daily),
        "logged_in_pv": logged_in,
        "daily": [{"day": d.day, "pv": d.pv, "uv": d.uv} for d in daily],
        "top_pages": [{"path": p.path, "pv": p.pv, "uv": p.uv} for p in top_pages],
        "top_stocks": [{"code": s.q.replace("code=", "")[:6], "pv": s.pv} for s in top_stocks],
        "refs": [{"ref": r.ref_domain, "pv": r.pv} for r in refs],
        "devices": [{"device": d.device or "unknown", "pv": d.pv} for d in devices],
    }
