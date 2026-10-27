"""到股息率提醒：扫描自选股档位穿越，邮件通知。

档位：上穿 5.5%（深度价值区）/ 上穿 5.0%（进入关注区）。
冷却：同一 (用户, 股票, 档位) 7 天内不重复提醒。
行情：腾讯 qt.gtimg.cn（GBK）；DPS：宿主 site_data.json（compose 已挂载只读）。
"""
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user
from .channels import send_raw
from .db import get_db
from .models import AlertEvent, User, WatchItem

router = APIRouter(prefix="/api", tags=["alerts"])

TIERS = [("5.5", 5.5, "深度价值区"), ("5.0", 5.0, "进入关注区")]
COOLDOWN_DAYS = 7
SITE_DATA_PATH = os.environ.get("SITE_DATA_PATH", "/srv/site_data.json")


def now():
    return datetime.now(timezone.utc)


def load_dps_map() -> dict:
    """code -> (name, dps)"""
    try:
        with open(SITE_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {r["code"]: (r["name"], r.get("dps")) for r in data.get("rows", [])}
    except Exception:
        return {}


def fetch_quotes(codes: list[str]) -> dict:
    """code -> {'price':float,'name':str}（腾讯行情，GBK）"""
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        keys = [(("sh" if c[0] == "6" else "sz") + c) for c in batch]
        try:
            r = httpx.get("https://qt.gtimg.cn/q=" + ",".join(keys),
                          headers={"Referer": "https://gu.qq.com/",
                                   "User-Agent": "Mozilla/5.0"}, timeout=12)
            for line in r.content.decode("gbk", "ignore").split("\n"):
                if '="' not in line:
                    continue
                p = line.split('="')[1].rstrip('";\r\n').split("~")
                if len(p) < 49:
                    continue
                try:
                    price = float(p[3])
                except Exception:
                    continue
                if price <= 0:
                    continue
                out[p[2]] = {"price": price, "name": p[1].replace(" ", "")}
        except Exception:
            continue
    return out


def scan_alerts(db: Session, dry: bool = False) -> dict:
    dps_map = load_dps_map()
    items = db.scalars(select(WatchItem)).all()
    if not items:
        return {"scanned": 0, "notified": 0}
    users = {u.id: u for u in db.scalars(select(User)).all()}
    quotes = fetch_quotes(sorted({i.code for i in items}))

    # 触发集合: (uid, code) -> [(tier_label, dy, price, name)]
    trig: dict[tuple, list] = {}
    for it in items:
        user = users.get(it.user_id)
        if not user or user.status != "active" or not user.email:
            continue
        q = quotes.get(it.code)
        name, dps = dps_map.get(it.code, (None, None))
        if not q or not dps:
            continue
        dy = dps / q["price"] * 100
        for tlabel, trate, tname in TIERS:
            if dy < trate:
                continue
            recent = db.scalar(select(AlertEvent).where(
                AlertEvent.user_id == it.user_id,
                AlertEvent.code == it.code,
                AlertEvent.tier == tlabel,
                AlertEvent.created_at > now() - timedelta(days=COOLDOWN_DAYS),
            ))
            if recent:
                continue
            db.add(AlertEvent(user_id=it.user_id, code=it.code, tier=tlabel,
                              price=round(q["price"], 3), dy=round(dy, 2)))
            trig.setdefault((it.user_id, user.email), []).append(
                (name or q["name"], it.code, tlabel, tname, round(dy, 2), round(q["price"], 2)))

    # ---- 除息提醒：自选股未来除息 T-7 / T-1 ----
    try:
        from .calendar_mod import refresh_calendar
        from .models import DivCalendar
        refresh_calendar(db)
        today = datetime.now().date()
        cal = db.scalars(select(DivCalendar).where(
            DivCalendar.ex_date >= today.isoformat(),
            DivCalendar.ex_date <= (today + timedelta(days=8)).isoformat())).all()
        cal_map = {c.code: c for c in cal}
        for it in items:
            user = users.get(it.user_id)
            if not user or user.status != "active" or not user.email:
                continue
            c = cal_map.get(it.code)
            if not c:
                continue
            days_left = (datetime.strptime(c.ex_date, "%Y-%m-%d").date() - today).days
            if days_left not in (7, 1):
                continue
            ev_key = f"exdiv:{c.ex_date}"
            recent = db.scalar(select(AlertEvent).where(
                AlertEvent.user_id == it.user_id,
                AlertEvent.code == it.code,
                AlertEvent.tier == ev_key,
            ))
            if recent:
                continue
            name, _ = dps_map.get(it.code, (c.name, None))
            db.add(AlertEvent(user_id=it.user_id, code=it.code, tier=ev_key,
                              price=0, dy=days_left))
            trig.setdefault((it.user_id, user.email), []).append(
                (name or c.name, it.code, ev_key,
                 f"{'7' if days_left == 7 else '1'} 天后除息", round(c.per, 4), days_left))
    except Exception:
        pass  # 除息数据不可得时不影响股息率提醒

    sent = 0
    for (uid, email), rows in trig.items():
        dy_rows = [r for r in rows if not r[2].startswith("exdiv:")]
        ex_rows = [r for r in rows if r[2].startswith("exdiv:")]
        lines = []
        if dy_rows:
            lines.append("您自选的股票有股息率档位提醒：")
            lines.append("")
            for nm, code, tlabel, tname, dy, price in dy_rows:
                lines.append(f"· {nm}({code}) 现价 {price}，股息率 {dy}%，已达 {tlabel}% 档（{tname}）")
                lines.append(f"  {tlabel}% 参考价 = 每股分红 ÷ {tlabel}%，详见看板：https://board.zekes.top")
        if ex_rows:
            lines.append("")
            lines.append("您自选的股票即将除息：")
            for nm, code, _, tname, per, _d in ex_rows:
                lines.append(f"· {nm}({code}) {tname}，每股派息 {per} 元（税前）")
                lines.append("  除息日股价会相应调减，留意分红到账时间")
        if not lines:
            continue
        lines += ["", "数据来自公开行情接口，仅供参考，不构成投资建议。",
                  "股息率档位 7 天内不重复提醒；除息提醒每个除息日只发一次。"]
        if dry:
            print("[dry-run] 会发给", email, ":\n" + "\n".join(lines))
            sent += 1
            continue
        try:
            send_raw("【泽克的数据笔记】自选股息率/除息提醒 " +
                      "、".join(sorted({r[0] for r in rows})), "\n".join(lines), email)
            sent += 1
        except Exception:
            pass
    db.commit()
    return {"scanned": len(items), "notified": sent}


@router.post("/cron/scan-alerts")
def cron_scan(x_cron_secret: str = Header(default=""), dry: bool = False,
              db: Session = Depends(get_db)):
    secret = os.environ.get("CRON_SECRET", "")
    if not secret or x_cron_secret != secret:
        raise HTTPException(403, "forbidden")
    return scan_alerts(db, dry=dry)


@router.get("/alerts")
def my_alerts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(AlertEvent).where(
        AlertEvent.user_id == user.id,
    ).order_by(AlertEvent.id.desc()).limit(20)).all()
    dps_map = load_dps_map()
    return {"items": [{
        "code": r.code, "name": dps_map.get(r.code, ("",))[0] or r.code,
        "tier": r.tier, "price": r.price, "dy": r.dy,
        "at": r.created_at.isoformat(),
    } for r in rows]}
