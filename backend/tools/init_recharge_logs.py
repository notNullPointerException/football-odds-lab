"""初始化 1000 条充值滚动记录"""
import os, sys, random
from datetime import datetime, timedelta
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.session import SessionLocal
from app.models.recharge_log import RechargeLog

PLANS = [
    ("week", 188),
    ("month", 588),
    ("quarter", 1488),
    ("year", 4888),
]

def mask_name():
    prefix = random.choice(["用户", "彩民", "球迷", "玩家"])
    return f"{prefix}{random.randint(1000,9999)}"

def main():
    db = SessionLocal()
    try:
        if db.query(RechargeLog).count() > 0:
            print("已有数据，跳过")
            return
        now = datetime.utcnow()
        rows = []
        for _ in range(1000):
            plan, amount = random.choice(PLANS)
            rows.append(RechargeLog(
                username=mask_name(),
                plan=plan,
                amount=amount,
                created_at=now - timedelta(minutes=random.randint(1, 60*24*30)),
            ))
        db.bulk_save_objects(rows)
        db.commit()
        print("已插入 1000 条充值记录")
    finally:
        db.close()

if __name__ == "__main__":
    main()
