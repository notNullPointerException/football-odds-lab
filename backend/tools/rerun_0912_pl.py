# -*- coding: utf-8 -*-
"""重跑周六 012-016 英超 5 场: 官方首发已出(21:28)，_fetch_lineups_text 这次能拿到确认名单。
predict.py 用 INSERT..ON DUPLICATE KEY UPDATE, 覆盖写不产生重复行。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db.session import SessionLocal
from prediction.predict import predict_fixture

FIDS = [
    (1557399, "012 切尔西vs赫尔城"),
    (1557398, "013 伯恩茅斯vs布伦特福德"),
    (1557397, "014 维拉vs诺丁汉"),
    (1557401, "015 水晶宫vs伊普斯"),
    (1557403, "016 利物浦vs富勒姆"),
]

db = SessionLocal()
try:
    for fid, nm in FIDS:
        print(f"[重跑] {nm} [{fid}] ...", flush=True)
        try:
            predict_fixture(fid, db=db)
            print(f"  OK", flush=True)
        except Exception as e:
            print(f"  FAIL: {e}", flush=True)
finally:
    db.close()
