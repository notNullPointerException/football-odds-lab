"""充值记录（用于滚动条展示）"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Numeric
from app.db.base import Base


class RechargeLog(Base):
    __tablename__ = "recharge_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, index=True)  # 脱敏用户名
    plan = Column(String(20), nullable=False)              # week/month/quarter/year
    amount = Column(Numeric(10, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
