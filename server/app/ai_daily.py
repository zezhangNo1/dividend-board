"""AI 每日观察：确定性代码算数据 → 便宜模型写解读 → 禁用词过滤 → 落库。

数据源：站内 site_data.json（服务器每日 18:35 由 update.sh 刷新，收盘口径），
AI 只做解读、不产生任何数字（数据先行原则，压幻觉）。

合规：
  - 输出过禁用词表（买卖/仓位建议类），命中先重试一次，仍命中则降级为纯数据简报
  - 页面固定 AIGC 标识 + 免责声明（前端 ai.html）
"""
import json
import os
import re
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .features import require_admin, require_feature
from .models import AiDaily, User

router = APIRouter(prefix="/api/ai/daily", tags=["ai-daily"])

SITE_DATA_PATH = os.environ.get("SITE_DATA_PATH", "/srv/site_data.json")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
LLM_MODEL_CHEAP = os.environ.get("LLM_MODEL_CHEAP", "glm-4-flash")

# 输出禁用词：任何买卖/仓位/价格建议类表述（命中即重写，再命中降级）
BANNED = [
    "买入", "卖出", "加仓", "重仓", "减仓", "补仓", "抄底", "清仓", "满仓",
    "目标价", "推荐", "上车", "入手", "止损", "止盈", "建议持有", "值得买",
    "可以买", "逢低", "建仓", "调仓", "仓位",
]

SYSTEM_PROMPT = """你是高股息投资研究频道的日报撰写助手。你会收到一份由确定性代码计算好的 JSON 数据包，你的任务是把数据写成一份客观、克制的中文研究日报。

铁律：
1. 只描述数据包里已有的事实，禁止编造任何数字，禁止预测涨跌方向。
2. 禁止出现任何买卖、仓位、价格建议类表述（这些词一律不许出现：买入/卖出/加仓/重仓/减仓/补仓/抄底/建仓/目标价/推荐/上车/止损/止盈/仓位）。
3. 语气像研究笔记，不像营销号；不确定的地方就说不确定。

输出为 Markdown，结构固定为四节，全文 250-400 字：
## 一句话总结
（今天红利池整体表现一句话，含数据日期）
## 板块全景
（涨幅前三和后三板块各一句话概括，点出领涨领跌的标的，不逐个罗列数字）
## 今日焦点
（从数据里挑 2-3 个最值得注意的现象：大跌大涨、股息率进入 5.5% 以上区域、即将除息等）
## 数据速览
（3-4 行关键数字：涨跌家数、平均股息率、5.5% 以上标的数量、最高股息率标的）"""


# ---------------------------------------------------------------- 数据包
def build_data_pack(path: str = SITE_DATA_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    rows = [r for r in raw.get("rows", []) if r.get("price")]
    gen = raw.get("generated", "")
    today = date.today()

    up = sum(1 for r in rows if (r.get("chg") or 0) > 0)
    down = sum(1 for r in rows if (r.get("chg") or 0) < 0)

    # 板块聚合
    sectors: dict[str, list] = {}
    for r in rows:
        sectors.setdefault(r.get("sector") or "其他", []).append(r)
    sector_stats = []
    for name, items in sectors.items():
        avg = sum(i.get("chg") or 0 for i in items) / len(items)
        best = max(items, key=lambda i: i.get("chg") or 0)
        worst = min(items, key=lambda i: i.get("chg") or 0)
        sector_stats.append({
            "板块": name, "平均涨跌%": round(avg, 2), "标的数": len(items),
            "领涨": f"{best['name']}({best.get('chg')}%)",
            "领跌": f"{worst['name']}({worst.get('chg')}%)",
        })
    sector_stats.sort(key=lambda s: s["平均涨跌%"], reverse=True)

    movers = sorted(rows, key=lambda r: r.get("chg") or 0)
    fmt = lambda r: {"名称": r["name"], "代码": r["code"], "涨跌%": r.get("chg"),
                     "现价": r.get("price"), "股息率%": r.get("dy")}
    losers, gainers = [fmt(r) for r in movers[:5]], [fmt(r) for r in movers[-5:][::-1]]

    # 股息率结构
    by_dy = sorted([r for r in rows if r.get("dy")], key=lambda r: r["dy"], reverse=True)
    deep = [fmt(r) for r in by_dy if r["dy"] >= 5.5]
    avg_dy = round(sum(r["dy"] for r in by_dy) / len(by_dy), 2) if by_dy else 0

    # 14 天内将除息（数据里有未来除息日才出现，没有就不编）
    exdiv = []
    for r in rows:
        for d in r.get("divs") or []:
            try:
                ex = datetime.strptime(str(d.get("exd", "")), "%Y-%m-%d").date()
            except ValueError:
                continue
            if today <= ex <= today + timedelta(days=14):
                exdiv.append({"名称": r["name"], "代码": r["code"], "除息日": str(ex),
                              "每股派息": d.get("per")})

    return {
        "数据截至": gen,
        "标的总数": len(rows),
        "上涨家数": up, "下跌家数": down,
        "平均股息率%": avg_dy,
        "股息率≥5.5%标的数": len(deep),
        "股息率前8": [fmt(r) for r in by_dy[:8]],
        "股息率5.5%以上": deep[:12],
        "板块统计": sector_stats,
        "跌幅前5": losers, "涨幅前5": gainers,
        "未来14天除息": exdiv,
    }


# ---------------------------------------------------------------- LLM
def call_llm(pack: dict) -> tuple[str, int]:
    """返回 (markdown, tokens)。失败抛异常，由上层降级。"""
    resp = httpx.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": LLM_MODEL_CHEAP,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "数据包如下（JSON）：\n" + json.dumps(pack, ensure_ascii=False, indent=1)},
            ],
            "temperature": 0.3,
            "max_tokens": 1200,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"].strip()
    tokens = data.get("usage", {}).get("total_tokens", 0)
    return content, int(tokens)


