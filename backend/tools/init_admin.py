"""初始化 admin 用户（密码 B8[i{Rf4）"""
import os, sys
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
        admin = User(
            username="admin",
            password_hash=hash_password("B8[i{Rf4"),
            role="admin",
            is_active=True,
            expires_at=None,  # admin 永不过期
        )
        db.add(admin)
        db.commit()
        print("admin 用户创建成功，密码: B8[i{Rf4")
    finally:
        db.close()

if __name__ == "__main__":
    main()
