"""周日 001-027 二十七场: 同步进库 + 打标 + 伤停"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from sqlalchemy import text
from app.db.session import SessionLocal
from app.services.fixture_service import _upsert_fixture
from app.core.config import settings

FIDS = [1552139, 1554018, 1507038, 1563104, 1550682, 1494251, 1495768,
        1552140, 1557374, 1552729, 1550677, 1575472, 1494252, 1552141,
        1570344, 1552730, 1557375, 1550090, 1550096, 1575470, 1570348,
        1550094, 1550088, 1570346, 1575466, 1490419, 1492348]

def fetch(url, params, fid):
    for attempt in range(5):
        try:
            r = httpx.get(url, headers={"x-apisports-key": settings.api_football_key},
                          params=params, timeout=30)
            return r.json().get("response", [])
        except Exception as e:
            print(f"  retry {fid} #{attempt+1}: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
    return None

db = SessionLocal()
try:
    for fid in FIDS:
        items = fetch(f"{settings.api_football_base_url}/fixtures", {"id": fid}, fid)
        if items:
            _upsert_fixture(db, items[0])
            db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"), {"fid": fid})
            t = items[0]["teams"]
            print(f"sync {fid}: OK {t['home']['name']} vs {t['away']['name']}")
        else:
            print(f"sync {fid}: NOT FOUND")
    db.commit()
finally:
    db.close()

print("--- 伤停 ---")
inj_summary = {}
for fid in FIDS:
    items = fetch(f"{settings.api_football_base_url}/injuries", {"fixture": fid}, fid)
    if items is None:
        inj_summary[fid] = "网络失败"
        continue
    if not items:
        inj_summary[fid] = "无数据"
        continue
    lines = []
    seen = set()
    for it in items:
        p = it.get("player") or {}; t = it.get("team") or {}
        key = (t.get('name'), p.get('name'))
        if key in seen: continue
        seen.add(key)
        lines.append(f"[{t.get('name')}] {p.get('name')} - {p.get('type')} - {p.get('reason')}")
    inj_summary[fid] = lines
    print(f"{fid}:")
    for l in lines: print("  " + l)

import json
with open('/tmp/inj_0823.json','w') as f:
    json.dump({str(k): v for k, v in inj_summary.items()}, f, ensure_ascii=False, indent=1)
