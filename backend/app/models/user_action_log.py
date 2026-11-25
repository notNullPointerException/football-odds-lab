"""用户操作日志：谁在什么时候点了哪个菜单、调了哪个接口。

只记行为，不记请求体（密码/验证码绝不进来）。时间用服务器本地时间
（Asia/Shanghai，与 scheduler_logs 一致），方便按「今天/近 7 天」直接查。
"""
from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String

from app.db.base import Base


class UserActionLog(Base):
    __tablename__ = "user_action_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String(64), nullable=True)
    # api = 调接口 / menu = 点菜单 / auth = 登录注册退出
    action_type = Column(String(16), nullable=False, default="api")
    action = Column(String(128), nullable=True)      # 菜单名 / 操作名
    method = Column(String(8), nullable=True)
    path = Column(String(255), nullable=True)
    query = Column(String(1024), nullable=True)      # 查询参数（敏感键已打码）
    status_code = Column(Integer, nullable=True)
    ip = Column(String(64), nullable=True)
    ua = Column(String(512), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now, index=True)

    __table_args__ = (
        Index("idx_ual_user_time", "username", "created_at"),
        Index("idx_ual_path_time", "path", "created_at"),
    )
