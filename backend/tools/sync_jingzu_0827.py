# -*- coding: utf-8 -*-
"""周四批次(8-28凌晨) 9场: 同步fixtures + 标记jingzu + 拉伤停 + 预测"""
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

# 001比尔森vs贝红星 002奥莫尼亚vs圣图尔登 003萨尔茨堡vs米亚尔比 004奥胡斯vs本菲卡
# 005费伦茨vs特拉布宗 006安德莱vs阿拉木图 007塞尔塔vs奥萨苏纳 008巴萨vs毕尔巴鄂 009巴西国际vs格雷米奥
FIXTURE_IDS = [1622633, 1623449, 1623450, 1623441, 1623445,
               1623442, 1570336, 1570335, 1623070]

def api_get(path, params):
    last = None
    for attempt in range(5):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=15)
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
        items = data.get("response") or []
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
            inj_items = inj_data.get("response") or []
            injuries[str(fid)] = inj_items
            print(f"   伤停 {len(inj_items)} 条")
        except Exception as e:
            print(f"   伤停失败: {e}")
        time.sleep(1)
    with open("/tmp/inj_0827.json", "w", encoding="utf-8") as f:
        json.dump(injuries, f, ensure_ascii=False)
    print("SYNC_DONE")
finally:
    db.close()

# 预测
from prediction.predict import predict_fixture
db = SessionLocal()
try:
    for fid in FIXTURE_IDS:
        predict_fixture(fid, db=db)
finally:
    db.close()
print("PREDICT_DONE")