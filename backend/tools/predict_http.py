# -*- coding: utf-8 -*-
"""批量预测 —— 只走 HTTP（POST /api/predict/{id}），禁 import predict_fixture / 直连 DB。

与 predict_batch.py 的区别：那个是老的直连版（违反 [[接口调用铁律]]），
这个走 uvicorn 的 HTTP 接口，和用户在网页上点的是同一条路径。

用法：
  .venv/bin/python tools/predict_http.py --date 2026-09-19          # 读 jc_map.json 全跑
  .venv/bin/python tools/predict_http.py --date 2026-09-19 --only 周六020
  .venv/bin/python tools/predict_http.py --ids 1550128 1550135
  .venv/bin/python tools/predict_http.py --date 2026-09-19 --force   # 已预测的也重跑
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "http://127.0.0.1:8000"
MAP_FILE = Path(__file__).resolve().parent / "jc_map.json"

_print_lock = threading.Lock()
_counter = {"done": 0, "ok": 0, "fail": 0, "skip": 0}


def http_json(url, method="GET", timeout=600):
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": "predict_http"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def already_predicted(date):
    """已经预测过的 fixture_id 集合（分页全量）。"""
    ids, page = set(), 1
    while True:
        d = http_json(f"{BASE}/api/predictions?date={date}&page={page}&page_size=100")
        data = d.get("data") or []
        for x in data:
            fid = x.get("fixture_id") or (x.get("fixture") or {}).get("id")
            if fid:
                ids.add(int(fid))
        if len(ids) >= d.get("total", 0) or not data:
            return ids
        page += 1


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def run_one(fid, label, date, force, pre):
    with _print_lock:
        _counter["done"] += 1
        n = _counter["done"]
    if not force and fid in pre:
        with _print_lock:
            _counter["skip"] += 1
        log(f"[{n}] SKIP  {label} ({fid}) 已预测")
        return
    t0 = time.time()
    try:
        r = http_json(f"{BASE}/api/predict/{fid}", method="POST")
        el = time.time() - t0
        with _print_lock:
            _counter["ok"] += 1
        res = r.get("result") or {}
        llm = res.get("llm") or {}
        tag = ""
        if llm.get("home_win_rate") is not None:
            tag = (f"  LLM 主{llm.get('home_win_rate')}% 平{llm.get('draw_rate')}% "
                   f"客{llm.get('away_win_rate')}%")
        log(f"[{n}] OK    {label} ({fid})  {el:.0f}s{tag}")
    except urllib.error.HTTPError as e:
        with _print_lock:
            _counter["fail"] += 1
        detail = e.read().decode()[:200]
        log(f"[{n}] FAIL  {label} ({fid})  HTTP {e.code}: {detail}")
    except Exception as e:
        with _print_lock:
            _counter["fail"] += 1
        log(f"[{n}] FAIL  {label} ({fid})  {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--ids", nargs="*", type=int, default=None)
    ap.add_argument("--only", nargs="*", default=None, help="只看这些体彩编号，如 周六020")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()

    if a.ids:
        targets = [(fid, str(fid)) for fid in a.ids]
        date = a.date
    else:
        raw = json.loads(MAP_FILE.read_text(encoding="utf-8"))
        date = a.date or raw.get("date")
        if date != raw.get("date"):
            sys.exit(f"jc_map.json 是 {raw.get('date')}，与 --date {date} 不符，先重建映射")
        targets = [(fid, num) for num, fid in raw["map"].items()
                   if not a.only or num in a.only]
        targets.sort(key=lambda x: x[1])

    if not targets:
        sys.exit("没有目标场次")

    pre = already_predicted(date) if date else set()
    log(f"目标 {len(targets)} 场，已预测 {len(pre)} 场，并发 {a.workers}\n")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fid, label in targets:
            ex.submit(run_one, fid, label, date, a.force, pre)

    el = time.time() - t0
    log(f"\n完成 {_counter['ok']} 成功 / {_counter['fail']} 失败 / "
        f"{_counter['skip']} 跳过，共 {el:.0f}s")
    if a.date or date:
        log(f"核对：curl '{BASE}/api/predictions?date={date}&page_size=100'")


if __name__ == "__main__":
    main()
