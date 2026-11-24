"""轻量限流：防爆破 / 防刷接口。

优先用 Redis（多进程也一致）；Redis 不可用时退化到进程内滑动窗口，
功能不中断，只是重启后计数清零。
"""
import time
from collections import defaultdict, deque

from app.core.redis_client import get_redis

# Redis 不可用时的进程内兜底
_mem: dict[str, deque] = defaultdict(deque)


def hit(key: str, limit: int, window: int) -> bool:
    """记一次访问。返回 True=放行，False=超限。

    key 由调用方拼（如 login:ip:1.2.3.4），limit 为窗口内最大次数。
    """
    r = get_redis()
    if r:
        try:
            k = f"rl:{key}"
            n = r.incr(k)
            if n == 1:
                r.expire(k, window)
            return n <= limit
        except Exception:
            pass  # Redis 抖动 → 落到内存方案
    now = time.time()
    q = _mem[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def client_ip(request) -> str:
    """真实来源 IP。

    服务在 Cloudflare / bore 隧道后面，request.client.host 永远是 127.0.0.1，
    所以优先读隧道带的头。X-Forwarded-For 可以被伪造 —— 这里只拿它当限流分桶键，
    即使伪造也只能绕过单个桶，不影响鉴权。
    """
    h = request.headers
    ip = (h.get("cf-connecting-ip")
          or (h.get("x-forwarded-for", "").split(",")[0].strip())
          or (request.client.host if request.client else ""))
    return ip or "unknown"
