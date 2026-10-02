# -*- coding: utf-8 -*-
"""周五批次(8-29凌晨) 14场: 同步fixtures + 标记jingzu + 拉伤停 + 预测"""
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

# 001不伦瑞克vs柏林赫塔 002波鸿vs奥斯纳 003桑坦德vs埃尔切 004赛哈海湾vs利雅新月 005蒙彼利埃vs布洛涅
# 006格罗宁根vs福图纳 007罗达JCvs布雷达 008拜仁vs斯图加特 009AC米兰vs威尼斯 010里尔vs巴黎圣曼
# 011水晶宫vs曼城 012雷克斯vs伯明翰 013里奥阿维vs里斯本 014阿拉维斯vs比利亚雷
FIXTURE_IDS = [1576153, 1576152, 1570359, 1603000, 1552455, 1552144, 1551772,
               1575140, 1550097, 1552746, 1557381, 1563118, 1575481, 1570353]

import subprocess, urllib.parse

def api_get(path, params):
    last = None
    for attempt in range(10):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=15)
            return r.json()
        except Exception as e:
            last = e
            print(f"   retry {attempt+1}: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
    # python SSL 抽风时用 curl 兜底
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
        print(f"OK {fid} {home} vs {away}")
        try:
            inj_data = api_get("/injuries", {"fixture": fid})
            inj_items = inj_data.get("response") or []
            injuries[str(fid)] = inj_items
            print(f"   伤停 {len(inj_items)} 条")
        except Exception as e:
            print(f"   伤停失败: {e}")
        time.sleep(1)
    with open("/tmp/inj_0829.json", "w", encoding="utf-8") as f:
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