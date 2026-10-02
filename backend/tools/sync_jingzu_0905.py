# -*- coding: utf-8 -*-
"""周六批次 001-003 (9-5 18:00 开球, J2×2 + K1×1): 按 league+date 拉 API 匹配队名 → 入库(category=jingzu)
001福冈黄蜂vs水户蜀葵(J2) 002山形山神vs甲府风林(J2) 003济州SKvs蔚山现代(K1)
"""
import sys, os, time, json, subprocess, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
from app.db.session import SessionLocal
from app.services.fixture_service import _upsert_fixture
from sqlalchemy import text

API_KEY = os.environ["API_FOOTBALL_KEY"]
BASE = os.environ["API_FOOTBALL_BASE_URL"]
HEADERS = {"x-apisports-key": API_KEY}

# 期望场次: (league_id, (home 关键字列表, away 关键字列表))
TARGETS = [
    (99, (["fukuoka"], ["mito"])),              # 001 J2 福冈vs水户
    (99, (["yamagata"], ["ventforet", "kofu"])),  # 002 J2 山形vs甲府
    (292, (["jeju"], ["ulsan"])),               # 003 K1 济州vs蔚山
]

def api_get(path, params):
    last = None
    for attempt in range(6):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=15)
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-m", "25", "-H",
                          f"x-apisports-key: {API_KEY}", url],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout)
    raise last

def match_item(it, lid, idx_expected):
    # 只匹配本 TARGETS 条目: idx_expected 的对手对上了才算
    idx, (lid_, (hk, ak)) = idx_expected, TARGETS[idx_expected]
    if lid_ != lid:
        return None
    hn = (it["teams"]["home"]["name"] or "").lower()
    an = (it["teams"]["away"]["name"] or "").lower()
    if any(k in hn for k in hk) and any(k in an for k in ak):
        return idx
    return None

def discover_korea():
    """K1 league id 兜底: 用 team search 找济州/蔚山的今日比赛"""
    out = {}
    for team_key, i in (("jeju", 2), ("ulsan", 2)):
        data = api_get("/teams", {"search": team_key})
        teams = data.get("response") or []
        for t in teams:
            tid = t["team"]["id"]
            fdata = api_get("/fixtures", {"team": tid, "from": "2026-09-05", "to": "2026-09-05"})
            for it in fdata.get("response") or []:
                idx = match_item(it, it["league"]["id"], i)
                if idx is not None:
                    out[idx] = it
    return out

db = SessionLocal()
found = {}
try:
    # 主路径: J2=99 / K1=292
    for idx, (lid, (hk, ak)) in enumerate(TARGETS):
        data = api_get("/fixtures", {"league": lid, "season": 2026, "date": "2026-09-05"})
        for it in data.get("response") or []:
            if match_item(it, lid, idx) is not None:
                found[idx] = it
                break
        time.sleep(0.3)
    if 2 not in found:
        found.update(discover_korea())
    for idx in sorted(found):
        it = found[idx]
        _upsert_fixture(db, it)
        db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"),
                   {"fid": it["fixture"]["id"]})
        db.commit()
        print(f"OK {idx+1:02d} [{it['fixture']['id']}] "
              f"{it['teams']['home']['name']} vs {it['teams']['away']['name']} "
              f"({it['league']['name']} {it['fixture']['date']})", flush=True)
    missing = [i + 1 for i in range(len(TARGETS)) if i not in found]
    if missing:
        print(f"MISSING: {missing}", flush=True)
    else:
        print("ALL_3_MATCHED", flush=True)
finally:
    db.close()
