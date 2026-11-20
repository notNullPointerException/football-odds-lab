"""Redis 客户端（用于用户到期倒计时和 token）"""
import redis
from loguru import logger
from app.core.config import settings

_redis = None

def get_redis():
    global _redis
    if _redis is None:
        try:
            _redis = redis.Redis(
                host=getattr(settings, "redis_host", "127.0.0.1"),
                port=getattr(settings, "redis_port", 6379),
                db=getattr(settings, "redis_db", 0),
                password=getattr(settings, "redis_password", None) or None,
                decode_responses=True,
                socket_connect_timeout=3,
            )
            _redis.ping()
            logger.info("Redis 已连接")
        except Exception as e:
            logger.warning(f"Redis 连接失败，将回退到内存模式: {e}")
            _redis = None
    return _redis
