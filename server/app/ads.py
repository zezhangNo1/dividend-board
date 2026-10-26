"""广告位系统：数据库配置素材 + 曝光/点击埋点 + 管理 CRUD/效果统计。

合规约束（对应 广告招商方案-v2 第二节，接入素材前逐条核对）：
  - 只接非金融产品（工具/软件/数据服务/硬件），任何金融产品品牌与导流一律不上
  - 素材必须可识别：前端固定渲染「广告」标签
  - 广告内容禁用收益承诺/保本暗示用语（广告法第二十五条）
  - 不做弹窗，只做融入型文字条
展示走 require_feature("ads.slots")：功能开关一关，全站广告位瞬间消失。
"""
import hashlib
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .features import require_admin, require_feature
from .models import AdCreative, AdEvent, User

router = APIRouter(prefix="/api/ads", tags=["ads"])

SALT = os.environ.get("JWT_SECRET", "board")

# 广告位注册（展示规格与报价口径，ads.html 同步展示）
SLOTS = {
    "a1": {"name": "首页顶部横幅", "spec": "文字条，桌面自适应 / 移动满宽", "note": "打开即见，曝光最高"},
    "a2": {"name": "首页表格下方", "spec": "文字两行 / 图文卡", "note": "看完数据后的决策时刻"},
    "a3": {"name": "个股档案页底部", "spec": "单条文字 + 品牌名", "note": "流量小、意图最强"},
    "a4": {"name": "AI 日报页底部", "spec": "文字条 / 内容合作位", "note": "日更页面，复访高"},
}


def _active_creative(db: Session, slot: str) -> AdCreative | None:
    today = date.today().isoformat()
    rows = db.scalars(select(AdCreative).where(
        AdCreative.slot_key == slot, AdCreative.status == "active")
        .order_by(AdCreative.id.desc())).all()
    for r in rows:
        if (not r.starts_at or r.starts_at <= today) and (not r.ends_at or r.ends_at >= today):
            return r
    return None


class AdBeacon(BaseModel):
    id: int
    type: str  # imp|click


class AdCreateReq(BaseModel):
    slot_key: str
    sponsor: str
    title: str
    body: str | None = None
    link_url: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    status: str = "draft"


class AdUpdateReq(BaseModel):
    status: str | None = None
    title: str | None = None
    body: str | None = None
    link_url: str | None = None
    ends_at: str | None = None


# ---------------------------------------------------------------- 公开接口
@router.get("/slots")
def serve_slots(user: User | None = Depends(require_feature("ads.slots")),
                db: Session = Depends(get_db)):
    """下发各广告位当前生效素材；无素材的位不下发（前端留空）。"""
    out = {}
    for key in SLOTS:
        c = _active_creative(db, key)
        if c:
            out[key] = {"id": c.id, "title": c.title, "body": c.body,
                        "link": c.link_url, "sponsor": c.sponsor}
    return {"slots": out}


@router.post("/track")
def track_ad(req: AdBeacon, request: Request, db: Session = Depends(get_db)):
    if req.type not in ("imp", "click"):
        return {"ok": False}
    c = db.get(AdCreative, req.id)
    if not c or c.status != "active":
        return {"ok": False}
    ua = request.headers.get("user-agent", "")[:120]
    ip = (request.headers.get("x-forwarded-for", "") or
          (request.client.host if request.client else ""))[:64]
    vh = hashlib.sha256(f"{ip}|{ua}|{SALT}".encode()).hexdigest()[:16]
    # 频控：同访客 1 小时 240 条（曝光+点击合计），异常流量静默丢弃
    now = datetime.now(timezone.utc)
    recent = db.scalar(select(func.count()).select_from(AdEvent).where(
        AdEvent.visitor_hash == vh, AdEvent.ts > now - timedelta(hours=1)))
    if recent and recent > 240:
        return {"ok": False}
    db.add(AdEvent(day=date.today().isoformat(), creative_id=c.id,
                   etype=req.type, visitor_hash=vh))
    if req.type == "imp":
        c.impressions += 1
    else:
        c.clicks += 1
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 管理接口
@router.get("/admin/list")
def admin_list(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AdCreative).order_by(AdCreative.id.desc())).all()
    today = date.today().isoformat()
    return {"slots": [{"key": k, **v} for k, v in SLOTS.items()],
            "creatives": [{
                "id": r.id, "slot": r.slot_key, "slot_name": SLOTS.get(r.slot_key, {}).get("name", r.slot_key),
                "sponsor": r.sponsor, "title": r.title, "body": r.body,
                "link": r.link_url, "status": r.status,
                "starts_at": r.starts_at, "ends_at": r.ends_at,
                "impressions": r.impressions, "clicks": r.clicks,
                "ctr": round(r.clicks / r.impressions * 100, 2) if r.impressions else 0,
                "active_now": (r.status == "active" and
                               (not r.starts_at or r.starts_at <= today) and
                               (not r.ends_at or r.ends_at >= today)),
            } for r in rows]}


@router.post("/admin/create")
def admin_create(req: AdCreateReq, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    if req.slot_key not in SLOTS:
        return {"ok": False, "message": "未知广告位"}
    if req.status not in ("draft", "active", "paused"):
        return {"ok": False, "message": "status 取值不合法"}
    # 合规守门：标题/文案里的违禁词直接拒绝（宁可错杀，人工再核）
    banned = ["稳赚", "保本", "无风险", "高收益", "零风险", "本息保障", "轻松赚钱",
              "开户", "返佣", "荐股", "目标价"]
    text = f"{req.title} {req.body or ''}"
    hit = [w for w in banned if w in text]
    if hit:
        return {"ok": False, "message": f"素材含违禁词 {hit}，请修改（广告法第二十五条/金融营销红线）"}
    c = AdCreative(slot_key=req.slot_key, sponsor=req.sponsor[:64], title=req.title[:128],
                   body=req.body, link_url=req.link_url,
                   starts_at=req.starts_at, ends_at=req.ends_at, status=req.status)
    db.add(c)
    db.commit()
    return {"ok": True, "id": c.id}


@router.post("/admin/update/{cid}")
def admin_update(cid: int, req: AdUpdateReq, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    c = db.get(AdCreative, cid)
    if not c:
        return {"ok": False, "message": "素材不存在"}
    if req.status:
        if req.status not in ("draft", "active", "paused", "done"):
            return {"ok": False, "message": "status 取值不合法"}
        c.status = req.status
    if req.title is not None:
        c.title = req.title[:128]
    if req.body is not None:
        c.body = req.body
    if req.link_url is not None:
        c.link_url = req.link_url
    if req.ends_at is not None:
        c.ends_at = req.ends_at
    db.commit()
    return {"ok": True}


@router.delete("/admin/{cid}")
def admin_delete(cid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    c = db.get(AdCreative, cid)
    if not c:
        return {"ok": False, "message": "素材不存在"}
    db.delete(c)
    db.execute(AdEvent.__table__.delete().where(AdEvent.creative_id == cid))
    db.commit()
    return {"ok": True}


@router.get("/admin/stats")
def admin_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """近 14 天按日曝光/点击（效果报告用）。"""
    since = (date.today() - timedelta(days=13)).isoformat()
    rows = db.execute(select(AdEvent.day, AdEvent.etype, func.count())
                      .where(AdEvent.day >= since)
                      .group_by(AdEvent.day, AdEvent.etype)).all()
    days: dict[str, dict] = {}
    for d, t, n in rows:
        days.setdefault(d, {"day": d, "imp": 0, "click": 0})[t] = n
    return {"daily": sorted(days.values(), key=lambda x: x["day"], reverse=True)}
