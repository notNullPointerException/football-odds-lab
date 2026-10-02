# -*- coding: utf-8 -*-
"""周六竞彩 007-030 (9-12 晚~9-13 凌晨开球): 按已确认 fixture id 入库(category=jingzu)
007 Freiburg vs M'gladbach(德甲)   008 Mainz vs E.Frankfurt(德甲)
009 Dortmund vs Paderborn(德甲)    010 Augsburg vs Leverkusen(德甲)
011 Hoffenheim vs Stuttgart(德甲)  012 Chelsea vs Hull(英超)
013 Bournemouth vs Brentford(英超) 014 Aston Villa vs N.Forest(英超)
015 C.Palace vs Ipswich(英超)      016 Liverpool vs Fulham(英超)
017 Osasuna vs Espanyol(西甲)      018 Strasbourg vs Monaco(法甲)
019 Al Taawon vs Al-Hilal(沙特)    020 Lazio vs AC Milan(意甲)
021 Rosenborg vs Tromso(挪超)      022 Tottenham vs Everton(英超)
023 Koln vs Werder Bremen(德甲)    024 Ath.Bilbao vs Elche(西甲)
025 Casa Pia vs Porto(葡超)        026 Fortuna Sittard vs Ajax(荷甲)
027 Atalanta vs Cagliari(意甲)     028 Paris FC vs Lyon(法甲)
029 Sunderland vs Arsenal(英超)    030 Real Madrid vs Rayo(西甲)
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
    7: 1575161, 8: 1575163, 9: 1575158, 10: 1575162, 11: 1575160,
    12: 1557399, 13: 1557398, 14: 1557397, 15: 1557401, 16: 1557403,
    17: 1570377, 18: 1552764, 19: 1603033, 20: 1550121, 21: 1494761,
    22: 1557406, 23: 1575165, 24: 1570373, 25: 1575495, 26: 1552165,
    27: 1550117, 28: 1552762, 29: 1557405, 30: 1570379,
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
