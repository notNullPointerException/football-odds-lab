# -*- coding: utf-8 -*-
"""复盘 9-11 周五批次 002-012 (9-11 晚 ~ 9-12 早) 11 场: 同步结果 + 写回 fixtures/predictions"""
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

FIXTURE_IDS = [1603028, 1576176, 1494277, 1552162, 1551792,
               1575164, 1550126, 1552763, 1563166, 1570381, 1492374]
NAMES = {
    1603028: "002 哈马费萨vs吉达联合", 1576176: "003 纽伦堡vs汉诺威96",
    1494277: "004 赫根vs米亚尔比",     1552162: "005 阿尔克马尔vs威廉二世",
    1551792: "006 马斯特里赫特vs阿尔梅勒城", 1575164: "007 柏林联合vs沙尔克04",
    1550126: "008 威尼斯vs佛罗伦萨",   1552763: "009 雷恩vs马赛",
    1563166: "010 西汉姆联vs雷克斯汉姆", 1570381: "011 塞维利亚vs巴伦西亚",
    1492374: "012 科里蒂巴vs巴拉纳竞技",
}


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
