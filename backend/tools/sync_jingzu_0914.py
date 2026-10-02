# -*- coding: utf-8 -*-
"""周一竞彩 002-012 (BJ 9-14 23:00 ~ 9-15 03:45 开球): 按已确认 fixture id 入库(category=jingzu)
002 国际图尔 vs 瓦萨(芬超)   003 科莫 vs 帕尔马(意甲)   004 都灵 vs 罗马(意甲)
005 佐加顿斯 vs 盖斯(瑞超)   006 博德闪耀 vs 桑纳菲(挪超) 007 吉达国民 vs 棉农(亚冠精英)
008 国米 vs 乌迪内斯(意甲)   009 圣旺红星 vs 梅斯(法乙)  010 利兹联 vs 纽卡斯尔(英超)
011 比利亚雷 vs 贝蒂斯(西甲) 012 布拉加 vs 埃斯托里(葡超)
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
    2: 1638097, 3: 1550118, 4: 1550125, 5: 1494274, 6: 1494756,
    7: 1629915, 8: 1550120, 9: 1552474, 10: 1557402, 11: 1570382, 12: 1575494,
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
