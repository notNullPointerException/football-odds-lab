# -*- coding: utf-8 -*-
"""周五批次 14 场(9-5 凌晨开球): 按日期拉 API 匹配队名 → 入库(category=jingzu) + 拉伤停
001汉诺威vs卡尔斯鲁厄 002里昂vs欧塞尔 003腓特烈斯塔vs博德闪耀(已在库) 004利雅得青年vs利雅得新月
005格勒诺布尔vs阿纳西 006鹿特丹斯巴达vs兹沃勒 007埃门vs福伦丹 008斯图加特vs科隆 009热那亚vs科莫
010伊普斯维奇vs利物浦 011贝蒂斯vs皇马 012巴黎vs摩纳哥 013波尔图vs摩雷伦斯 014纽约城vs纳什维尔(已在库)
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

# 期望场次: (home 关键字列表, away 关键字列表)
TARGETS = [
    (["hannover"], ["karlsruher"]),            # 001 德乙
    (["lyon"], ["auxerre"]),                   # 002 法甲
    (["fredrikstad"], ["bodo", "glimt"]),       # 003 挪超(已在库,幂等重放)
    (["shabab"], ["hilal"]),                    # 004 沙特
    (["grenoble"], ["annecy"]),                 # 005 法乙
    (["sparta"], ["zwolle"]),                   # 006 荷甲
    (["emmen"], ["volendam"]),                  # 007 荷乙
    (["stuttgart"], ["koln", "köln", "cologne"]),  # 008 德甲
    (["genoa", "genova"], ["como"]),            # 009 意甲
    (["ipswich"], ["liverpool"]),               # 010 英超
    (["betis"], ["madrid"]),                    # 011 西甲
    (["paris", "psg"], ["monaco"]),             # 012 法甲
    (["porto"], ["moreirense"]),                # 013 葡超
    (["new york"], ["nashville"]),              # 014 MLS(已在库,幂等)
]

def api_get(path, params):
    last = None
    for attempt in range(10):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=15)
            return r.json()
        except Exception as e:
            last = e
            print(f"   retry {attempt+1}: {type(e).__name__}")
            time.sleep(2 * (attempt + 1))
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-m", "25", "-H",
                          f"x-apisports-key: {API_KEY}", url],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout)
    raise last

def match_item(it):
    hn = (it["teams"]["home"]["name"] or "").lower()
    an = (it["teams"]["away"]["name"] or "").lower()
    for i, (hk, ak) in enumerate(TARGETS):
        if any(k in hn for k in hk) and any(k in an for k in ak):
            return i
    return None

db = SessionLocal()
found = {}
try:
    for day in ["2026-09-04", "2026-09-05"]:
        for page in range(1, 5):
            data = api_get("/fixtures", {"date": day, "page": page})
            items = data.get("response") or []
            if not items:
                break
            for it in items:
                idx = match_item(it)
                if idx is None:
                    continue
                if idx in found:
                    continue
                found[idx] = it
                _upsert_fixture(db, it)
                db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"),
                           {"fid": it["fixture"]["id"]})
                db.commit()
                print(f"OK {idx+1:02d} [{it['fixture']['id']}] "
                      f"{it['teams']['home']['name']} vs {it['teams']['away']['name']} "
                      f"({it['league']['name']} {it['fixture']['date']})", flush=True)
            if page >= (data.get("paging") or {}).get("total", 1):
                break
            time.sleep(0.5)
    missing = [i + 1 for i in range(len(TARGETS)) if i not in found]
    if missing:
        print(f"MISSING: {missing}", flush=True)
    else:
        print("ALL_14_MATCHED", flush=True)
finally:
    db.close()