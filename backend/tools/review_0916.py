# -*- coding: utf-8 -*-
"""复盘 9-16 周三批次 004-017 共 14 场: 同步赛果 + 写回 fixtures/predictions"""
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

NAMES = {
    1636312: "004 奥莫尼亚vs塞尔塔",   1570387: "005 拉科vs塞维利亚",
    1570384: "006 马竞vs奥萨苏纳",     1636207: "007 AC米兰vs本菲卡",
    1636225: "008 勒沃库森vs采列",     1636344: "009 桑德兰vs阿尔克马尔",
    1636341: "010 格拉茨风暴vs雷恩",   1636212: "011 安德莱赫特vs里昂",
    1635557: "012 考文垂vs维拉",       1570385: "013 巴萨vs桑坦德",
    1570389: "014 莱万特vs毕尔巴鄂",   1631509: "015 LDU vs 帕尔梅拉斯",
    1492311: "016 博塔弗戈vs格雷米奥", 1631508: "017 科林蒂安vs大学生",
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
    for fid, nm in NAMES.items():
        data = api_get("/fixtures", {"id": fid})
        if not data or not data.get("response"):
            print(f"[!] {fid} 拉取失败 {nm}", flush=True)
            fail += 1
            continue
        it = data["response"][0]
        goals = it["score"]["fulltime"]
        hg, ag = goals["home"], goals["away"]
        if hg is None:
            print(f"[!] {nm} 未结束或无比分 status={it['fixture']['status']['short']}", flush=True)
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
        print(f"OK {nm:<24} {hg}-{ag}  (半场 {ht.get('home')}-{ht.get('away')})", flush=True)
        time.sleep(0.8)
    print(f"SYNC_RESULTS_DONE ok={ok} fail={fail}", flush=True)
finally:
    db.close()
