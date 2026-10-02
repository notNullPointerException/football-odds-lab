"""批量预测 8-20 竞彩比赛(已预测的跳过)"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy import text
from app.db.session import SessionLocal
from prediction.predict import predict_fixture
from app.models.fixture import Fixture

FIXTURE_IDS = [1570334, 1547765, 1610923, 1622624, 1622625]

db = SessionLocal()
try:
    for i, fid in enumerate(FIXTURE_IDS):
        f = db.get(Fixture, fid)
        if f is None:
            print(f"[{i+1}/{len(FIXTURE_IDS)}] SKIP {fid}: 库里不存在", flush=True)
            continue
        exists = db.execute(
            text("SELECT 1 FROM predictions WHERE fixture_id = :fid"),
            {"fid": fid},
        ).first()
        if exists:
            print(f"[{i+1}/{len(FIXTURE_IDS)}] SKIP {fid}: {f.home_name} vs {f.away_name} 已预测过", flush=True)
            continue
        print(f"[{i+1}/{len(FIXTURE_IDS)}] 预测中: {f.home_name} vs {f.away_name} ({f.league_name})...", flush=True)
        try:
            predict_fixture(fid, db=db)
            print(f"  OK", flush=True)
        except Exception as e:
            print(f"  FAIL: {e}", flush=True)
finally:
    db.close()
