"""AI 个股报告：确定性代码算数据 → 强模型解读 → 禁用词过滤 → 落库缓存。

token 保护（重要）：
  - 同一 (code, 交易日) 命中当日缓存直接返回，force=1 才重新生成；
  - 功能走 features 门禁（当前 self 层 = 仅站长/显式授权账号）；
  - 非 admin 用户按月限额（AI_REPORT_MONTHLY_LIMIT，默认 30 次/月）。

合规：输出过禁用词表（与 ai_daily 同表），命中重试一次，仍命中降级为纯数据模板；
前端展示固定 AIGC 标识 + 免责声明。
"""
import json
import os
import re
import statistics
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .alerts import fetch_quotes
from .db import get_db
from .features import require_feature
from .models import AiReport, Kline, StockDividend, User
from .tech import _bands, _position, _resample, _month_ends
from .lab import trailing_dps

router = APIRouter(prefix="/api/ai/report", tags=["ai-report"])

SITE_DATA_PATH = os.environ.get("SITE_DATA_PATH", "/srv/site_data.json")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
LLM_MODEL_STRONG = os.environ.get("LLM_MODEL_STRONG", "glm-4-plus")
MONTHLY_LIMIT = int(os.environ.get("AI_REPORT_MONTHLY_LIMIT", "30"))
TARGETS = [5.5, 5.0, 4.5, 4.0]

BANNED = [
    "买入", "卖出", "加仓", "重仓", "减仓", "补仓", "抄底", "清仓", "满仓",
    "目标价", "推荐", "上车", "入手", "止损", "止盈", "建议持有", "值得买",
    "可以买", "逢低", "建仓", "调仓", "仓位",
]

SYSTEM_PROMPT = """你是高股息投资研究频道的研究员。你会收到一份由确定性代码计算好的单只股票 JSON 数据包（分红、股息率档位、估值、区间与技术位置、历史股息率分位、除息记录）。你的任务是把它写成一份客观、克制的中文研究简报。

铁律：
1. 只描述数据包里已有的事实，禁止编造任何数字，禁止预测涨跌方向，禁止给出任何买卖/仓位/价格建议。
2. 禁止出现这些词：买入/卖出/加仓/重仓/减仓/补仓/抄底/建仓/目标价/推荐/上车/止损/止盈/仓位。
3. 语气像研究笔记：给事实、给口径、给风险提示；不确定的地方直说数据不足。
4. 数据包里标注"数据不足"的部分，明确写"数据积累中"，不要推测。

输出 Markdown，结构固定四节，全文 400-600 字：
## 分红档案速读
（这只票是谁：板块/市值/现价/股息率处在什么档位；每股分红与近12个月派息节奏；历年已实施的分红记录）
## 当前位置
（四档股息率参考价与现价的距离；近一年区间位置；布林带日线/周/月位置；历史股息率分位——把它翻译成"比历史上多少比例的月份更高"）
## 风险与注意
（3-4 条，只基于数据：周期属性、分位含义、估值水平、即将除息、数据口径限制等）
## 数据速览
（6-8 行关键数字原文照列：现价/涨跌/股息率/DPS/PE/PB/52周区间/分位）"""


