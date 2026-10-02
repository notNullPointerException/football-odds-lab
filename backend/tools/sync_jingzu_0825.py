# -*- coding: utf-8 -*-
"""周二批次(8-26) 9场: 同步fixtures + 标记jingzu + 拉伤停"""
import sys, os, time, json
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

# 001金泉vs全北 002济州vs浦项 003达曼vs利雅胜利 004斯托克vs赫尔 005林茨vs凯尔特人
# 006博德闪耀vs奈梅亨 007诺丁汉vs利兹 008伯明翰vs布伦特 009巴伦西亚vs贝蒂斯
FIXTURE_IDS = [1507040, 1507041, 1602994, 1623097, 1610924,
               1622627, 1623100, 1623082, 1570342]

def api_get(path, params):
    last = None
    for attempt in range(5):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=10)
            return r.json()
        except Exception as e:
            last = e
            print(f"   retry {attempt+1}: {e}")
            time.sleep(3 * (attempt + 1))
    raise last

db = SessionLocal()
injuries = {}
try:
    for fid in FIXTURE_IDS:
        data = api_get("/fixtures", {"id": fid})
        items = data.get("response", []) or []
        if not items:
            print(f"[!] {fid} 未找到")
            continue
        it = items[0]
        home, away = it["teams"]["home"]["name"], it["teams"]["away"]["name"]
        _upsert_fixture(db, it)
        db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"), {"fid": fid})
        db.commit()
        print(f"OK {fid} {home} vs {away}")
        try:
            inj_data = api_get("/injuries", {"fixture": fid})
            inj_items = inj_data.get("response", []) or []
            injuries[str(fid)] = inj_items
            print(f"   伤停 {len(inj_items)} 条")
        except Exception as e:
            print(f"   伤停失败: {e}")
        time.sleep(1)
    with open("/tmp/inj_0825.json", "w", encoding="utf-8") as f:
        json.dump(injuries, f, ensure_ascii=False)
    print("DONE")
finally:
    db.close()