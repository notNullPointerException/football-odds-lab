# -*- coding: utf-8 -*-
"""周一竞彩 001-009 (9-8 凌晨开球): 按 league+date 拉 API 精确匹配队名 → 入库(category=jingzu)
001卡利亚里vs莱切(I) 002赫塔费vs塞尔塔(S) 003马尔默vsAIK(SE) 004卡尔马vs佐加顿斯(SE)
005利雅得新月vs新未来(SA) 006乌迪内斯vs拉齐奥(I) 007埃斯托里尔vs阿罗卡(P) 008埃尔切vs皇社(S)
009维多利亚vs格雷米奥(BR)
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

# (编号-1, league_id, home 精确名, away 精确名) 名字已在 discovery 验证
TARGETS = [
    (0, 135, "Cagliari",           "Lecce"),
    (1, 140, "Getafe",             "Celta Vigo"),
    (2, 113, "Malmo FF",           "AIK Stockholm"),
    (3, 113, "Kalmar FF",          "Djurgardens IF"),
    (4, 307, "Al-Hilal Saudi FC",  "NEOM"),
    (5, 135, "Udinese",            "Lazio"),
    (6, 94,  "Estoril",            "Arouca"),
    (7, 140, "Elche",              "Real Sociedad"),
    (8, 71,  "Vitoria",            "Gremio"),
]
DATE = "2026-09-07"

def api_get(path, params):
    last = None
    for attempt in range(5):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=15)
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1)
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-m", "20", "-H",
                          f"x-apisports-key: {API_KEY}", url],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout)
    raise last

db = SessionLocal()
found = {}
try:
    leagues = sorted({t[1] for t in TARGETS})
    for lid in leagues:
        try:
            data = api_get("/fixtures", {"league": lid, "season": 2026, "date": DATE})
        except Exception as e:
            print(f"L{lid} API失败 {e}", flush=True)
            continue
        for it in data.get("response") or []:
            hn = (it["teams"]["home"]["name"] or "").lower()
            an = (it["teams"]["away"]["name"] or "").lower()
            for idx, lid2, hname, aname in TARGETS:
                if idx in found or lid2 != lid:
                    continue
                if hn == hname.lower() and an == aname.lower():
                    found[idx] = it
        time.sleep(0.25)
    for idx in sorted(found):
        it = found[idx]
        _upsert_fixture(db, it)
        db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"),
                   {"fid": it["fixture"]["id"]})
        db.commit()
        print(f"OK {idx+1:03d} [{it['fixture']['id']}] "
              f"{it['teams']['home']['name']} vs {it['teams']['away']['name']} "
              f"({it['league']['name']} {it['fixture']['date']})", flush=True)
    missing = [i + 1 for i in range(len(TARGETS)) if i not in found]
    if missing:
        print(f"MISSING: {missing}", flush=True)
    else:
        print("ALL_9_MATCHED", flush=True)
finally:
    db.close()