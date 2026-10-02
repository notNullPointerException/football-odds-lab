# -*- coding: utf-8 -*-
"""复盘 9-9 周三批次(002 今晚23:55 + 9-10 凌晨/早) 14 场: 同步结果 + 写回 fixtures/predictions"""
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

FIXTURE_IDS = [1603023, 1635628, 1635741, 1552142, 1635736, 1635698, 1635686,
               1635705, 1563151, 1636205, 1575469, 1631507, 1631506, 1490457]
NAMES = {1603023: "002 拉斯永恒vs利雅得青年", 1635628: "003 巴萨vs费耶诺德",
         1635741: "004 斯图加特vs维京", 1552142: "005 特温特vs特尔斯达",
         1635736: "006 里斯本竞技vs加拉塔萨雷", 1635698: "007 那不勒斯vs阿森纳",
         1635686: "008 利物浦vs马竞", 1635705: "009 巴黎vs布拉迪斯拉发",
         1563151: "010 查尔顿vsQPR", 1636205: "011 切尔西vs利兹联",
         1575469: "012 摩雷伦斯vs本菲卡", 1631507: "013 帕尔梅拉斯vs基多",
         1631506: "014 大学生vs科林蒂安", 1490457: "015 芝加哥vs迈阿密"}

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
            print(f"[!] {fid} 未结束或无比分 {NAMES[fid]}", flush=True)
            continue
        st = it["fixture"]["status"]["short"]
        ht = it["score"].get("halftime") or {}
        db.execute(text("UPDATE predictions SET actual_home_goals=:hg, actual_away_goals=:ag WHERE fixture_id=:fid"),
                   {"hg": hg, "ag": ag, "fid": fid})
        db.execute(text("""UPDATE fixtures SET status_short=:st, status_long='Match Finished',
            goals_home=:hg, goals_away=:ag, home_winner=:hw,
            halftime_home=:hth, halftime_away=:hta,
            fulltime_home=:hg, fulltime_away=:ag
            WHERE id=:fid"""),
                   {"st": st, "hg": hg, "ag": ag, "hw": hg > ag, "fid": fid,
                    "hth": ht.get("home"), "hta": ht.get("away")})
        db.commit()
        print(f"OK {NAMES[fid]} {hg}-{ag} (半场 {ht.get('home')}-{ht.get('away')})", flush=True)
        time.sleep(0.8)
    print("SYNC_RESULTS_DONE", flush=True)
finally:
    db.close()
