# -*- coding: utf-8 -*-
"""周日批次 15场(8-30晚~8-31凌晨): 同步 + 伤停 + 预测"""
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

# 012特尔斯达vs阿贾克斯 018维京vs奥勒松 013皇马vs马拉加 014曼联vs伊普斯维奇 015奥格斯堡vs沙尔克
# 016那不勒斯vs科莫 017拉科vs巴伦西亚 018坎布尔vs特温特 019拉齐奥vs热那亚 020卡利亚里vs国米
# 021摩纳哥vs马赛 022弗拉门戈vs博塔弗戈 023塞尔塔vs毕尔巴鄂 024法马利康vs吉维森特 025圣路易斯城vs达拉斯
FIXTURE_IDS = [1552151, 1494747, 1570360, 1557384, 1575141,
               1550105, 1570356, 1552152, 1550102, 1550099,
               1552742, 1492354, 1570355, 1575479, 1490435]

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
    with open("/tmp/inj_0831.json", "w", encoding="utf-8") as f:
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