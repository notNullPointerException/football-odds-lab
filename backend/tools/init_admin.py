"""初始化 admin 用户。

明文密码不写进代码：优先读环境变量 ADMIN_INIT_PASSWORD，
没有就交互输入（不回显），建好后也不打印。
"""
import os, sys, getpass
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime
from app.db.session import SessionLocal
from app.models.user import User
from app.api.auth import hash_password

def main():
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if admin:
            print("admin 已存在")
            return
        password = os.environ.get("ADMIN_INIT_PASSWORD") or getpass.getpass("新 admin 密码: ")
        if not password:
            print("密码为空，已取消")
            return
        admin = User(
            username="admin",
            password_hash=hash_password(password),
            role="admin",
            is_active=True,
            expires_at=None,  # admin 永不过期
        )
        db.add(admin)
        db.commit()
        print("admin 用户创建成功（密码不回显）")
    finally:
        db.close()

if __name__ == "__main__":
    main()
