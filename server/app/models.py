"""数据模型：用户、验证码、自选、功能开关、AI 日报。"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 双通道：email / phone 至少其一，均唯一可空
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    nickname: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|banned
    tier: Mapped[str] = mapped_column(String(16), default="free")      # free|plus|pro
    role: Mapped[str] = mapped_column(String(16), default="user")      # admin|self|user
    password_hash: Mapped[str | None] = mapped_column(String(128))     # bcrypt，可空
    token_ver: Mapped[int] = mapped_column(Integer, default=0)          # 改密后+1使旧会话失效
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    watchlist: Mapped[list["WatchItem"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AlertEvent(Base):
    """股息率提醒触发记录（用于冷却与历史展示）。"""
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code: Mapped[str] = mapped_column(String(12), index=True)
    tier: Mapped[str] = mapped_column(String(8))            # "5.5" / "5.0"
    price: Mapped[float] = mapped_column(Float)             # 触发时现价
    dy: Mapped[float] = mapped_column(Float)                # 触发时股息率%
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LoginFail(Base):
    """密码登录失败记录，用于防爆破锁定。"""
    __tablename__ = "login_fails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target: Mapped[str] = mapped_column(String(255), index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OtpCode(Base):
    __tablename__ = "otp_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(8))          # email|phone
    target: Mapped[str] = mapped_column(String(255), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WatchItem(Base):
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code: Mapped[str] = mapped_column(String(12), index=True)  # 股票代码
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    user: Mapped["User"] = relationship(back_populates="watchlist")


class Feature(Base):
    """功能注册表：每个功能一行，layer 决定默认可见范围，status 是一键总开关。

    新功能默认 self 层（只对 admin/self 角色与显式授权账号可见），
    表述合规验证后再手动切 public。
    """
    __tablename__ = "features"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    layer: Mapped[str] = mapped_column(String(8), default="self")    # public|member|self
    status: Mapped[str] = mapped_column(String(8), default="on")     # on|off
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class FeatureGrant(Base):
    """把某个 self 层功能单独授权（或拒绝）给某个账号。"""
    __tablename__ = "feature_grants"
    __table_args__ = (UniqueConstraint("user_id", "feature_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    allow: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 空=永久
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FeatureAccessLog(Base):
    """self 层功能的访问留痕：证明功能从未对公众开放。"""
    __tablename__ = "feature_access_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AiDaily(Base):
    """AI 每日观察：一天一条，重新生成即覆盖（按 trade_date 唯一）。"""
    __tablename__ = "ai_daily"
    __table_args__ = (UniqueConstraint("trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_date: Mapped[str] = mapped_column(String(10), index=True)  # 数据日期 YYYY-MM-DD
    data_pack: Mapped[str] = mapped_column(Text)                     # 喂给模型的确定性数据包
    content_md: Mapped[str] = mapped_column(Text)                    # 模型输出的 Markdown
    model: Mapped[str] = mapped_column(String(64), default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(8), default="ok")     # ok|fallback
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Kline(Base):
    """日线缓存：adj=前复权收盘（总收益口径），raw=不复权收盘（价格口径）。"""
    __tablename__ = "kline"
    __table_args__ = (UniqueConstraint("code", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), index=True)
    date: Mapped[str] = mapped_column(String(10))
    adj: Mapped[float] = mapped_column(Float)
    raw: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)


class StockDividend(Base):
    """每股分红历史（已实施，按除息日），供回测还原历史股息率。"""
    __tablename__ = "dividends"
    __table_args__ = (UniqueConstraint("code", "ex_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), index=True)
    ex_date: Mapped[str] = mapped_column(String(10))
    per: Mapped[float] = mapped_column(Float)  # 每股派息（元，税前）
