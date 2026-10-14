"""自选股接口：登录用户维护自选列表，免费档上限 5 只。"""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user
from .db import get_db
from .models import User, WatchItem

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

FREE_LIMIT = int(os.environ.get("WATCH_FREE_LIMIT", "5"))
CODE_RE_MIN, CODE_RE_MAX = 6, 6


class AddReq(BaseModel):
    code: str
    note: str | None = None


@router.get("")
def list_items(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(WatchItem).where(
        WatchItem.user_id == user.id).order_by(WatchItem.id)).all()
    return {"items": [
        {"id": r.id, "code": r.code, "note": r.note,
         "created_at": r.created_at.isoformat()} for r in rows
    ], "limit": FREE_LIMIT if user.tier == "free" else None}


@router.post("")
def add_item(req: AddReq, user: User = Depends(current_user), db: Session = Depends(get_db)):
    code = req.code.strip()
    if not (code.isdigit() and len(code) == CODE_RE_MIN):
        raise HTTPException(400, "股票代码格式不正确")
    exists = db.scalar(select(WatchItem).where(
        WatchItem.user_id == user.id, WatchItem.code == code))
    if exists:
        raise HTTPException(409, "已在自选中")
    if user.tier == "free":
        count = len(db.scalars(select(WatchItem).where(
            WatchItem.user_id == user.id)).all())
        if count >= FREE_LIMIT:
            raise HTTPException(403, f"免费版最多自选 {FREE_LIMIT} 只")
    item = WatchItem(user_id=user.id, code=code, note=req.note)
    db.add(item)
    db.commit()
    return {"ok": True, "id": item.id}


@router.delete("/{item_id}")
def del_item(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.get(WatchItem, item_id)
    if not item or item.user_id != user.id:
        raise HTTPException(404, "条目不存在")
    db.delete(item)
    db.commit()
    return {"ok": True}
