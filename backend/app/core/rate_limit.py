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
    """Extract the client IP used as the rate-limit bucket key.

    Behind the Cloudflare Tunnel the only trustworthy source is
    ``CF-Connecting-IP``: Cloudflare sets it at the edge and overwrites anything
    the browser sent. ``X-Forwarded-For`` must NOT be used here — nginx appends
    to it (``$proxy_add_x_forwarded_for``), so its leftmost entry is whatever the
    caller typed, and an attacker could rotate it to get a fresh bucket per
    request. Direct local calls have no CF header and fall back to the real
    socket peer (127.0.0.1), which is fine because uvicorn only listens locally.
    """
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    return request.client.host if request.client else "unknown"
