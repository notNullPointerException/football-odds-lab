# -*- coding: utf-8 -*-
"""周五竞彩 002-012 (9-11 晚~9-12 凌晨开球): 按已确认 fixture id 入库(category=jingzu)
002 Al-Faisaly vs Al-Ittihad(沙特)      003 Nurnberg vs Hannover 96(德乙)
004 Hacken vs Mjallby(瑞超)             005 AZ Alkmaar vs Willem II(荷甲)
006 MVV vs Almere City(荷乙)            007 Union Berlin vs Schalke 04(德甲)
008 Venezia vs Fiorentina(意甲)         009 Rennes vs Marseille(法甲)
010 West Ham vs Wrexham(英冠)           011 Sevilla vs Valencia(西甲)
012 Coritiba vs Atletico Paranaense(巴甲)
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

FIDS = {
    2: 1603028, 3: 1576176, 4: 1494277, 5: 1552162, 6: 1551792,
    7: 1575164, 8: 1550126, 9: 1552763, 10: 1563166, 11: 1570381, 12: 1492374,
}


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
ok, missing = [], []
try:
    for idx, fid in sorted(FIDS.items()):
        data = api_get("/fixtures", {"id": fid})
        if not data or not data.get("response"):
            print(f"MISSING {idx:03d} {fid}", flush=True)
            missing.append(idx)
            continue
        it = data["response"][0]
        _upsert_fixture(db, it)
        db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"), {"fid": fid})
        db.commit()
        print(f"OK {idx:03d} [{fid}] {it['teams']['home']['name']} vs {it['teams']['away']['name']} ({it['league']['name']})", flush=True)
        ok.append(idx)
        time.sleep(0.4)
    print(f"SYNCED {len(ok)} MISSING {missing}", flush=True)
finally:
    db.close()