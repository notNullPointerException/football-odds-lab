"""登录 / 注册 / 当前用户（Redis 存到期时间）"""
import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from loguru import logger

from app.db.session import get_db
from app.models.user import User
from app.core.redis_client import get_redis
from app.core.rate_limit import hit, client_ip
from app.services.audit_service import log_action

router = APIRouter()


WEAK_PASSWORDS = {
    "123456", "12345678", "password", "admin", "qwerty", "abc123",
    "111111", "000000", "888888", "666666", "123123", "admin123",
}

def is_weak_password(pwd: str) -> bool:
    if len(pwd) < 6:
        return True
    if pwd.lower() in WEAK_PASSWORDS:
        return True
    if pwd.isdigit() or pwd.isalpha():
        return True
    return False

import bcrypt

PBKDF2_ROUNDS = 120_000
BCRYPT_ROUNDS = 12   # Spring Security BCryptPasswordEncoder 默认 10，这里更慢更抗爆破


def hash_password(password: str) -> str:
    """bcrypt（与 Spring Security BCryptPasswordEncoder 同一算法）。

    每个密码独立随机盐，慢哈希 + 加盐 → 拿到数据库也几乎不可能反推出密码。
    库里同时兼容三种历史格式，登录成功时自动升级成 bcrypt：
      $2b$...              bcrypt（当前格式）
      pbkdf2_sha256$...    PBKDF2（过渡格式）
      64 位十六进制        无盐 sha256（最老格式）
    """
    pw = password.encode()[:72]   # bcrypt 只取前 72 字节，显式截断避免版本差异
    return bcrypt.hashpw(pw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(plain: str, stored: str) -> bool:
    try:
        if stored.startswith("$2"):        # bcrypt: $2a$ / $2b$ / $2y$
            return bcrypt.checkpw(plain.encode()[:72], stored.encode())
        if stored.startswith("pbkdf2_sha256$"):
            _, rounds, b64salt, b64hash = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                "sha256", plain.encode(), base64.b64decode(b64salt), int(rounds)
            )
            return hmac.compare_digest(dk, base64.b64decode(b64hash))
    except Exception:
        return False
    # 最老格式：无盐 sha256（常量时间对比）
    return hmac.compare_digest(hashlib.sha256(plain.encode()).hexdigest(), stored)


def needs_password_upgrade(stored: str) -> bool:
    return not stored.startswith("$2")


class RegisterIn(BaseModel):
    # 长度硬上限：防大体积请求（业务规则仍由下面的 8/12 位检查兜住）
    username: str = Field(max_length=64)
    password: str = Field(max_length=64)
    plan: str = Field(default="week", max_length=20)
    captcha_id: str = Field(default="", max_length=64)
    captcha_code: str = Field(default="", max_length=16)

# 价格表（单位：元）
PLANS = {
    "week":    {"weeks": 1,  "price": 188,  "label": "周卡"},
    "month":   {"weeks": 4,  "price": 588,  "label": "月卡"},
    "quarter": {"weeks": 12, "price": 1488, "label": "季卡"},
    "year":    {"weeks": 52, "price": 4888, "label": "年卡"},
}


def _apply_plan(user: User, plan: str) -> datetime:
    """把套餐时长加到到期时间上：未过期从现有到期日顺延，过期/新用户从现在起算。"""
    p = PLANS.get(plan)
    if not p:
        raise HTTPException(400, "套餐不存在（week/month/quarter/year）")
    now = datetime.utcnow()
    base = user.expires_at if (user.expires_at and user.expires_at > now) else now
    user.expires_at = base + timedelta(weeks=p["weeks"])
    return user.expires_at


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=64)
    captcha_id: str = Field(default="", max_length=64)
    captcha_code: str = Field(default="", max_length=16)
    remember: bool = False   # 勾「保持登录」→ 30 天 token；否则仍是 30 分钟空闲超时


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    expires_at: Optional[datetime]
    is_active: bool
    is_member: bool = False   # 由 _user_out() 填，不是数据库列

    class Config:
        from_attributes = True


# 数据库里的 expires_at 是 UTC（datetime.utcnow 写入）；对外一律转成北京时间输出。
# 只改展示，不改存储：is_member/续费计算仍用 UTC 比较，存量数据不用迁移。
BJ_TZ = timezone(timedelta(hours=8))


