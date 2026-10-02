"""周六 016-028 十三场: 同步进库 + 打标 + 伤停"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from sqlalchemy import text
from app.db.session import SessionLocal
from app.services.fixture_service import _upsert_fixture
from app.core.config import settings

FIDS = [1570343, 1552732, 1557372, 1550095, 1550092, 1570352, 1582362,
        1550091, 1550093, 1552734, 1552736, 1570347, 1490415]

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
for fid in FIDS:
    items = fetch(f"{settings.api_football_base_url}/injuries", {"fixture": fid}, fid)
    if items is None:
        print(f"{fid}: 网络失败")
        continue
    if not items:
        print(f"{fid}: 无伤停数据")
        continue
    print(f"{fid}:")
    seen = set()
    for it in items:
        p = it.get("player") or {}; t = it.get("team") or {}
        key = (t.get('name'), p.get('name'))
        if key in seen: continue
        seen.add(key)
        print(f"  [{t.get('name')}] {p.get('name')} - {p.get('type')} - {p.get('reason')}")
