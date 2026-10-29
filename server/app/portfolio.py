"""持仓体检：纯规则计算，不调 LLM（零 token）。

输入持仓代码列表 → 输出逐票档案 + 组合聚合（股息率/档位/板块集中度/周期占比/
估值中位/未来除息）+ 规则化提示。所有数字来自 site_data 快照与行情接口，
仅作客观统计描述，不构成投资建议。
"""
import json
import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .alerts import fetch_quotes
from .db import get_db
from .features import require_feature
from .models import User

router = APIRouter(prefix="/api/checkup", tags=["portfolio-checkup"])

SITE_DATA_PATH = os.environ.get("SITE_DATA_PATH", "/srv/site_data.json")
MAX_HOLDINGS = 20
# 周期属性板块（分红随盈利大幅波动的典型板块，口径保守）
CYCLICAL_SECTORS = {"能源电力", "有色化工", "煤炭", "钢铁"}


class CheckupReq(BaseModel):
    codes: list[str]


def _zone(dy):
    if dy is None:
        return "数据待补"
    if dy >= 5.5:
        return "深度价值区"
    if dy >= 5.0:
        return "关注区"
    if dy >= 4.5:
        return "观察区"
    return "中性"


@router.post("")
def checkup(req: CheckupReq, user: User = Depends(require_feature("portfolio.checkup")),
            db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(404, "Not Found")
    codes, seen = [], set()
    for c in req.codes:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    if not codes:
        raise HTTPException(400, "请至少输入一个持仓代码")
    if len(codes) > MAX_HOLDINGS:
        raise HTTPException(400, f"一次最多 {MAX_HOLDINGS} 只")

    with open(SITE_DATA_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    uni = {r["code"]: r for r in raw.get("rows", [])}
    gen = raw.get("generated", "")

    missing = [c for c in codes if c not in uni]
    codes = [c for c in codes if c in uni]
    if not codes:
        raise HTTPException(400, "持仓代码均不在标的中：" + "、".join(missing))

    quotes = fetch_quotes(codes)
    today = date.today()

    rows, sector_count, zone_count, cyclical_n = [], {}, {}, 0
    dy_list, pe_list, exdiv_soon = [], [], []
    for c in codes:
        r = uni[c]
        q = quotes.get(c, {})
        price = q.get("price") or r.get("price")
        dps = r.get("dps")
        dy = round(dps / price * 100, 2) if (dps and price) else None
        zone = _zone(dy)
        rows.append({
            "code": c, "name": r["name"], "sector": r.get("sector"),
            "price": price, "chg": q.get("chg"),
            "dy": dy, "zone": zone,
            "dps": dps, "pe": r.get("pe"), "pb": r.get("pb"),
            "pct": r.get("pct"), "mktcap": r.get("mktcap"),
        })
        if dy is not None:
            dy_list.append(dy)
            zone_count[zone] = zone_count.get(zone, 0) + 1
        if r.get("pe"):
            pe_list.append(r["pe"])
        sec = r.get("sector") or "其他"
        sector_count[sec] = sector_count.get(sec, 0) + 1
        if sec in CYCLICAL_SECTORS:
            cyclical_n += 1
        for d in (r.get("divs") or []):
            try:
                ex = datetime.strptime(str(d.get("exd", "")), "%Y-%m-%d").date()
            except ValueError:
                continue
            if today <= ex <= today + timedelta(days=30):
                exdiv_soon.append({"name": r["name"], "ex_date": str(ex), "per": d.get("per")})

    n = len(codes)
    top_sector, top_n = max(sector_count.items(), key=lambda kv: kv[1])
    top_share = round(top_n / n * 100)
    cyclical_share = round(cyclical_n / n * 100)
    avg_dy = round(sum(dy_list) / len(dy_list), 2) if dy_list else None
    med_pe = sorted(pe_list)[len(pe_list) // 2] if pe_list else None

    # 全池平均股息率（对照基准）
    pool_dy = [r.get("dy") for r in uni.values() if r.get("dy")]
    pool_avg = round(sum(pool_dy) / len(pool_dy), 2) if pool_dy else None

    notes = []
    if top_share >= 50:
        notes.append(f"板块集中：{top_sector} 占持仓 {top_share}%（{top_n}/{n}），单一板块波动对整体股息影响较大。")
    if cyclical_share >= 40:
        notes.append(f"周期类板块（能源电力/有色化工等）占 {cyclical_share}%：周期股分红金额随盈利大幅波动，股息率在盈利拐点处可能失真。")
    low = [x["name"] for x in rows if x["dy"] is not None and x["dy"] < 4.0]
    if low:
        notes.append("股息率低于 4%：" + "、".join(low) + "——按本站口径暂不属高股息区间。")
    deep = [x["name"] for x in rows if x["dy"] is not None and x["dy"] >= 5.5]
    if deep:
        notes.append("处于深度价值区（股息率 ≥5.5%）：" + "、".join(deep) + "。")
    if avg_dy is not None and pool_avg is not None:
        pos = "高于" if avg_dy > pool_avg else "低于"
        notes.append(f"组合平均股息率 {avg_dy}%，{pos}全池平均（{pool_avg}%，等权口径）。")
    if exdiv_soon:
        notes.append(f"未来 30 天内除息 {len(exdiv_soon)} 笔："
                     + "、".join(f"{e['name']} {e['ex_date']}" for e in exdiv_soon) + "。")
    if not notes:
        notes.append("未触发任何集中度/周期/档位提示规则。")

    return {
        "ok": True, "generated": gen, "count": n,
        "missing": missing,
        "rows": rows,
        "aggregate": {
            "avg_dy": avg_dy, "pool_avg_dy": pool_avg,
            "zone_count": zone_count,
            "top_sector": {"name": top_sector, "count": top_n, "share%": top_share},
            "cyclical_share%": cyclical_share,
            "median_pe": med_pe,
            "exdiv_30d": exdiv_soon,
        },
        "notes": notes,
        "disclaimer": "本体检为纯数据规则的客观统计描述，不构成任何投资建议；等权口径，未考虑持仓数量差异。",
    }