def _to_bj(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJ_TZ)


def _user_out(user: User) -> UserOut:
    out = UserOut.model_validate(user)
    # 带 +08:00 的 ISO，前端 new Date()/切片显示都是北京时间
    out.expires_at = _to_bj(user.expires_at)
    out.is_member = is_member(user)
    return out


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


def is_member(user: User) -> bool:
    """会员判定：admin 永久有效；普通用户看到期时间。

    以【数据库 expires_at】为准 —— Redis 键只做倒计时展示，
    Redis 重启丢了键不能把付费用户变成非会员。
    """
    if user.role == "admin":
        return True
    return bool(user.expires_at and user.expires_at > datetime.utcnow())


# 会话空闲超时：30 分钟没有请求才失效；有请求就滑动续期。
# 与 Spring Security / Spring Session 的 server.servlet.session.timeout 语义一致：
# 计的是【距最后一次活动的空闲时长】，不是从登录起算的绝对时长 ——
# 否则正在用站的人用满 30 分钟会被硬踢下线。
TOKEN_TTL_SECONDS = 1800
# 剩余低于这个值才写 Redis 续期，避免每个请求都写一次（最坏 5 分钟写一次）。
TOKEN_REFRESH_BELOW = 1500
# 勾选「保持登录」时的会话时长（30 天）。与登录页的「记住账号密码」是两件事：
# 这个只管 token 活多久，账号密码回填由浏览器本地存储单独负责。
REMEMBER_TTL_HOURS = 24 * 30


def create_token(user_id: int, hours: float | None = None) -> str:
    ttl = int(hours * 3600) if hours else TOKEN_TTL_SECONDS
    token = secrets.token_urlsafe(32)
    r = get_redis()
    if r:
        r.setex(_token_key(token), ttl, str(user_id))
    else:
        # fallback
        if not hasattr(create_token, "_mem"):
            create_token._mem = {}
        create_token._mem[token] = {
            "user_id": user_id,
            "expires": datetime.utcnow() + timedelta(seconds=ttl),
        }
    return token


def touch_token(token: str) -> None:
    """滑动续期：把活跃会话的 TTL 拉回 30 分钟。

    只在「键存在且剩余 < 25 分钟」时续 —— 已过期（ttl=-2）或不存在的不续，
    不会把登出/被踢的 token 复活。Redis 故障时静默跳过，不能因为续期失败
    把正常请求变成 500（token 还有剩余时长，下次请求再试）。
    """
    r = get_redis()
    if not r:
        info = getattr(create_token, "_mem", {}).get(token)
        if info:
            info["expires"] = datetime.utcnow() + timedelta(seconds=TOKEN_TTL_SECONDS)
        return
    try:
        ttl = r.ttl(_token_key(token))
        if 0 < ttl < TOKEN_REFRESH_BELOW:
            r.expire(_token_key(token), TOKEN_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"token 滑动续期失败: {e}")


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


def revoke_user_tokens(user_id: int):
    """让某用户的所有已登录会话立即失效（Redis 和进程内兜底都清）。

    改密码/禁用账号后调用：旧 token 不能继续用新密码的账号。
    """
    r = get_redis()
    if r:
        try:
            for key in r.scan_iter(match="token:*", count=500):
                if r.get(key) == str(user_id):
                    r.delete(key)
        except Exception as e:
            logger.warning(f"清除用户 {user_id} 的 token 失败: {e}")
    mem = getattr(create_token, "_mem", None)
    if mem:
        for t in [t for t, v in mem.items() if v.get("user_id") == user_id]:
            mem.pop(t, None)


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
    # 滑动续期在 main.py 的 sliding_session 中间件里统一做（带 Bearer 的请求都算活跃），
    # 这里不再重复 touch，避免每个已登录请求多打一次 Redis。
    # ⚠️ 这里【不判会员到期】：到期只是失去预测权限，登录/浏览照常。
    # 会员门槛放在 require_member 里，只挡付费功能。
    return user


# ── 权限依赖 ──────────────────────────────────────────────
# 规则：游客可浏览历史/赛程；预测类功能必须【登录 + 会员】。

