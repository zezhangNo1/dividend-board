"""高股息看板 Phase 1 后端入口。"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .ads import router as ads_router
from .ai_daily import router as ai_daily_router
from .calendar_mod import router as calendar_router
from .auth import router as auth_router
from .db import Base, engine
from .features import router as features_router, ensure_seed
from .lab import router as lab_router
from .alerts import router as alerts_router
from .password_auth import router as password_router
from .tech import router as tech_router
from .tracking import router as tracking_router
from .watchlist import router as watch_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="board-api", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(password_router)
app.include_router(watch_router)
app.include_router(alerts_router)
app.include_router(features_router)
app.include_router(ai_daily_router)
app.include_router(lab_router)
app.include_router(tracking_router)
app.include_router(ads_router)
app.include_router(calendar_router)
app.include_router(tech_router)


@app.on_event("startup")
def init_db():
    Base.metadata.create_all(engine)
    # create_all 不加列：手动幂等迁移
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(128)"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_ver INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(16) DEFAULT 'user'"))
        conn.execute(text("ALTER TABLE ad_events ALTER COLUMN etype TYPE VARCHAR(8)"))
        conn.execute(text("ALTER TABLE alert_events ALTER COLUMN tier TYPE VARCHAR(24)"))
        conn.execute(text("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS tiers VARCHAR(64)"))
    # 功能注册表补种（已存在的不动）
    from .db import SessionLocal
    db = SessionLocal()
    try:
        ensure_seed(db)
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"ok": True, "service": "board-api"}
