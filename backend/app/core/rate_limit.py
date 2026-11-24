"""Simple in-memory rate limiter."""
import time
from collections import defaultdict

_hits = defaultdict(list)

def hit(key: str, limit: int, window: int) -> bool:
    """Return True if under limit, False if rate limited.
    key: unique identifier (e.g. "login:ip:1.2.3.4")
    limit: max hits allowed
    window: time window in seconds
    """
    now = time.time()
    # Clean old hits
    _hits[key] = [t for t in _hits[key] if now - t < window]
    if len(_hits[key]) >= limit:
        return False
    _hits[key].append(now)
    return True

def client_ip(request) -> str:
    """Extract client IP from request."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
