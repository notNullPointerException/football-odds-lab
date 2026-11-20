"""登录 / 注册 / 当前用户"""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from loguru import logger

from app.db.session import get_db
from app.models.user import User

router = APIRouter()

# 简单密码哈希（生产建议换 bcrypt）
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain: str, hashed: str) -> bool:
    return hash_password(plain) == hashed


class RegisterIn(BaseModel):
    username: str
    password: str
    months: int = 1  # 默认 1 个月


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    expires_at: Optional[datetime]
    is_active: bool

    class Config:
        from_attributes = True


# 内存 token 存储（简单版；生产可换 Redis）
# token -> {"user_id": int, "expires": datetime}
_TOKENS: dict[str, dict] = {}


def create_token(user_id: int, hours: int = 24) -> str:
    token = secrets.token_urlsafe(32)
    _TOKENS[token] = {
        "user_id": user_id,
        "expires": datetime.utcnow() + timedelta(hours=hours),
    }
    return token


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    info = _TOKENS.get(token)
    if not info or info["expires"] < datetime.utcnow():
        _TOKENS.pop(token, None)
        return None
    user = db.query(User).filter(User.id == info["user_id"]).first()
    if not user or not user.is_active:
        return None
    # 检查订阅是否过期（admin 除外）
    if user.role != "admin" and user.expires_at and user.expires_at < datetime.utcnow():
        return None
    return user


@router.post("/register")
def register(body: RegisterIn, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(400, "用户名已存在")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role="user",
        expires_at=datetime.utcnow() + timedelta(days=body.months * 30),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_token(user.id)
    return {"token": token, "user": UserOut.from_orm(user)}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已禁用")
    if user.role != "admin" and user.expires_at and user.expires_at < datetime.utcnow():
        raise HTTPException(403, "订阅已到期，请续费")
    token = create_token(user.id)
    return {"token": token, "user": UserOut.from_orm(user)}


@router.get("/me")
def me(user: Optional[User] = Depends(get_current_user)):
    if not user:
        raise HTTPException(401, "未登录")
    return UserOut.from_orm(user)


@router.post("/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        _TOKENS.pop(token, None)
    return {"ok": True}
