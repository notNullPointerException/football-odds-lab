"""用户操作日志：写入、查询参数清洗、过期清理。

设计约束：
- 只记「谁 + 什么时候 + 调了什么/点了什么」，**不记请求体**，密码和验证码进不来。
- 查询参数里的 token/password 等键统一打码后再落库。
- 写日志失败绝不影响业务请求，只记一条 warning。
"""
import asyncio
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode

from loguru import logger

from app.db.session import SessionLocal
from app.models.user import User
from app.models.user_action_log import UserActionLog

RETENTION_DAYS = 30
USERNAME_CACHE_SECONDS = 600

# 这些键出现在 query string 里就打成 ***（正常接口不会带，防御性处理）
_SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "token", "access_token", "refresh_token",
    "authorization", "auth", "secret", "captcha_code",
}

_name_cache: dict[int, tuple[str | None, float]] = {}
_tasks: set = set()
_cleanup_lock = threading.Lock()
_last_cleanup = 0.0


def _cut(value, limit: int) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= limit else s[:limit]


def sanitize_query(raw: str | None) -> str:
    """把查询参数里的敏感键打码，并限制长度。"""
    if not raw:
        return ""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True)
    except Exception:
        return _cut(raw, 1000) or ""
    cleaned = [
        (k, "***" if k.lower() in _SENSITIVE_KEYS else v)
        for k, v in pairs
    ]
    return _cut(urlencode(cleaned, doseq=True), 1000) or ""


def _username_for(user_id: int) -> str | None:
    now = time.time()
    cached = _name_cache.get(user_id)
    if cached and cached[1] > now:
        return cached[0]
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        name = user.username if user else None
    except Exception:
        name = None
    finally:
        db.close()
    _name_cache[user_id] = (name, now + USERNAME_CACHE_SECONDS)
    return name


def cleanup_old_logs(force: bool = False) -> int:
    """删除超过保留期的日志，返回删除条数。

    force=False：每 24 小时最多跑一次（写日志时顺带触发，作为兜底）；
    force=True ：定时任务入口用，不受节流限制（每天 03:30 由 scheduler 调用）。
    """
    global _last_cleanup
    now = time.time()
    with _cleanup_lock:
        if not force and now - _last_cleanup < 86400:
            return 0
        _last_cleanup = now
    db = SessionLocal()
    try:
        cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
        n = (
            db.query(UserActionLog)
            .filter(UserActionLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        if n:
            logger.info(f"[audit] 清理 {n} 条超过 {RETENTION_DAYS} 天的操作日志")
        return n
    except Exception as e:
        db.rollback()
        logger.warning(f"[audit] 清理旧日志失败: {e}")
        return 0
    finally:
        db.close()


def audit_cleanup_job(db=None) -> None:
    """scheduler 定时任务入口（每日 03:30）。db 参数由 run_task_with_log 传入，这里不用。"""
    n = cleanup_old_logs(force=True)
    logger.info(f"[audit] 每日清理完成：删除 {n} 条（保留 {RETENTION_DAYS} 天）")


def write_action(
    *,
    user_id: int | None = None,
    username: str | None = None,
    action_type: str = "api",
    action: str | None = None,
    method: str | None = None,
    path: str | None = None,
    query: str | None = None,
    status_code: int | None = None,
    ip: str | None = None,
    ua: str | None = None,
    duration_ms: int | None = None,
) -> None:
    try:
        if not username and user_id:
            username = _username_for(user_id)
        db = SessionLocal()
        try:
            db.add(UserActionLog(
                user_id=user_id,
                username=_cut(username, 64),
                action_type=(action_type or "api")[:16],
                action=_cut(action, 128),
                method=_cut(method, 8),
                path=_cut(path, 255),
                query=_cut(query, 1024),
                status_code=status_code,
                ip=_cut(ip, 64),
                ua=_cut(ua, 512),
                duration_ms=duration_ms,
            ))
            db.commit()
        finally:
            db.close()
        cleanup_old_logs()
    except Exception as e:
        logger.warning(f"[audit] 写入失败: {e}")


def log_action(**kwargs) -> None:
    """异步落库：在事件循环里不阻塞响应；同步上下文（如线程池里的登录）直接写。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        write_action(**kwargs)
        return
    task = loop.create_task(asyncio.to_thread(write_action, **kwargs))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
