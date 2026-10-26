"""功能可见性开关：注册表 + 白名单授权 + 统一 gate。

判定规则（唯一入口，前后端共用逻辑）：
  1. 功能不在注册表 / status=off        → 任何人都不可见（404 隐身）
  2. layer=public                       → 所有人可见（含未登录）
  3. layer=member                       → 登录即可见（tier 细分后续再加）
  4. layer=self                         → role=admin 恒可见；role=self 默认可见；
                                          其余账号看 feature_grants：显式 allow 且未过期可见，
                                          显式 deny 不可见，无记录不可见

未授权一律抛 404（不是 403），不向外部暴露功能存在。
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Feature, FeatureAccessLog, FeatureGrant, User

router = APIRouter(prefix="/api/features", tags=["features"])

# 启动时补种的功能清单（已存在的不动，新行按这里的默认值出生）
SEED_FEATURES = [
    ("board.main", "高股息看板", "public", "118 只标的全景行情与股息率"),
    ("board.cards", "研究卡片库", "public", "历史研究卡片存档"),
    ("board.stock", "个股档案页", "public", "个股股息率与分红明细"),
    ("ai.daily", "AI 每日观察", "public", "每日盘后 AI 生成的红利数据观察"),
    ("ai.report", "AI 个股报告", "member", "按需个股 AI 报告（Phase 3，接口未开放）"),
    ("lab.backtest", "策略实验室", "self", "历史回测（开发中）"),
    ("lab.signals", "择时信号看板", "self", "全标的择时信号（开发中）"),
    ("lab.factors", "多因子排行", "self", "因子排行与监控（开发中）"),
    ("stats.views", "流量统计", "self", "页面浏览量记录与管理视图（不在前台展示）"),
    ("ads.slots", "广告位", "public", "站内广告位展示（素材走后台配置，本开关=全站一键下线）"),
]


def ensure_seed(db: Session):
    for key, name, layer, desc in SEED_FEATURES:
        if not db.get(Feature, key):
            db.add(Feature(key=key, name=name, layer=layer, status="on", description=desc))
    db.commit()


def _now():
    return datetime.now(timezone.utc)


def visible(db: Session, user: User | None, key: str) -> bool:
    f = db.get(Feature, key)
    if not f or f.status != "on":
        return False
    if f.layer == "public":
        return True
    if user is None:
        return False
    if f.layer == "member":
        return True
    # self 层
    if user.role in ("admin", "self"):
        return True
    g = db.scalar(select(FeatureGrant).where(
        FeatureGrant.user_id == user.id, FeatureGrant.feature_key == key))
    if g and g.allow:
        return g.expires_at is None or g.expires_at > _now()
    return False


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """匿名也放行（public 功能需要对游客开放），有合法会话则返回用户。"""
    authz = request.headers.get("authorization", "")
    token = authz[7:] if authz.startswith("Bearer ") else request.cookies.get("token", "")
    if not token:
        return None
    try:
        import jwt as pyjwt
        from .auth import JWT_SECRET
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None
    user = db.get(User, payload.get("uid"))
    if not user or user.status != "active":
        return None
    if (user.token_ver or 0) != payload.get("tv", -1):
        return None
    return user


def require_feature(key: str):
    """路由依赖：功能不可见 → 404；self 层访问留痕。用法：
    @router.get("")
    def handler(user: User = Depends(require_feature("ai.daily")), ...):
    """
    def dep(request: Request, db: Session = Depends(get_db)) -> User | None:
        user = optional_user(request, db)
        f = db.get(Feature, key)
        if not visible(db, user, key):
            raise HTTPException(404, "Not Found")
        if f and f.layer == "self":
            db.add(FeatureAccessLog(
                user_id=user.id if user else None, feature_key=key,
                ip=(request.headers.get("x-forwarded-for", "") or
                    (request.client.host if request.client else ""))[:64]))
            db.commit()
        return user
    return dep


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """未登录/非管理员一律 404，不暴露管理接口存在。"""
    user = optional_user(request, db)
    if not user or user.role != "admin":
        raise HTTPException(404, "Not Found")
    return user


def user_feature_keys(db: Session, user: User | None) -> list[str]:
    keys = db.scalars(select(Feature.key).where(Feature.status == "on")).all()
    return [k for k in keys if visible(db, user, k)]


# ---------------------------------------------------------------- 管理接口
class FeatureUpdate(BaseModel):
    layer: str | None = None    # public|member|self
    status: str | None = None   # on|off


class GrantReq(BaseModel):
    feature_key: str
    email: str
    allow: bool = True
    days: int | None = None     # 空=永久


@router.get("/admin/list")
def admin_list(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    feats = db.scalars(select(Feature).order_by(Feature.key)).all()
    grants = db.scalars(select(FeatureGrant).order_by(FeatureGrant.id.desc())).all()
    users = {u.id: u for u in db.scalars(select(User)).all()}
    return {
        "features": [{
            "key": f.key, "name": f.name, "layer": f.layer, "status": f.status,
            "description": f.description,
        } for f in feats],
        "grants": [{
            "id": g.id, "feature_key": g.feature_key, "user_id": g.user_id,
            "email": users[g.user_id].email if g.user_id in users else f"#{g.user_id}",
            "allow": g.allow, "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        } for g in grants],
    }


@router.post("/admin/update")
def admin_update(req: FeatureUpdate, key: str,
                 admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    f = db.get(Feature, key)
    if not f:
        raise HTTPException(404, "功能不存在")
    if req.layer:
        if req.layer not in ("public", "member", "self"):
            raise HTTPException(400, "layer 取值不合法")
        f.layer = req.layer
    if req.status:
        if req.status not in ("on", "off"):
            raise HTTPException(400, "status 取值不合法")
        f.status = req.status
    db.commit()
    return {"ok": True, "key": key, "layer": f.layer, "status": f.status}


@router.post("/admin/grant")
def admin_grant(req: GrantReq, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if not db.get(Feature, req.feature_key):
        raise HTTPException(404, "功能不存在")
    user = db.scalar(select(User).where(User.email == req.email.strip().lower()))
    if not user:
        raise HTTPException(404, "账号不存在（需先用该邮箱注册过）")
    g = db.scalar(select(FeatureGrant).where(
        FeatureGrant.user_id == user.id, FeatureGrant.feature_key == req.feature_key))
    expires = None
    if req.days:
        expires = _now() + timedelta(days=req.days)
    if g:
        g.allow, g.expires_at = req.allow, expires
    else:
        g = FeatureGrant(user_id=user.id, feature_key=req.feature_key,
                         allow=req.allow, expires_at=expires)
        db.add(g)
    db.commit()
    return {"ok": True, "id": g.id, "email": user.email,
            "message": f"已{'授权' if req.allow else '拒绝'} {user.email} 访问 {req.feature_key}"}


@router.delete("/admin/grant/{grant_id}")
def admin_revoke(grant_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    g = db.get(FeatureGrant, grant_id)
    if not g:
        raise HTTPException(404, "授权记录不存在")
    db.delete(g)
    db.commit()
    return {"ok": True}