def is_local_request(request: Request) -> bool:
    """是不是本机脚本直连 127.0.0.1:8000（tools/*.py 的历史查询走这条）。

    外部请求经 cloudflared → nginx:8080 → uvicorn，nginx 一定会加
    `X-Forwarded-For`，Cloudflare 一定会加 `CF-Connecting-IP`；两个都没有、
    且 socket 对端是回环，才认作本机。这样既不用给一堆本地工具发 token，
    也不会因为某个头缺失就把公网请求误判成本机。
    """
    if request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for"):
        return False
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def require_login_or_local(
    request: Request,
    user: Optional[User] = Depends(get_current_user),
) -> Optional[User]:
    """历史/复盘类只读接口：登录用户放行；本机工具直连放行；公网匿名 401。"""
    if user:
        return user
    if is_local_request(request):
        return None
    raise HTTPException(401, "请先登录")


def require_login(user: Optional[User] = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def require_member(user: Optional[User] = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(401, "请先登录")
    if not is_member(user):
        # expires_at 有值 = 开过会员但已到期（如周卡用完了），要提示续费，
        # 不能和「从未开通」用同一句话，否则老用户看不出是哪种情况。
        if user.expires_at:
            raise HTTPException(403, "会员已到期，请续费后再使用预测功能")
        raise HTTPException(403, "该功能需要会员，请先开通会员")
    return user


def require_admin(user: Optional[User] = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(401, "请先登录")
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def _ensure_not_admin(user: User, action: str):
    """统一护栏：管理员账号是只读的，任何后台操作都不能修改它。

    禁用 / 启用 / 重置密码 / 开通会员 全部走这里 —— 防止误操作，
    也防止 token 被偷后把管理员锁死。角色和用户名双重判断。
    """
    if user.role == "admin" or user.username == "admin":
        raise HTTPException(400, f"管理员账号不允许{action}")



import io
import random
import string
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

def _captcha_key(cid: str) -> str:
    return f"captcha:{cid}"

@router.get("/captcha")
def get_captcha(request: Request):
    """生成图形验证码，返回图片 + captcha_id"""
    # 每张图都要跑一次 Pillow，限流防刷 CPU
    if not hit(f"captcha:ip:{client_ip(request)}", 60, 60):
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    cid = secrets.token_urlsafe(8)
    r = get_redis()
    if r:
        _store_captcha(cid, code)  # 5分钟有效
    # 生成图片
    img = Image.new("RGB", (160, 56), color=(30, 34, 43))
    draw = ImageDraw.Draw(img)
    font = None
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(path, 32)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=32)
        except TypeError:
            font = ImageFont.load_default()
    for i, ch in enumerate(code):
        draw.text((12 + i*36, 8), ch, fill=(230, 233, 239), font=font)
    # 干扰线
    for _ in range(3):
        draw.line([(random.randint(0,160), random.randint(0,56)),
                   (random.randint(0,160), random.randint(0,56))], fill=(74,158,255), width=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"X-Captcha-Id": cid, "Access-Control-Expose-Headers": "X-Captcha-Id"})


# 内存验证码兜底（Redis 不可用时）
_mem_captcha = {}

def _store_captcha(cid: str, code: str):
    r = get_redis()
    if r:
        r.setex(_captcha_key(cid), 300, code.lower())
    else:
        _mem_captcha[cid] = code.lower()

def verify_captcha(cid: str, code: str) -> bool:
    if not cid or not code:
        return False
    code = code.lower().strip()
    r = get_redis()
    if r:
        stored = r.get(_captcha_key(cid))
        if stored and stored == code:
            r.delete(_captcha_key(cid))
            return True
        return False
    # 内存兜底
    stored = _mem_captcha.pop(cid, None)
    return stored == code



@router.post("/register")
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    if not hit(f"reg:ip:{client_ip(request)}", 5, 600):
        raise HTTPException(429, "注册过于频繁，请稍后再试")
    if len(body.username) > 8:
        raise HTTPException(400, "用户名最长8位")
    if len(body.password) > 12:
        raise HTTPException(400, "密码最长12位")
    if is_weak_password(body.password):
        raise HTTPException(400, "密码太简单，请包含字母和数字，且不少于6位")
    if not verify_captcha(body.captcha_id, body.captcha_code):
        raise HTTPException(400, "验证码错误或已过期")
    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(400, "用户名已存在，请换一个")
    # 注册即可登录/浏览（游客级权限）；会员是单独的付费开通，
    # 付款后管理员用 /activate?username=xxx&plan=week 开通。
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role="user",
        is_active=True,
        expires_at=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_token(user.id)
    log_action(
        user_id=user.id, username=user.username, action_type="auth",
        action="注册", method="POST", path="/api/auth/register", status_code=200,
        ip=client_ip(request), ua=request.headers.get("user-agent", "")[:512],
        duration_ms=0,
    )
    return {"token": token, "user": _user_out(user)}


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    # 防密码爆破：按来源 IP 和按被打的用户名各一个桶。
    # ⚠️ 一次请求只能各记一次 hit()：曾经写成两个阈值对（10/5 和 20/10），
    #    同一个键被计两次，实际生效上限被砍半（IP 5 次 / 账号 3 次）。
    ip = client_ip(request)
    if not hit(f"login:ip:{ip}", 10, 300):
        raise HTTPException(429, "登录尝试过多，请5分钟后再试")
    if not hit(f"login:user:{body.username}", 5, 300):
        raise HTTPException(429, "该账号登录失败次数过多，请5分钟后再试")
    if not verify_captcha(body.captcha_id, body.captcha_code):
        raise HTTPException(400, "验证码错误或已过期")
    user = db.query(User).filter(User.username == body.username).first()
    # 两个分支分开报：没注册 vs 密码错。登录页按原话显示，不夹带用户名/密码细节。
    if not user:
        log_action(
            username=body.username, action_type="auth",
            action="登录失败：用户名不存在", method="POST",
            path="/api/auth/login", status_code=404, ip=ip,
            ua=request.headers.get("user-agent", "")[:512], duration_ms=0,
        )
        raise HTTPException(404, "当前用户名不存在")
    if not verify_password(body.password, user.password_hash):
        log_action(
            username=body.username, action_type="auth",
            action="登录失败：用户名或密码错误", method="POST",
            path="/api/auth/login", status_code=401, ip=ip,
            ua=request.headers.get("user-agent", "")[:512], duration_ms=0,
        )
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        log_action(
            user_id=user.id, username=user.username, action_type="auth",
            action="登录失败：账号已禁用", method="POST",
            path="/api/auth/login", status_code=403, ip=ip,
            ua=request.headers.get("user-agent", "")[:512], duration_ms=0,
        )
        raise HTTPException(403, "账号已禁用")
    # 旧格式（sha256 / pbkdf2）在成功登录后静默升级成 bcrypt（用户无感）
    if needs_password_upgrade(user.password_hash):
        user.password_hash = hash_password(body.password)
        db.commit()
    # 会员到期不影响登录，只影响预测功能（见 require_member）
    if user.expires_at and user.role != "admin":
        set_user_expiry(user.id, user.expires_at)
    token = create_token(user.id, hours=REMEMBER_TTL_HOURS if body.remember else None)
    log_action(
        user_id=user.id, username=user.username, action_type="auth",
        action="登录（保持登录）" if body.remember else "登录",
        method="POST", path="/api/auth/login", status_code=200, ip=ip,
        ua=request.headers.get("user-agent", "")[:512], duration_ms=0,
    )
    return {"token": token, "user": _user_out(user)}


@router.get("/me")
def me(user: User = Depends(require_login)):
    return _user_out(user)


@router.post("/logout")
def logout(request: Request, authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        uid = get_user_id_from_token(token)
        r = get_redis()
        if r:
            r.delete(_token_key(token))
        if uid:
            log_action(
                user_id=uid, action_type="auth", action="退出登录",
                method="POST", path="/api/auth/logout", status_code=200,
                ip=client_ip(request), ua=request.headers.get("user-agent", "")[:512],
                duration_ms=0,
            )
    return {"ok": True}

@router.post("/activate")
def activate_user(
    username: str,
    plan: Optional[str] = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """管理员开通会员：plan=week/month/quarter/year 时按套餐顺延到期时间。

    必须带管理员 token：Authorization: Bearer <admin token>。
    未过期的老会员续费时从现有到期时间往后加，不吞掉剩余天数。
    """
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    _ensure_not_admin(user, "开通会员")
    user.is_active = True
    if plan:
        _apply_plan(user, plan)
    db.commit()
    if user.expires_at and user.role != "admin":
        set_user_expiry(user.id, user.expires_at)
    exp_bj = _to_bj(user.expires_at)
    return {"msg": f"已激活 {username}，到期 {exp_bj:%Y-%m-%d %H:%M}（北京时间）",
            "is_member": is_member(user)}


@router.get("/admin/users")
def admin_list_users(db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    """管理员：用户列表（含会员状态），用于后台管理页"""
    users = db.query(User).order_by(User.id).all()
    return [_user_out(u) for u in users]


@router.post("/admin/users/{user_id}/toggle")
def admin_toggle_user(
    user_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """管理员：启用/禁用账号（管理员账号本身不允许禁用）"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    _ensure_not_admin(user, "禁用或启用")
    user.is_active = not user.is_active
    db.commit()
    if not user.is_active:
        revoke_user_tokens(user.id)   # 禁用后立刻踢下线，不等 token 自然过期
    return {"id": user.id, "username": user.username, "is_active": user.is_active}


class ResetPasswordIn(BaseModel):
    # 留空 = 自动生成随机密码；明文只在本次响应里返回一次，不落库
    password: str = Field(default="", max_length=64)


@router.post("/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    body: Optional[ResetPasswordIn] = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """管理员：重置用户密码。

    bcrypt 不可逆 → 只能重置、不能查看原密码。新密码只在本次响应的
    password 字段里出现一次（库里仍是 bcrypt），关掉页面就看不到了。
    重置成功会踢掉该用户所有已登录会话。
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    _ensure_not_admin(user, "重置密码（请用服务器上的 tools/init_admin.py）")
    pwd = (body.password or "").strip() if body else ""
    if pwd:
        if is_weak_password(pwd):
            raise HTTPException(400, "密码太简单，请包含字母和数字，且不少于6位")
    else:
        # 自动生成：去掉易混淆的 l/i/o/0/1，10 位够用且好口述
        pwd = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
    user.password_hash = hash_password(pwd)
    db.commit()
    revoke_user_tokens(user.id)
    return {"id": user.id, "username": user.username, "password": pwd}


@router.get("/check-username")
def check_username(username: str, request: Request, db: Session = Depends(get_db)):
    """检查用户名是否可用。

    注册页需要它，所以保持公开；但加 IP 限速 —— 这个接口只返回「存在/不存在」，
    不限速就是用户名枚举器。
    """
    if not hit(f"checkuser:ip:{client_ip(request)}", 30, 60):
        raise HTTPException(429, "查询过于频繁，请稍后再试")
    exists = db.query(User).filter(User.username == username).first() is not None
    return {"available": not exists, "username": username}


from app.models.recharge_log import RechargeLog
import random

def _mask_username(name: str) -> str:
    """滚动条只露首字，别把真实用户名整批送出去。"""
    s = (name or "").strip()
    return (s[0] + "***") if s else "***"


@router.get("/recharge-ticker")
def recharge_ticker(request: Request, db: Session = Depends(get_db)):
    """随机返回 30 条充值记录用于滚动条。

    这是登录页的装饰性数据，保持公开；但用户名必须脱敏 —— 原实现匿名返回
    30 条真实用户名 + 套餐 + 金额，多刷几轮就能收集用户和消费信息。
    """
    if not hit(f"ticker:ip:{client_ip(request)}", 60, 60):
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    total = db.query(RechargeLog).count()
    if total == 0:
        return []
    # 随机取 30 条
    ids = [r[0] for r in db.query(RechargeLog.id).all()]
    sample = random.sample(ids, min(30, len(ids)))
    rows = db.query(RechargeLog).filter(RechargeLog.id.in_(sample)).order_by(RechargeLog.created_at.desc()).all()
    return [
        {
            "username": _mask_username(r.username),
            "plan": r.plan,
            "amount": float(r.amount),
            "time": r.created_at.strftime("%m-%d %H:%M") if r.created_at else "",
        }
        for r in rows
    ]


@router.get("/menus")
def get_menus(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    """Get menus for current user based on role. Guest gets user menus."""
    from app.models.menu import Menu, role_menus
    role = user.role if user else "user"
    # Guest and non-member get basic menus
    if not user:
        role = "user"
    menus = db.query(Menu).join(role_menus).filter(
        role_menus.c.role == role,
        Menu.is_active == True
    ).order_by(Menu.sort_order).all()
    return [{"code": m.code, "name": m.name} for m in menus]
