# -*- coding: utf-8 -*-
"""周日批次 007-024 (9-6 晚): 按 league+date 拉 API 匹配队名 → 入库(category=jingzu)
007海伦芬vs阿尔克马尔(E) 008埃弗顿vs曼联(E) 009弗洛西诺内vs威尼斯(I) 010帕尔马vs蒙扎(I)
011特鲁瓦vs斯特拉斯堡(F) 012汉堡vs美因茨(D) 013巴伦西亚vs巴萨(S) 014圣克拉拉vs里奥阿维(P)
015莫尔德vs奥斯陆KFUM(N) 016阿森纳vs切尔西(E) 017法兰克福vs奥格斯堡(D) 018博洛尼亚vs萨索洛(I)
019阿拉维斯vs奥萨苏纳(S) 020马拉加vs莱万特(S) 021尤文vsAC米兰(I) 022马赛vs巴黎FC(F)
023西班牙人vs塞维利亚(S) 024博塔弗戈vs帕尔梅拉斯(BR)
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

# (编号-1, league_id, home kw, away kw)
TARGETS = [
    (0, 88,  ["heerenveen"],            ["az"]),
    (1, 39,  ["everton"],               ["manchester united", "man united"]),
    (2, 135, ["frosinone"],             ["venezia"]),
    (3, 135, ["parma"],                 ["monza"]),
    (4, 61,  ["troyes"],                ["strasbourg"]),
    (5, 78,  ["hamburg", "hamburger"],  ["mainz"]),
    (6, 140, ["valencia"],              ["barcelona"]),
    (7, 94,  ["santa clara"],           ["rio ave"]),
    (8, 103, ["molde"],                 ["kfum", "oslo"]),
    (9, 39,  ["arsenal"],               ["chelsea"]),
    (10, 78, ["frankfurt"],             ["augsburg"]),
    (11, 135,["bologna"],               ["sassuolo"]),
    (12, 140,["alaves", "alavés"],      ["osasuna"]),
    (13, 140,["malaga", "málaga"],      ["levante"]),
    (14, 135,["juventus"],              ["milan"]),
    (15, 61, ["marseille"],             ["paris fc"]),
    (16, 140,["espanyol"],              ["sevilla"]),
    (17, 71, ["botafogo"],              ["palmeiras"]),
]

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
found = {}
try:
    leagues = sorted({t[1] for t in TARGETS})
    for lid in leagues:
        try:
            data = api_get("/fixtures", {"league": lid, "season": 2026, "date": "2026-09-06"})
        except Exception as e:
            print(f"L{lid} API失败 {e}", flush=True); continue
        for it in data.get("response") or []:
            hn = (it["teams"]["home"]["name"] or "").lower()
            an = (it["teams"]["away"]["name"] or "").lower()
            for idx, lid2, hk, ak in TARGETS:
                if idx in found or lid2 != lid:
                    continue
                if any(k in hn for k in hk) and any(k in an for k in ak):
                    found[idx] = it
        time.sleep(0.25)
    for idx in sorted(found):
        it = found[idx]
        _upsert_fixture(db, it)
        db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"),
                   {"fid": it["fixture"]["id"]})
        db.commit()
        print(f"OK {idx+7:02d} [{it['fixture']['id']}] "
              f"{it['teams']['home']['name']} vs {it['teams']['away']['name']} "
              f"({it['league']['name']} {it['fixture']['date']})", flush=True)
    missing = [i + 7 for i in range(len(TARGETS)) if i not in found]
    if missing:
        print(f"MISSING: {missing}", flush=True)
    else:
        print("ALL_18_MATCHED", flush=True)
finally:
    db.close()
