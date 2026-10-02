"""周五批次 9 场: 同步进库 + 打 jingzu 标签 (带重试)"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from sqlalchemy import text
from app.db.session import SessionLocal
from app.services.fixture_service import _upsert_fixture
from app.core.config import settings

FIDS = [1602982, 1495763, 1494255, 1602985, 1552443, 1551757, 1552733, 1557367, 1570345]

def fetch(fid):
    for attempt in range(5):
        try:
            r = httpx.get(f"{settings.api_football_base_url}/fixtures",
                          headers={"x-apisports-key": settings.api_football_key},
                          params={"id": fid}, timeout=30)
            return r.json().get("response", [])
        except Exception as e:
            print(f"  retry {fid} #{attempt+1}: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
    return []

db = SessionLocal()
try:
    for fid in FIDS:
        items = fetch(fid)
        if not items:
            print(f"{fid}: NOT FOUND (跳过)")
            continue
        _upsert_fixture(db, items[0])
        db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:fid"), {"fid": fid})
        t = items[0]["teams"]
        print(f"{fid}: OK {t['home']['name']} vs {t['away']['name']}")
    db.commit()
finally:
    db.close()
