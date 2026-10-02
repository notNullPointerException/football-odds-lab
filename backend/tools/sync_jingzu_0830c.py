# -*- coding: utf-8 -*-
"""周六晚场批次(8-29晚~8-30凌晨) 20场: 同步 + 伤停 + 预测"""
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

# 009埃沃斯堡vs药厂 010莱红牛vs门兴 011柏林联合vs法兰克福 012科隆vs霍村 013美因茨vs帕德博恩
# 014考文垂vs赫尔城 015伯恩茅斯vs埃弗顿 016莱万特vs贝蒂斯 017斯特拉斯vs朗斯 019热刺vs纽卡
# 020多特vs汉堡 021蒙扎vs乌迪内斯 022佛罗伦萨vs弗洛西诺 023萨索洛vs都灵 024阿尔克马vs前进之鹰
# 025皇家社会vs西班牙人 026维塞乌vs波尔图 027尤文vs帕尔马 028塞维利亚vs马竞 029华盛顿vs洛杉矶FC
FIXTURE_IDS = [1575143, 1575147, 1575148, 1575144, 1575146,
               1557380, 1557378, 1570357, 1552745, 1557386,
               1575142, 1550104, 1550100, 1550106, 1552146,
               1570361, 1575473, 1550101, 1570362, 1490423]

def api_get(path, params):
    last = None
    for attempt in range(10):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=15)
            return r.json()
        except Exception as e:
            last = e
            print(f"   retry {attempt+1}: {type(e).__name__}")
            time.sleep(2 * (attempt + 1))
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-m", "25", "-H",
                          f"x-apisports-key: {API_KEY}", url],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout)
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
        print(f"OK {fid} {home} vs {away}", flush=True)
        try:
            inj_data = api_get("/injuries", {"fixture": fid})
            injuries[str(fid)] = inj_data.get("response") or []
            print(f"   伤停 {len(injuries[str(fid)])} 条", flush=True)
        except Exception as e:
            print(f"   伤停失败: {e}", flush=True)
        time.sleep(1)
    with open("/tmp/inj_0830c.json", "w", encoding="utf-8") as f:
        json.dump(injuries, f, ensure_ascii=False)
    print("SYNC_DONE", flush=True)
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
print("PREDICT_DONE", flush=True)