# ---------------------------------------------------------------- 数据包
def _load_universe() -> dict:
    with open(SITE_DATA_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return {r["code"]: r for r in raw.get("rows", [])}, raw.get("generated", "")


def _tech_pack(db: Session, code: str) -> dict:
    rows = db.scalars(select(Kline).where(Kline.code == code).order_by(Kline.date)).all()
    closes = [r.raw for r in rows if r.raw and r.raw > 0]
    out = {"布林": {}, "K线截至": rows[-1].date if rows else None}
    if len(closes) >= 25:
        b = _bands(closes[-260:])
        if b:
            label, pb = _position(closes[-1], b)
            out["布林"]["日线"] = {"位置": label, "带宽位置%": round(pb * 100)}
        for mode, key in (("W", "周线"), ("M", "月线")):
            series = _resample(rows, mode)
            b = _bands(series)
            if b:
                label, pb = _position(series[-1], b)
                out["布林"][key] = {"位置": label, "带宽位置%": round(pb * 100)}
    return out


def _yield_pack(db: Session, code: str) -> dict:
    krows = db.scalars(select(Kline).where(Kline.code == code).order_by(Kline.date)).all()
    divs = db.scalars(select(StockDividend).where(
        StockDividend.code == code).order_by(StockDividend.ex_date)).all()
    pairs = [(d.ex_date, d.per) for d in divs]
    if not krows or not pairs:
        return {"状态": "数据积累中"}
    pts = []
    for r in _month_ends(krows):
        if not r.raw or r.raw <= 0:
            continue
        dps = trailing_dps(pairs, r.date)
        if dps > 0:
            pts.append((r.date[:7], round(dps / r.raw * 100, 3)))
    if len(pts) < 24:
        return {"状态": "数据积累中（不足两年）"}
    med0 = statistics.median(v for _, v in pts)
    pts = [p for p in pts if med0 / 3 <= p[1] <= med0 * 3]
    if len(pts) < 24:
        return {"状态": "数据积累中（不足两年）"}
    vals = sorted(v for _, v in pts)
    cur = pts[-1][1]
    pct = round(sum(1 for v in vals if v <= cur) / len(vals) * 100)

    def q(p):
        return round(vals[min(len(vals) - 1, int(len(vals) * p))], 2)
    return {
        "当前月度股息率%": round(cur, 2),
        "历史分位%": pct,
        "P25": q(0.25), "中位数": q(0.5), "P75": q(0.75),
        "区间最低%": round(vals[0], 2), "区间最高%": round(vals[-1], 2),
        "样本月数": len(pts), "样本区间": f"{pts[0][0]} ~ {pts[-1][0]}",
        "口径": "近12个月已实施派息 ÷ 月末不复权收盘",
    }


def build_stock_pack(db: Session, code: str) -> tuple[dict, str]:
    uni, gen = _load_universe()
    r = uni.get(code)
    if not r:
        raise HTTPException(404, "标的不存在")
    q = fetch_quotes([code]).get(code, {})
    price = q.get("price") or r.get("price")
    dps = r.get("dps")
    dy = round(dps / price * 100, 2) if (dps and price) else None
    pack = {
        "数据截至": gen,
        "基本档案": {
            "名称": r["name"], "代码": code, "板块": r.get("sector"),
            "总市值亿": r.get("mktcap"), "现价": price,
            "当日涨跌%": q.get("chg"),
        },
        "分红档案": {
            "每股分红DPS": dps, "财年": r.get("fy"),
            "近12个月派息次数": r.get("times12"),
            "股息率%（现价口径）": dy,
            "已实施分红记录": [
                {"类型": "年度" if d.get("mmdd") == "12-31" else "中期",
                 "除息日": d.get("exd"), "每股派息": d.get("per")}
                for d in (r.get("divs") or [])
            ],
        },
        "股息率参考价（DPS÷目标股息率的数学换算，非建议）": {},
        "估值与区间": {
            "PE": r.get("pe"), "PB": r.get("pb"),
            "52周最高": r.get("hi"), "52周最低": r.get("lo"),
            "近一年区间位置%": (r.get("pct") if r.get("pct") is not None else None),
        },
    }
    if q.get("price"):
        pack["基本档案"]["当日涨跌%"] = None
    for t in TARGETS:
        if dps:
            p = round(dps / (t / 100), 2)
            gap = round((price - p) / price * 100, 1) if price else None
            pack["股息率参考价（DPS÷目标股息率的数学换算，非建议）"][f"{t}%档"] = {
                "参考价": p,
                "现价距该档": ("已低 " + str(abs(gap)) + "%") if (gap is not None and gap <= 0)
                            else (f"高 {gap}%" if gap is not None else None),
            }
    pack["技术位置"] = _tech_pack(db, code)
    pack["历史股息率分位"] = _yield_pack(db, code)
    return pack, gen


# ---------------------------------------------------------------- LLM 与降级
def call_llm(pack: dict) -> tuple[str, int, str]:
    resp = httpx.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": LLM_MODEL_STRONG,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "数据包如下（JSON）：\n" + json.dumps(pack, ensure_ascii=False, indent=1)},
            ],
            "temperature": 0.4,
            "max_tokens": 1800,
            # 推理模型会把额度先花在思考上导致正文为空，简报不需要深推理
            "thinking": {"type": "disabled"},
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    content = (data["choices"][0]["message"].get("content") or "").strip()
    if not content:
        raise RuntimeError("模型返回正文为空（可能被思考耗尽 max_tokens）")
    tokens = data.get("usage", {}).get("total_tokens", 0)
    return content, int(tokens), LLM_MODEL_STRONG


def banned_hits(text: str) -> list[str]:
    return [w for w in BANNED if w in text]


