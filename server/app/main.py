"""高股息看板 Phase 1 后端入口。"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import router as auth_router
from .db import Base, engine
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
app.include_router(watch_router)


@app.on_event("startup")
def init_db():
    Base.metadata.create_all(engine)


@app.get("/api/health")
def health():
    return {"ok": True, "service": "board-api"}
