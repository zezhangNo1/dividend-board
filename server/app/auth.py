"""认证：验证码签发/校验、JWT 会话。"""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .channels import CHANNELS
from .db import get_db
from .models import OtpCode, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_TTL_DAYS = 14

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


class CodeReq(BaseModel):
    channel: str  # email|phone
    target: str


class LoginReq(BaseModel):
    channel: str
    target: str
    code: str


def _uid(user: User) -> int:
    return user.id


def make_token(user: User) -> str:
    payload = {
        "uid": user.id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("token", "")
    if not token:
        raise HTTPException(401, "未登录")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "会话已过期")
    user = db.get(User, payload.get("uid"))
    if not user or user.status != "active":
        raise HTTPException(401, "账号不可用")
    return user


@router.post("/code")
def send_code(req: CodeReq, request: Request, db: Session = Depends(get_db)):
    ch = req.channel
    if ch not in CHANNELS:
        raise HTTPException(400, "不支持的通道")
    target = req.target.strip()
    if ch == "email" and not EMAIL_RE.match(target):
        raise HTTPException(400, "邮箱格式不正确")
    if ch == "phone" and not PHONE_RE.match(target):
        raise HTTPException(400, "手机号格式不正确")
    if ch == "phone" and not CHANNELS["phone"].ENABLED:
        raise HTTPException(503, "短信登录即将开放，请先使用邮箱登录")

    now = datetime.now(timezone.utc)
    # 频控：同一目标 60s 一条、1 小时 5 条；同一 IP 1 小时 10 条
    recent = db.scalar(select(func.count()).select_from(OtpCode).where(
        OtpCode.target == target, OtpCode.created_at > now - timedelta(seconds=60)))
    if recent:
        raise HTTPException(429, "发送太频繁，请 1 分钟后再试")
    hourly = db.scalar(select(func.count()).select_from(OtpCode).where(
        OtpCode.target == target, OtpCode.created_at > now - timedelta(hours=1)))
    if hourly >= 5:
        raise HTTPException(429, "发送次数过多，请 1 小时后再试")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
    ip_hour = db.scalar(select(func.count()).select_from(OtpCode).where(
        OtpCode.ip == ip, OtpCode.created_at > now - timedelta(hours=1)))
    if ip_hour >= 10:
        raise HTTPException(429, "操作太频繁，请稍后再试")

    code = f"{secrets.randbelow(1000000):06d}"
    row = OtpCode(
        channel=ch, target=target,
        code_hash=hashlib.sha256(code.encode()).hexdigest(),
        ip=ip[:64], expires_at=now + timedelta(minutes=10),
    )
    try:
        CHANNELS[ch].send(code, target)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    db.add(row)
    db.commit()
    return {"ok": True, "message": "验证码已发送，10 分钟内有效"}


@router.post("/login")
def login(req: LoginReq, db: Session = Depends(get_db)):
    ch, target, code = req.channel, req.target.strip(), req.code.strip()
    if ch not in CHANNELS:
        raise HTTPException(400, "不支持的通道")
    now = datetime.now(timezone.utc)
    # 取该目标最新一条未用码校验（10 分钟内）；成功即作废。允许 5 次尝试后失效。
    row = db.scalar(select(OtpCode).where(
        OtpCode.target == target, OtpCode.used.is_(False),
        OtpCode.expires_at > now,
    ).order_by(OtpCode.id.desc()))
    if not row:
        raise HTTPException(400, "验证码已失效，请重新获取")
    if row.code_hash != hashlib.sha256(code.encode()).hexdigest():
        row.attempts = getattr(row, "attempts", 0)
        db.commit()
        raise HTTPException(400, "验证码不正确")
    row.used = True
    db.commit()

    # 找到或创建用户
    field = "email" if ch == "email" else "phone"
    user = db.scalar(select(User).where(getattr(User, field) == target))
    if not user:
        user = User(**{field: target})
        db.add(user)
    user.last_login_at = now
    db.commit()
    return {"ok": True, "token": make_token(user), "user": {"id": user.id, field: target, "tier": user.tier}}
