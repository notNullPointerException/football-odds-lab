# -*- coding: utf-8 -*-
"""复盘 9-12 周六批次 005-030 (9-12 晚 ~ 9-13 凌晨) 26 场: 同步结果 + 写回 fixtures/predictions"""
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
    1570378: "005 桑坦德vs阿拉维斯",      1550119: "006 热那亚vs弗洛西诺内",
    1575161: "007 弗赖堡vs门兴",          1575163: "008 美因茨vs法兰克福",
    1575158: "009 多特vs帕德博恩",        1575162: "010 奥格斯堡vs勒沃库森",
    1575160: "011 霍芬海姆vs斯图加特",    1557399: "012 切尔西vs赫尔城",
    1557398: "013 伯恩茅斯vs布伦特福德",  1557397: "014 维拉vs诺丁汉",
    1557401: "015 水晶宫vs伊普斯",        1557403: "016 利物浦vs富勒姆",
    1570377: "017 奥萨苏纳vs西班牙人",    1552764: "018 斯特拉斯vs摩纳哥",
    1603033: "019 布赖代合作vs利雅得新月", 1550121: "020 拉齐奥vsAC米兰",
    1494761: "021 罗森博格vs特罗姆瑟",    1557406: "022 热刺vs埃弗顿",
    1575165: "023 科隆vs不来梅",          1570373: "024 毕尔巴鄂vs埃尔切",
    1575495: "025 卡萨皮亚vs波尔图",      1552165: "026 福图纳vs阿贾克斯",
    1550117: "027 亚特兰大vs卡利亚里",    1552762: "028 巴黎FCvs里昂",
    1557405: "029 桑德兰vs阿森纳",        1570379: "030 皇马vs巴列卡诺",
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
    # 顺带修复被刷掉的 category
    db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id IN (%s)"
                    % ",".join(map(str, NAMES))))
    db.commit()
    for fid in sorted(NAMES, key=lambda x: x):
        data = api_get("/fixtures", {"id": fid})
        if not data or not data.get("response"):
            print(f"[!] {fid} {NAMES[fid]} 拉取失败", flush=True)
            fail += 1
            continue
        it = data["response"][0]
        ft = it["score"]["fulltime"]
        hg, ag = ft["home"], ft["away"]
        st = it["fixture"]["status"]["short"]
        if hg is None:
            print(f"[!] {fid} {NAMES[fid]} 未结束 status={st}", flush=True)
            fail += 1
            continue
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
        print(f"OK {NAMES[fid]} {hg}-{ag} (半场 {ht.get('home')}-{ht.get('away')}) {st}", flush=True)
        ok += 1
        time.sleep(0.8)
    print(f"SYNC_RESULTS_DONE ok={ok} fail={fail}", flush=True)
finally:
    db.close()