def fallback_md(pack: dict) -> str:
    b, d, v = pack["基本档案"], pack["分红档案"], pack["估值与区间"]
    divs = "；".join(
        f"{x['类型']} {x['除息日']} 每股 {x['每股派息']} 元" for x in d["已实施分红记录"][:6]
    ) or "无已实施记录"
    tiers = pack.get("股息率参考价（DPS÷目标股息率的数学换算，非建议）", {})
    tier_lines = "\n".join(
        f"- {k}：参考价 {x['参考价']} 元（现价{x['现价距该档']}）" for k, x in tiers.items()
    ) or "- 数据不足"
    y = pack["历史股息率分位"]
    yline = (f"当前 {y['当前月度股息率%']}%，历史分位 {y['历史分位%']}%（P25 {y['P25']} / 中位 {y['中位数']} / P75 {y['P75']}，样本 {y['样本月数']} 个月）"
             if y.get("当前月度股息率%") is not None else "历史数据积累中")
    t = pack.get("技术位置", {}).get("布林", {})
    tline = "；".join(f"{k} {x['位置']}" for k, x in t.items()) or "数据积累中"
    return f"""## 分红档案速读
{b['名称']}（{b['代码']}，{b['板块']}，市值 {b.get('总市值亿') or '—'} 亿）现价 {b.get('现价') or '—'} 元，股息率 {d['股息率%（现价口径）'] or '—'}%。每股分红 {d['每股分红DPS']} 元（FY{d['财年']}），近 12 个月派息 {d['近12个月派息次数'] or 0} 次。已实施分红：{divs}。

## 当前位置
四档股息率参考价（数学换算，非建议）：
{tier_lines}
近一年区间 {v.get('52周最低')}~{v.get('52周最高')}，位置 {v.get('近一年区间位置%')}%；布林带：{tline}。
历史股息率分位：{yline}。

## 风险与注意
- 各档参考价仅为股息率换算的数学参考，不构成任何投资建议。
- 历史股息率分位受样本区间与脏价过滤影响，仅作客观统计描述。
- 分红数据按季更新，以最近已实施财年为口径，可能滞后公告。

## 数据速览
- 现价 {b.get('现价') or '—'} · 涨跌 {b.get('当日涨跌%') if b.get('当日涨跌%') is not None else '—'}%
- 股息率 {d['股息率%（现价口径）'] or '—'}% · DPS {d['每股分红DPS']} · 近12月派 {d['近12个月派息次数'] or 0} 次
- PE {v.get('PE') or '—'} · PB {v.get('PB') or '—'} · 市值 {b.get('总市值亿') or '—'} 亿
- 数据截至 {pack['数据截至']}（本简报为数据模板降级版，未调用模型）"""


# ---------------------------------------------------------------- 接口
class ReportReq(BaseModel):
    code: str
    force: bool = False


@router.post("")
def create_report(req: ReportReq, user: User = Depends(require_feature("ai.report")),
                  db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(404, "Not Found")
    code = req.code.strip()
    if not (code.isdigit() and len(code) == 6):
        raise HTTPException(400, "股票代码格式不正确")
    today = str(date.today())

    cached = db.scalar(select(AiReport).where(
        AiReport.code == code, AiReport.trade_date == today
    ).order_by(AiReport.id.desc()).limit(1))
    if cached and not req.force:
        return _row_json(cached, cached=True)

    # 非 admin 月限额（admin 不限，靠当日缓存兜 token）
    if user.role != "admin":
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        used = len(db.scalars(select(AiReport).where(
            AiReport.user_id == user.id,
            AiReport.created_at >= month_start)).all())
        if used >= MONTHLY_LIMIT:
            raise HTTPException(403, f"本月额度已用完（{MONTHLY_LIMIT} 次/月）")

    pack, gen = build_stock_pack(db, code)
    uni, _ = _load_universe()
    name = uni[code]["name"]

    content, model, tokens, status = "", "", 0, "fallback"
    if LLM_API_KEY:
        for attempt in range(2):
            try:
                content, tokens, model = call_llm(pack)
            except Exception as e:
                print(f"[ai_report] LLM 调用失败: {e}")
                break
            hits = banned_hits(content)
            if not hits:
                status = "ok"
                break
            print(f"[ai_report] 第{attempt + 1}次输出命中禁用词: {hits}")
            content = ""
    if status != "ok":
        content, model, tokens = fallback_md(pack), "", 0

    row = AiReport(user_id=user.id, code=code, name=name, trade_date=today,
                   content_md=content, model=model, tokens=tokens, status=status)
    db.add(row)
    db.commit()
    return _row_json(row, cached=False)


@router.get("/{code}")
def latest_report(code: str, user: User = Depends(require_feature("ai.report")),
                  db: Session = Depends(get_db)):
    row = db.scalar(select(AiReport).where(
        AiReport.code == code).order_by(AiReport.id.desc()).limit(1))
    if not row:
        raise HTTPException(404, "该股暂无报告")
    return _row_json(row, cached=True)


# ---------------------------------------------------------------- 流式生成（SSE）
def call_llm_stream(pack: dict):
    """yield 增量正文片段；全部片段拼起来 = 完整报告。失败抛异常由上层降级。"""
    import json as _json
    with httpx.stream(
        "POST",
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": LLM_MODEL_STRONG,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "数据包如下（JSON）：\n" + _json.dumps(pack, ensure_ascii=False, indent=1)},
            ],
            "temperature": 0.4,
            "max_tokens": 1800,
            "stream": True,
            "thinking": {"type": "disabled"},
        },
        timeout=httpx.Timeout(300, connect=15),
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = _json.loads(payload)
            except ValueError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = (choices[0].get("delta") or {})
            piece = delta.get("content") or ""
            if piece:
                yield piece
            usage = obj.get("usage")
            if usage:
                yield ("__usage__", usage.get("total_tokens", 0))


