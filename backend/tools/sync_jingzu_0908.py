# -*- coding: utf-8 -*-
"""周二竞彩 002-012 (9-9 凌晨开球): 按 league+date 拉 API 精确匹配 → 入库(category=jingzu)
002雅典AEKvsLASK(UCL) 003布鲁日vs维拉(UCL) 004奈梅亨vs精英(荷甲) 005卡德西亚vs吉达国民(沙)
006南安普敦vs斯旺西(英冠) 007桑德兰vs赫尔城(英联杯L48) 008皇马vs国米(UCL) 009多特vs黄潜(UCL)
010里尔vs贝蒂斯(UCL) 011波尔图vs曼城(UCL) 012弗鲁米嫩塞vs普拉滕斯(解放者杯)
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

# (编号-2, league_id, home 精确名, away 精确名)
TARGETS = [
    (2, 2,  "AEK Athens FC",       "Lask Linz"),
    (3, 2,  "Club Brugge KV",      "Aston Villa"),
    (4, 88, "NEC Nijmegen",        "Excelsior"),
    (5, 307, "Al-Qadisiyah FC",    "Al-Ahli Jeddah"),
    (6, 40, "Southampton",         "Swansea"),
    (7, 48, "Sunderland",          "Hull City"),
    (8, 2,  "Real Madrid",         "Inter"),
    (9, 2,  "Borussia Dortmund",   "Villarreal"),
    (10, 2, "Lille",               "Real Betis"),
    (11, 2, "FC Porto",            "Manchester City"),
    (12, 13, "Fluminense",         "Platense"),
]
DATES = ["2026-09-08", "2026-09-09"]

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
        for date in DATES:
            try:
                data = api_get("/fixtures", {"league": lid, "season": 2026, "date": date})
            except Exception as e:
                print(f"L{lid} {date} API失败 {e}", flush=True)
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
        print(f"OK {idx:03d} [{it['fixture']['id']}] "
              f"{it['teams']['home']['name']} vs {it['teams']['away']['name']} "
              f"({it['league']['name']} {it['fixture']['date']})", flush=True)
    missing = [i for i in [x[0] for x in TARGETS] if i not in found]
    if missing:
        print(f"MISSING: {missing}", flush=True)
    else:
        print("ALL_11_MATCHED", flush=True)
finally:
    db.close()
