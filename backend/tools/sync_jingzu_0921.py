# -*- coding: utf-8 -*-
"""周二竞彩 002-004 (businessDate 9-22, 开球 BJ 9-23 02:00): 按已确认 fixture id 入库(category=jingzu)

体彩 2026-09-21 21:00 在售就这 3 场，全 EFL Trophy(英锦标赛, league 46)：
002 米尔顿 vs 克劳利   (Milton Keynes Dons vs Crawley Town)
003 诺茨郡 vs 格里姆   (Notts County vs Grimsby)
004 维冈   vs 布莱克浦 (Wigan vs Blackpool)

三场体彩玩法都开(had/hhad 都非空)，让球线均为 hhad.line=-1、side=主队让。

⚠️ 定时任务 fixture_daily / fixture_live 的同步范围是【截至今天】(fixture_service.py:109
   `while d <= today`)，永远拉不到 9-22/9-23 —— 这批必须靠本工具手动入库。
⚠️ `_upsert_fixture()` 不写 category，set category 必须在 upsert 之后（memory 记过这个坑）。
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

# 体彩编号 -> fixture_id
FIDS = {
    2: 1588908,   # 米尔顿 vs 克劳利
    3: 1588849,   # 诺茨郡 vs 格里姆
    4: 1588823,   # 维冈 vs 布莱克浦
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
        db.commit()
        # 🔴 upsert 之后再标 category（_upsert_fixture 不写该字段）
        db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"), {"fid": fid})
        db.commit()
        print(f"OK {idx:03d} [{fid}] {it['teams']['home']['name']} vs "
              f"{it['teams']['away']['name']} ({it['league']['name']}) "
              f"{it['fixture']['date']}", flush=True)
        ok.append(idx)
        time.sleep(0.4)
    print(f"SYNCED {len(ok)} {ok} MISSING {missing}", flush=True)

    # 核对本批 category —— sync 后 category 曾被重置为 NULL，必须回查
    rows = db.execute(text(
        "SELECT id, category, league_name, home_name, away_name, date, status_short "
        "FROM fixtures WHERE id IN (%s)" % ",".join(str(v) for v in FIDS.values())
    )).mappings().all()
    print("核对:", flush=True)
    for r in rows:
        print("  ", dict(r), flush=True)
finally:
    db.close()
