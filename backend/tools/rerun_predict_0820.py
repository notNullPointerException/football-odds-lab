"""重跑 2 场降级预测(网络超时): 波特诺 vs 帕梅拉斯, 凯尔特人 vs LASK"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.db.session import SessionLocal
from prediction.predict import predict_fixture
from app.models.fixture import Fixture

FIXTURE_IDS = [1547765, 1610923]

db = SessionLocal()
try:
    for i, fid in enumerate(FIXTURE_IDS):
        f = db.get(Fixture, fid)
        print(f"[{i+1}/{len(FIXTURE_IDS)}] 重跑: {f.home_name} vs {f.away_name} ({f.league_name})...", flush=True)
        try:
            predict_fixture(fid, db=db)
            print(f"  OK", flush=True)
        except Exception as e:
            print(f"  FAIL: {e}", flush=True)
finally:
    db.close()
