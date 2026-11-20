"""登录 / 注册 / 当前用户（Redis 存到期时间）"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from loguru import logger

from app.db.session import get_db
from app.models.user import User
from app.core.redis_client import get_redis

router = APIRouter()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain: str, hashed: str) -> bool:
    return hash_password(plain) == hashed


class RegisterIn(BaseModel):
    username: str
    password: str
    plan: str = "week"  # week / month / quarter / year

# 价格表（单位：元）
PLANS = {
    "week":    {"weeks": 1,  "price": 188,  "label": "周卡"},
    "month":   {"weeks": 4,  "price": 588,  "label": "月卡"},
    "quarter": {"weeks": 12, "price": 1488, "label": "季卡"},
    "year":    {"weeks": 52, "price": 4888, "label": "年卡"},
}


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


# Redis keys
def _user_expire_key(user_id: int) -> str:
    return f"user:expire:{user_id}"

def _token_key(token: str) -> str:
    return f"token:{token}"


def set_user_expiry(user_id: int, expires_at: datetime):
    """把用户到期时间写入 Redis（TTL 精确到秒）"""
    r = get_redis()
    if not r:
        return
    ttl = int((expires_at - datetime.utcnow()).total_seconds())
    if ttl > 0:
        r.setex(_user_expire_key(user_id), ttl, expires_at.isoformat())
        logger.info(f"用户 {user_id} 到期时间已写入 Redis，TTL={ttl}s")


def is_user_expired(user_id: int, role: str) -> bool:
    """检查用户是否已过期（admin 永不过期）"""
    if role == "admin":
        return False
    r = get_redis()
    if not r:
        return False  # Redis 不可用时不拦截
    return r.get(_user_expire_key(user_id)) is None


def create_token(user_id: int, hours: int = 24) -> str:
    token = secrets.token_urlsafe(32)
    r = get_redis()
    if r:
        r.setex(_token_key(token), hours * 3600, str(user_id))
    else:
        # fallback
        if not hasattr(create_token, "_mem"):
            create_token._mem = {}
        create_token._mem[token] = {
            "user_id": user_id,
            "expires": datetime.utcnow() + timedelta(hours=hours),
        }
    return token


def get_user_id_from_token(token: str) -> Optional[int]:
    r = get_redis()
    if r:
        uid = r.get(_token_key(token))
        return int(uid) if uid else None
    # fallback
    info = getattr(create_token, "_mem", {}).get(token)
    if info and info["expires"] > datetime.utcnow():
        return info["user_id"]
    return None


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    user_id = get_user_id_from_token(token)
    if not user_id:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        return None
    if is_user_expired(user.id, user.role):
        return None
    return user


@router.post("/register")
def register(body: RegisterIn, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(400, "用户名已存在")
    plan = PLANS.get(body.plan, PLANS["week"])
    expires = datetime.utcnow() + timedelta(weeks=plan["weeks"])
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role="user",
        is_active=False,  # 待激活
        expires_at=expires,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    # 写入 Redis 倒计时
    # 注册时不写 Redis，等管理员激活后再写
    token = create_token(user.id)
    return {"token": token, "user": UserOut.from_orm(user)}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已禁用")
    if is_user_expired(user.id, user.role):
        raise HTTPException(403, "订阅已到期，请续费")
    # 确保 Redis 里有到期记录（兼容旧数据）
    if user.expires_at and user.role != "admin":
        set_user_expiry(user.id, user.expires_at)
    token = create_token(user.id)
    return {"token": token, "user": UserOut.from_orm(user)}


@router.get("/me")
def me(user: Optional[User] = Depends(get_current_user)):
    if not user:
        raise HTTPException(401, "未登录或已过期")
    return UserOut.from_orm(user)


@router.post("/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        r = get_redis()
        if r:
            r.delete(_token_key(token))
    return {"ok": True}

@router.post("/activate")
def activate_user(username: str, db: Session = Depends(get_db)):
    """管理员激活用户（临时接口，后续可加权限）"""
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.is_active:
        return {"msg": "已激活"}
    user.is_active = True
    db.commit()
    if user.expires_at:
        set_user_expiry(user.id, user.expires_at)
    return {"msg": f"已激活 {username}，到期 {user.expires_at}"}
