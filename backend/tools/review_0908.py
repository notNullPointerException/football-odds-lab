# -*- coding: utf-8 -*-
"""复盘 9-7 周一批次(9-8 凌晨开球) 9 场: 同步结果 + 对错对比 + 写回 fixtures"""
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

FIXTURE_IDS = [1550109, 1570368, 1494269, 1494268, 1603018,
               1550116, 1575483, 1570366, 1492369]
NAMES = {1550109: "001 卡利亚里vs莱切", 1570368: "002 赫塔费vs塞尔塔", 1494269: "003 马尔默vsAIK",
         1494268: "004 卡尔马vs佐加顿斯", 1603018: "005 新月vs新未来", 1550116: "006 乌迪内斯vs拉齐奥",
         1575483: "007 埃斯托里尔vs阿罗卡", 1570366: "008 埃尔切vs皇社", 1492369: "009 维多利亚vs格雷米奥"}

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
    for fid in FIXTURE_IDS:
        data = api_get("/fixtures", {"id": fid})
        if not data or not data.get("response"):
            print(f"[!] {fid} 拉取失败", flush=True)
            continue
        it = data["response"][0]
        goals = it["score"]["fulltime"]
        hg, ag = goals["home"], goals["away"]
        if hg is None:
            print(f"[!] {fid} 未开赛 {NAMES[fid]}", flush=True)
            continue
        st = it["fixture"]["status"]["short"]
        db.execute(text("UPDATE predictions SET actual_home_goals=:hg, actual_away_goals=:ag WHERE fixture_id=:fid"),
                   {"hg": hg, "ag": ag, "fid": fid})
        db.execute(text("""UPDATE fixtures SET status_short=:st, status_long='Match Finished',
            goals_home=:hg, goals_away=:ag, home_winner=:hw, fulltime_home=:hg, fulltime_away=:ag
            WHERE id=:fid"""),
                   {"st": st, "hg": hg, "ag": ag, "hw": hg > ag, "fid": fid})
        db.commit()
        print(f"OK {NAMES[fid]} {hg}-{ag}", flush=True)
        time.sleep(0.8)
    print("SYNC_RESULTS_DONE", flush=True)
finally:
    db.close()
