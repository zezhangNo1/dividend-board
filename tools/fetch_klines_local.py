# -*- coding: utf-8 -*-
"""本机拉取策略实验室 K 线缓存（腾讯 proxy.finance.qq.com 源，分页拼接 2015 至今）。

接口行为（实测 2026-10）：单次最多 800 根，返回「以区间 end 为终点」的最近 N 根；
所以倒序分页（end=上一页最早日期）往前翻，按日期去重合并。
经验：python urllib TLS 指纹会被识别，必须 curl 子进程；间隔 ≥1.5s。

用法：
  python3 tools/fetch_klines_local.py /tmp/lab_codes.json /tmp/lab_klines.json            # 全量
  python3 tools/fetch_klines_local.py /tmp/lab_codes.json /tmp/lab_klines.json --resume   # 续拉缺失
输出 {code: [[date, adj, raw, volume], ...]}，导入：
  docker exec -i board-api python -m app.lab import < /tmp/lab_klines.json
"""
import json
import os
import subprocess
import sys
import time

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
BEG = "2015-01-01"
END = "2026-12-31"
PAGE = 800


def sym(code):
    if code == "000300":
        return "sh000300"  # 沪深300 指数在上证段
    return ("sh" if code.startswith(("6", "5")) else "sz") + code


def _get(s, fqt, end):
    if fqt == 1:
        url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?"
               f"param={s},day,{BEG},{end},{PAGE},qfq")
    else:
        url = (f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/kline?"
               f"param={s},day,{BEG},{end},{PAGE}")
    r = subprocess.run(["curl", "-s", "-m", "25", "-A", UA, url], capture_output=True)
    try:
        data = (json.loads(r.stdout.decode()).get("data") or {}).get(s) or {}
        rows = data.get("qfqday") or data.get("day") or []
        return [[p[0], float(p[2]), float(p[5] if len(p) > 5 else 0)] for p in rows if len(p) >= 3]
    except Exception:
        return []


LITE = "--lite" in sys.argv  # 每只单页 800 根（≈3.3 年），够布林日/周/月用

def kline(code, fqt):
    """倒序分页拉全史（--lite 时单页）。返回按日期升序 [[date, close, volume]]。"""
    s, merged, end = sym(code), {}, END
    for _ in range(1 if LITE else 6):  # 2015 至今约 2900 根，4 页够，留余量
        rows = _get(s, fqt, end)
        if not rows:
            break
        new = 0
        for r in rows:
            if r[0] not in merged:
                merged[r[0]] = r
                new += 1
        first = min(r[0] for r in rows)
        if new == 0 or first <= BEG:
            break
        end = first
        time.sleep(1.4)
    return [merged[d] for d in sorted(merged)]


def main(codes_path, out_path, resume=False):
    spec = json.load(open(codes_path))
    codes = spec["missing"] if isinstance(spec, dict) and spec.get("missing") else (
        spec["all"] if isinstance(spec, dict) else spec)
    if isinstance(spec, dict) and spec.get("missing"):
        codes = spec["missing"]
    payload = {}
    if resume and os.path.exists(out_path):
        payload = json.load(open(out_path))
        codes = [c for c in codes if c not in payload]
        print(f"resume: 已有 {len(payload)}，待拉 {len(codes)}")
    blocked, fails = 0, []
    for i, c in enumerate(codes, 1):
        adj = kline(c, 1)
        time.sleep(1.5)
        if not adj:
            blocked += 1
            print(f"[{i}/{len(codes)}] {c} 空响应（{blocked}/3）")
            if blocked >= 3:
                print("连续被拦，中止；稍后 --resume 续拉")
                break
            time.sleep(4)
            continue
        raw = kline(c, 0)
        time.sleep(1.5)
        raw_map = {r[0]: r[1] for r in raw}
        payload[c] = [[a[0], a[1], raw_map.get(a[0], a[1]), a[2]] for a in adj]
        print(f"[{i}/{len(codes)}] {c} {len(adj)} bars ({adj[0][0]}~{adj[-1][0]})")
        json.dump(payload, open(out_path, "w"), ensure_ascii=False)
    json.dump(payload, open(out_path, "w"), ensure_ascii=False)
    print(f"DONE 共 {len(payload)} 只 -> {out_path}；失败: {fails}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--resume" in sys.argv)
