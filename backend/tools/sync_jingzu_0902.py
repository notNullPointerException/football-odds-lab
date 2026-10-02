# -*- coding: utf-8 -*-
"""周一批次 10场(9-2凌晨): 同步 + 伤停 + 预测
001利雅得新月vs吉达国民 002斯旺西vs沃特福德 003西汉姆vs狼队 004朴次茅斯vs德比郡 005林肯城vs布莱克本
006普雷斯顿vs布里斯托尔城 007谢菲联vs博尔顿 008伯明翰vs南安普敦 009斯托克城vs诺维奇 010都灵vs蒙扎
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

FIXTURE_IDS = [1603007, 1563125, 1563126, 1563121, 1563120,
               1563122, 1563123, 1563119, 1563124, 1628894]

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
    with open("/tmp/inj_0902.json", "w", encoding="utf-8") as f:
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