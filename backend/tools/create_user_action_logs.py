"""创建用户操作日志表（幂等，重复执行安全）。

用法：
    cd backend && .venv/bin/python tools/create_user_action_logs.py
"""
from app.db.base import Base
from app.db.session import engine
from app.models.user_action_log import UserActionLog


def main():
    Base.metadata.create_all(engine, tables=[UserActionLog.__table__], checkfirst=True)
    print(f"table ready: {UserActionLog.__tablename__}")


if __name__ == "__main__":
    main()
