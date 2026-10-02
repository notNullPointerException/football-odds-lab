# -*- coding: utf-8 -*-
"""复盘周六: 下午4场(001-004) + 晚场20场(009-029)"""
import sys, os, time, json, subprocess, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
from app.db.session import SessionLocal
from sqlalchemy import text

API_KEY = os.environ["API_FOOTBALL_KEY"]
BASE = os.environ["API_FOOTBALL_BASE_URL"]
HEADERS = {"x-apisports-key": API_KEY}

AFT = [1556034, 1556036, 1606634, 1507047]
EVE = [1575143, 1575147, 1575148, 1575144, 1575146,
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
            print(f"   retry {attempt+1}: {type(e).__name__}", flush=True)
            time.sleep(2 * (attempt + 1))
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-m", "25", "-H",
                          f"x-apisports-key: {API_KEY}", url],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout)
    raise last

db = SessionLocal()
try:
    for fid in AFT + EVE:
        data = api_get("/fixtures", {"id": fid})
        if not data or not data.get("response"):
            print(f"[!] {fid} 拉取失败", flush=True)
            continue
        it = data["response"][0]
        goals = it["score"]["fulltime"]
        hg, ag = goals["home"], goals["away"]
        if hg is None:
            print(f"[!] {fid} 未开赛 {it['teams']['home']['name']} vs {it['teams']['away']['name']}", flush=True)
            continue
        db.execute(text("""
            UPDATE predictions SET actual_home_goals=:hg, actual_away_goals=:ag WHERE fixture_id=:fid
        """), {"hg": hg, "ag": ag, "fid": fid})
        db.commit()
        print(f"OK {fid} {it['teams']['home']['name']} {hg}-{ag} {it['teams']['away']['name']}", flush=True)
        time.sleep(0.8)
finally:
    db.close()
print("SYNC_RESULTS_DONE", flush=True)