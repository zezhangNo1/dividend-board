"""密码认证：密码登录、设置/修改密码、验证码重置密码、账户信息。"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .auth import EMAIL_RE, JWT_SECRET, current_user, make_token
from .db import get_db
from .models import LoginFail, OtpCode, User

router = APIRouter(prefix="/api", tags=["password"])

FAIL_WINDOW = timedelta(minutes=15)
FAIL_MAX = 5


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def _check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except Exception:
        return False


def _valid_password(pw: str) -> bool:
    return isinstance(pw, str) and 8 <= len(pw) <= 64


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "") or
            (request.client.host if request.client else ""))[:64]


class PwLoginReq(BaseModel):
    email: str
    password: str


class SetPwReq(BaseModel):
    old_password: str | None = None
    new_password: str


class ResetPwReq(BaseModel):
    email: str
    code: str
    new_password: str


@router.post("/auth/password-login")
def password_login(req: PwLoginReq, request: Request, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    if not EMAIL_RE.match(email) or not req.password:
        raise HTTPException(400, "邮箱或密码格式不正确")
    now = datetime.now(timezone.utc)
    ip = _client_ip(request)

    count = len(db.scalars(select(LoginFail).where(
        LoginFail.target == email, LoginFail.created_at > now - FAIL_WINDOW)).all())
    if count >= FAIL_MAX:
        raise HTTPException(429, "失败次数过多，账号已锁定 15 分钟")

    user = db.scalar(select(User).where(User.email == email))
    ok = user is not None and user.password_hash and _check_password(req.password, user.password_hash)
    if not ok:
        db.add(LoginFail(target=email, ip=ip))
        db.commit()
        left = FAIL_MAX - count - 1
        raise HTTPException(401, f"邮箱或密码不正确" + (f"（再错 {left} 次将锁定 15 分钟）" if left > 0 else "（已锁定）"))
    # 登录成功：清失败记录
    db.execute(delete(LoginFail).where(LoginFail.target == email))
    user.last_login_at = now
    db.commit()
    return {"ok": True, "token": make_token(user),
            "user": {"id": user.id, "email": user.email, "phone": user.phone, "tier": user.tier}}


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from .features import user_feature_keys
    return {"id": user.id, "email": user.email, "phone": user.phone,
            "tier": user.tier, "role": user.role,
            "has_password": bool(user.password_hash),
            "features": user_feature_keys(db, user)}


@router.post("/auth/set-password")
def set_password(req: SetPwReq, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not _valid_password(req.new_password):
        raise HTTPException(400, "新密码需 8-64 位")
    if user.password_hash:
        if not req.old_password or not _check_password(req.old_password, user.password_hash):
            raise HTTPException(401, "当前密码不正确")
    user.password_hash = _hash_password(req.new_password)
    user.token_ver += 1  # 旧会话全部失效
    db.commit()
    return {"ok": True, "token": make_token(user), "message": "密码已设置，请使用新会话"}


@router.post("/auth/reset-password")
def reset_password(req: ResetPwReq, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    if not _valid_password(req.new_password):
        raise HTTPException(400, "新密码需 8-64 位")
    now = datetime.now(timezone.utc)
    row = db.scalar(select(OtpCode).where(
        OtpCode.target == email, OtpCode.used.is_(False),
        OtpCode.expires_at > now,
    ).order_by(OtpCode.id.desc()))
    if not row:
        raise HTTPException(400, "验证码已失效，请重新获取")
    if row.code_hash != hashlib.sha256(req.code.strip().encode()).hexdigest():
        raise HTTPException(400, "验证码不正确")
    row.used = True
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        raise HTTPException(404, "该邮箱尚未注册")
    user.password_hash = _hash_password(req.new_password)
    user.token_ver += 1
    db.commit()
    return {"ok": True, "message": "密码已重置，请用新密码登录"}
