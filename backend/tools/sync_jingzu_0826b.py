# -*- coding: utf-8 -*-
"""周三剩余批次(8-26晚~8-27凌晨): 同步fixtures + 标记jingzu + 拉伤停"""
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

# 西甲:皇马vs皇家社会 欧冠:里昂vs费内巴切 AEKvs索菲亚列夫斯基 维京vs萨格勒布迪纳摩 采列vs布拉迪斯拉发
# 英联杯:纽卡vs西布朗 热刺vs查尔顿 布拉德福德vs伯恩利 普雷斯顿vs埃弗顿
# 沙职:费萨里vs法特赫 迪里耶vs胡鲁德 韩职:江原vs光州
FIXTURE_IDS = [1570340, 1622630, 1622629, 1622631, 1622628,
               1623089, 1623092, 1623084, 1623101,
               1602996, 1602997, 1507045]

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
    with open("/tmp/inj_0826b.json", "w", encoding="utf-8") as f:
        json.dump(injuries, f, ensure_ascii=False)
    print("DONE")
finally:
    db.close()