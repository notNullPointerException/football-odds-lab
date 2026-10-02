# -*- coding: utf-8 -*-
"""周三竞彩 004-017 (businessDate 9-16, 开球 BJ 9-17 00:45 ~ 08:30): 按已确认 fixture id 入库(category=jingzu)
004 奥莫尼亚 vs 塞尔塔(欧罗巴)   005 拉科 vs 塞维利亚(西甲)     006 马竞 vs 奥萨苏纳(西甲)
007 AC米兰 vs 本菲卡(欧罗巴)     008 勒沃库森 vs 采列(欧罗巴)   009 桑德兰 vs 阿尔克马(欧罗巴)
010 格风暴 vs 雷恩(欧罗巴)       011 安德莱 vs 里昂(欧罗巴)     012 考文垂 vs 维拉(英联赛杯)
013 巴萨 vs 桑坦德(西甲)         014 莱万特 vs 毕尔巴鄂(西甲)   015 基多体大 vs 帕梅拉斯(解放者杯)
016 博塔弗戈 vs 格雷米奥(巴甲)   017 科林蒂安 vs 拉普大学(解放者杯)
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
    4: 1636312, 5: 1570387, 6: 1570384, 7: 1636207, 8: 1636225,
    9: 1636344, 10: 1636341, 11: 1636212, 12: 1635557, 13: 1570385,
    14: 1570389, 15: 1631509, 16: 1492311, 17: 1631508,
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
    # 核对本批 category 数量（memory: sync 后 category 曾丢掉）
    n = db.execute(text("SELECT COUNT(*) FROM fixtures WHERE id IN :ids").bindparams(
        ids=tuple(FIDS.values())))._fetchone() if False else None
    rows = db.execute(text("SELECT id, category FROM fixtures WHERE id IN (%s)" %
                           ",".join(str(v) for v in FIDS.values()))).fetchall()
    print("category 核对:", {r[0]: r[1] for r in rows}, flush=True)
finally:
    db.close()