@router.get("/stream/{code}")
def stream_report(code: str, user: User = Depends(require_feature("ai.report")),
                  db: Session = Depends(get_db)):
    """SSE：事件 {"t": "meta"|"delta"|"done"|"banned"|"error", ...}。当日缓存直接整段吐出。"""
    from fastapi.responses import StreamingResponse
    import asyncio

    if not user:
        raise HTTPException(404, "Not Found")
    if not (code.isdigit() and len(code) == 6):
        raise HTTPException(400, "股票代码格式不正确")
    today = str(date.today())

    def sse(obj) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def gen():
        # 1) 当日缓存：整段返回
        cached = db.scalar(select(AiReport).where(
            AiReport.code == code, AiReport.trade_date == today
        ).order_by(AiReport.id.desc()).limit(1))
        if cached:
            yield sse({"t": "meta", "model": cached.model or "数据模板", "cached": True})
            yield sse({"t": "delta", "c": cached.content_md})
            yield sse({"t": "done", "cached": True, "trade_date": cached.trade_date,
                       "status": cached.status, "tokens": cached.tokens})
            return

        # 2) 限额（非 admin）
        if user.role != "admin":
            month_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0)
            used = len(db.scalars(select(AiReport).where(
                AiReport.user_id == user.id,
                AiReport.created_at >= month_start)).all())
            if used >= MONTHLY_LIMIT:
                yield sse({"t": "error", "message": f"本月额度已用完（{MONTHLY_LIMIT} 次/月）"})
                return

        # 3) 数据包 → 流式生成 → 禁用词在线检查
        try:
            pack, gen_date = build_stock_pack(db, code)
        except HTTPException as e:
            yield sse({"t": "error", "message": e.detail})
            return
        uni, _ = _load_universe()
        name = uni[code]["name"]
        yield sse({"t": "meta", "model": LLM_MODEL_STRONG, "cached": False})

        acc, tokens, ok = "", 0, False
        if LLM_API_KEY:
            try:
                for piece in call_llm_stream(pack):
                    if isinstance(piece, tuple):
                        tokens = piece[1]
                        continue
                    acc += piece
                    hits = banned_hits(acc)
                    if hits:
                        yield sse({"t": "banned", "words": hits})
                        acc = ""
                        break
                    yield sse({"t": "delta", "c": piece})
                else:
                    ok = bool(acc.strip())
            except Exception as e:
                print(f"[ai_report] 流式调用失败: {e}")
        if ok and banned_hits(acc):
            ok = False
        if not ok:
            content, model, tokens, status = fallback_md(pack), "", 0, "fallback"
            yield sse({"t": "delta", "c": content})
        else:
            content, model, status = acc, LLM_MODEL_STRONG, "ok"

        row = AiReport(user_id=user.id, code=code, name=name, trade_date=today,
                       content_md=content, model=model, tokens=tokens, status=status)
        db.add(row)
        db.commit()
        yield sse({"t": "done", "cached": False, "trade_date": today,
                   "status": status, "tokens": tokens, "model": model})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _row_json(row: AiReport, cached: bool) -> dict:
    return {"ok": True, "code": row.code, "name": row.name,
            "trade_date": row.trade_date, "content_md": row.content_md,
            "model": row.model or "数据模板", "tokens": row.tokens,
            "status": row.status, "cached": cached,
            "created_at": row.created_at.isoformat() if row.created_at else None}
