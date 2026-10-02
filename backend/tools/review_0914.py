# -*- coding: utf-8 -*-
"""复盘 9-13 周日批次 009-024 共 16 场: 同步赛果 + 写回 fixtures/predictions"""
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

FIXTURE_IDS = [1575159, 1570376, 1494758, 1552759, 1557404, 1575166, 1550123, 1570375,
               1575493, 1552169, 1550124, 1552757, 1570380, 1575496, 1492375, 1490479]
NAMES = {
    1575159: "009 莱红牛vs汉堡",      1570376: "010 莱万特vs巴萨",
    1494758: "011 汉坎vs莫尔德",      1552759: "012 勒芒vs朗斯",
    1557404: "013 曼联vs曼城",        1575166: "014 埃沃斯堡vs拜仁",
    1550123: "015 那不勒斯vs博洛尼亚", 1570375: "016 赫塔费vs拉科",
    1575493: "017 本菲卡vs吉维森特",   1552169: "018 埃因霍温vs鹿斯巴达",
    1550124: "019 萨索洛vs尤文",      1552757: "020 布雷斯特vs巴黎",
    1570380: "021 皇家社会vs马竞",    1575496: "022 法马利康vs里斯本",
    1492375: "023 弗拉门戈vs科林蒂安", 1490479: "024 芝加哥vs新英格兰",
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
ok = fail = 0
try:
    for fid in FIXTURE_IDS:
        data = api_get("/fixtures", {"id": fid})
        if not data or not data.get("response"):
            print(f"[!] {fid} 拉取失败", flush=True)
            fail += 1
            continue
        it = data["response"][0]
        goals = it["score"]["fulltime"]
        hg, ag = goals["home"], goals["away"]
        if hg is None:
            print(f"[!] {fid} 未结束或无比分 {NAMES[fid]}", flush=True)
            fail += 1
            continue
        st = it["fixture"]["status"]["short"]
        ht = it["score"].get("halftime") or {}
        db.execute(text("UPDATE predictions SET actual_home_goals=:hg, actual_away_goals=:ag WHERE fixture_id=:fid"),
                   {"hg": hg, "ag": ag, "fid": fid})
        db.execute(text("""UPDATE fixtures SET status_short=:st, status_long='Match Finished',
            goals_home=:hg, goals_away=:ag, home_winner=:hw, category='jingzu',
            halftime_home=:hth, halftime_away=:hta,
            fulltime_home=:hg, fulltime_away=:ag
            WHERE id=:fid"""),
                   {"st": st, "hg": hg, "ag": ag, "hw": hg > ag, "fid": fid,
                    "hth": ht.get("home"), "hta": ht.get("away")})
        db.commit()
        ok += 1
        print(f"OK {NAMES[fid]} {hg}-{ag} (半场 {ht.get('home')}-{ht.get('away')})", flush=True)
        time.sleep(0.8)
    print(f"SYNC_RESULTS_DONE ok={ok} fail={fail}", flush=True)
finally:
    db.close()
