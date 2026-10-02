# -*- coding: utf-8 -*-
"""周三竞彩 002-015 (9-9/9-10 凌晨开球): 按已确认 fixture id 入库(category=jingzu)
002 Al Kholood vs Al Shabab(沙特) 003 Barcelona vs Feyenoord(UCL) 004 Stuttgart vs Viking(UCL)
005 Twente vs Telstar(荷甲) 006 Sporting CP vs Galatasaray(UCL) 007 Napoli vs Arsenal(UCL)
008 Liverpool vs Atletico(UCL) 009 PSG vs Slovan(UCL) 010 Charlton vs QPR(英冠)
011 Chelsea vs Leeds(英联杯) 012 Moreirense vs Benfica(葡超) 013 Palmeiras vs LDU(解放者)
014 Estudiantes LP vs Corinthians(解放者) 015 Chicago Fire vs Inter Miami(MLS)
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
    2: 1603023, 3: 1635628, 4: 1635741, 5: 1552142, 6: 1635736, 7: 1635698,
    8: 1635686, 9: 1635705, 10: 1563151, 11: 1636205, 12: 1575469, 13: 1631507,
    14: 1631506, 15: 1490457,
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
