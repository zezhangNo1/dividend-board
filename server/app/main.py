"""高股息看板 Phase 1 后端入口。"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import router as auth_router
from .db import Base, engine
from .password_auth import router as password_router
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


@app.on_event("startup")
def init_db():
    Base.metadata.create_all(engine)
    # create_all 不加列：手动幂等迁移
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(128)"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_ver INTEGER DEFAULT 0"))


@app.get("/api/health")
def health():
    return {"ok": True, "service": "board-api"}