def banned_hits(text: str) -> list[str]:
    return [w for w in BANNED if w in text]


FALLBACK_TMPL = """## 一句话总结
{asof} 收盘，红利池 {n} 只标的中 {up} 涨 {down} 跌，平均股息率 {avg}%。

## 板块全景
涨幅前三：{top3}；跌幅前三：{bot3}。

## 今日焦点
{focus}

## 数据速览
股息率 5.5% 以上标的 {deep} 只；最高为 {topdy}。{exdiv}
"""


def fallback_markdown(pack: dict) -> str:
    s = pack["板块统计"]
    top3 = "、".join(f"{x['板块']}({x['平均涨跌%']}%)" for x in s[:3])
    bot3 = "、".join(f"{x['板块']}({x['平均涨跌%']}%)" for x in s[-3:][::-1])
    focus_lines = []
    if pack["跌幅前5"]:
        g = pack["跌幅前5"][0]
        focus_lines.append(f"当日跌幅最大：{g['名称']} {g['涨跌%']}%（股息率 {g['股息率%']}%）")
    if pack["股息率前8"]:
        t = pack["股息率前8"][0]
        focus_lines.append(f"股息率最高：{t['名称']} {t['股息率%']}%")
    if pack["未来14天除息"]:
        e = pack["未来14天除息"][0]
        focus_lines.append(f"最近将除息：{e['名称']}，除息日 {e['除息日']}")
    return FALLBACK_TMPL.format(
        asof=pack["数据截至"], n=pack["标的总数"], up=pack["上涨家数"], down=pack["下跌家数"],
        avg=pack["平均股息率%"], top3=top3, bot3=bot3,
        focus="；".join(focus_lines) or "当日无显著异动。",
        deep=pack["股息率≥5.5%标的数"],
        topdy=f"{pack['股息率前8'][0]['名称']} {pack['股息率前8'][0]['股息率%']}%" if pack["股息率前8"] else "—",
        exdiv=f"未来 14 天有 {len(pack['未来14天除息'])} 只标的除息。" if pack["未来14天除息"] else "",
    )


# ---------------------------------------------------------------- 生成入口
def generate(db: Session) -> AiDaily:
    pack = build_data_pack()
    trade_date = (pack["数据截至"] or "")[:10] or str(date.today())

    content, model, tokens, status = "", "", 0, "fallback"
    if LLM_API_KEY:
        for attempt in range(2):  # 命中禁用词重试一次
            try:
                content, tokens = call_llm(pack)
            except Exception as e:
                print(f"[ai_daily] LLM 调用失败: {e}")
                break
            hits = banned_hits(content)
            if not hits:
                model, status = LLM_MODEL_CHEAP, "ok"
                break
            print(f"[ai_daily] 第{attempt + 1}次输出命中禁用词: {hits}")
            content = ""
    if status != "ok":
        content, model, tokens = fallback_markdown(pack), "", 0

    row = db.scalar(select(AiDaily).where(AiDaily.trade_date == trade_date))
    if row:
        row.data_pack = json.dumps(pack, ensure_ascii=False)
        row.content_md, row.model, row.tokens, row.status = content, model, tokens, status
    else:
        row = AiDaily(trade_date=trade_date,
                      data_pack=json.dumps(pack, ensure_ascii=False),
                      content_md=content, model=model, tokens=tokens, status=status)
        db.add(row)
    db.commit()
    print(f"[ai_daily] {trade_date} 生成完成 status={status} model={model} tokens={tokens}")
    return row


# ---------------------------------------------------------------- 读接口
@router.get("/latest")
def latest(user: User | None = Depends(require_feature("ai.daily")),
           db: Session = Depends(get_db)):
    row = db.scalar(select(AiDaily).order_by(AiDaily.trade_date.desc()).limit(1))
    if not row:
        return {"ok": False, "message": "今日报告生成中，请稍后再看"}
    return {"ok": True, "trade_date": row.trade_date, "content_md": row.content_md,
            "model": row.model, "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None}


@router.get("/list")
def list_days(user: User | None = Depends(require_feature("ai.daily")),
              db: Session = Depends(get_db)):
    rows = db.scalars(select(AiDaily).order_by(AiDaily.trade_date.desc()).limit(30)).all()
    return {"days": [{"trade_date": r.trade_date, "status": r.status} for r in rows]}


@router.get("/{trade_date}")
def by_date(trade_date: str, user: User | None = Depends(require_feature("ai.daily")),
            db: Session = Depends(get_db)):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
        return {"ok": False, "message": "日期格式不正确"}
    row = db.scalar(select(AiDaily).where(AiDaily.trade_date == trade_date))
    if not row:
        return {"ok": False, "message": "该日无报告"}
    return {"ok": True, "trade_date": row.trade_date, "content_md": row.content_md,
            "model": row.model, "status": row.status}


@router.post("/generate")
def trigger_generate(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = generate(db)
    return {"ok": True, "trade_date": row.trade_date, "status": row.status,
            "model": row.model, "tokens": row.tokens}


if __name__ == "__main__":
    # cron 入口：docker exec board-api python -m app.ai_daily
    from .db import SessionLocal
    generate(SessionLocal())
