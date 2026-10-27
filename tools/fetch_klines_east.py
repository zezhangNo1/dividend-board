# -*- coding: utf-8 -*-
"""东财全历史 K 线抓取（整段一次返回，无 800 根限制）。

用途：修正腾讯分页源对部分代码返回的错误历史价格。
东财现为间歇性放行（软限流）：每请求最多重试 4 次、间隔 2.5s，失败代码最后补一轮。
用法：
  python3 tools/fetch_klines_east.py codes.json out.json [--resume]
  codes.json: {"all":[...]} 或 ["...", ...]
导入：docker exec -i board-api python -m app.lab import < out.json
"""
import json
import os
import subprocess
import sys
import time

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def secid(code):
    if code == "000300":
        return "1.000300"
    return ("1." if code.startswith(("6", "5")) else "0.") + code


def fetch(code, fqt, tries=4):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid(code)}&klt=101&fqt={fqt}&beg=20130101&end=20500101"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f53,f56")
    for t in range(tries):
        r = subprocess.run(["curl", "-s", "-m", "25", "-A", UA,
                            "-e", "https://quote.eastmoney.com/", url],
                           capture_output=True)
        try:
            data = (json.loads(r.stdout.decode()) or {}).get("data") or {}
            out = []
            for row in data.get("klines") or []:
                p = row.split(",")
                if len(p) >= 2 and float(p[1]) > 0:
                    out.append([p[0], float(p[1]), float(p[2] or 0)])
            if out:
                return out
        except Exception:
            pass
        time.sleep(4 + t * 3)
    return None


def main(codes_path, out_path, resume):
    spec = json.load(open(codes_path))
    codes = spec["all"] if isinstance(spec, dict) and spec.get("all") else (
        spec["missing"] if isinstance(spec, dict) and spec.get("missing") else spec)
    payload = {}
    if resume and os.path.exists(out_path):
        payload = json.load(open(out_path))
        codes = [c for c in codes if c not in payload]
        print(f"resume: 已有 {len(payload)}，待拉 {len(codes)}")
    fails = []
    for i, c in enumerate(codes, 1):
        adj = fetch(c, 1)
        time.sleep(2.5)
        raw = fetch(c, 0)
        time.sleep(2.5)
        if not adj or not raw:
            fails.append(c)
            print(f"[{i}/{len(codes)}] {c} FAIL")
            continue
        raw_map = {r[0]: r[1] for r in raw}
        payload[c] = [[a[0], a[1], raw_map.get(a[0], a[1]), a[2]] for a in adj]
        print(f"[{i}/{len(codes)}] {c} {len(adj)} bars ({adj[0][0]}~{adj[-1][0]})")
        if i % 6 == 0:
            json.dump(payload, open(out_path, "w"), ensure_ascii=False)
    # 失败补一轮
    if fails:
        print("补拉失败代码:", fails)
        for c in list(fails):
            adj = fetch(c, 1)
            time.sleep(3)
            raw = fetch(c, 0)
            time.sleep(3)
            if adj and raw:
                raw_map = {r[0]: r[1] for r in raw}
                payload[c] = [[a[0], a[1], raw_map.get(a[0], a[1]), a[2]] for a in adj]
                print(f"补拉成功 {c}")
        json.dump(payload, open(out_path, "w"), ensure_ascii=False)
    json.dump(payload, open(out_path, "w"), ensure_ascii=False)
    left = [c for c in codes if c not in payload]
    print(f"DONE {len(payload)}/{len(codes) + len(payload)} -> {out_path}；仍失败: {left}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--resume" in sys.argv)
