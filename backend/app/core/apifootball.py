"""API-Football 错误检测 + 带重试的请求。

api-sports 把**错误也当 HTTP 200 返回**，形如：

    {"errors": {"plan": "Free plans do not have access to this season..."},
     "response": []}

`raise_for_status()` 不会抛（状态码是 200），`data.get("response", [])` 拿到空数组，
于是各同步服务把「被拒绝」当成「这个联赛没有数据」静默跳过，循环跑完照样打印
「同步完成」—— 日志看起来一切正常，实际一个字节都没写。

2026-09-18 的 Free 档掉级事件就是被这个假象瞒了一整天，所以统一在这里做检测。

另外网络层会偶发 SSL 断流（`UNEXPECTED_EOF_WHILE_READING`），长循环里一次抖动
就让整个联赛作废（分页只写了一半）。`api_get` 负责退避重试。

🔴 2026-10-06 结案：那条 SSL 断流的根因**不是** TLS、不是 openssl、更不是
「api-sports 挂了」，而是 **macOS 系统代理**。

    系统代理指向 127.0.0.1:17890（一个已经不响应任何请求的代理）
    Python 的 HTTP 库默认读系统代理 —— httpx / requests / urllib 全走
        `urllib.request.getproxies()`（在 macOS 上是 `scutil --proxy`）
    curl 不读系统代理 -> 直连 -> 200

于是同一个 URL：curl 200、裸 socket+ssl 200、httpx 全挂。排查时会一路误判成
「通道断了 / 没开盘 / openssl 装坏了」，因为**唯一正常的那条路(直连)恰好是 curl 走的**。

api-sports.io 直连可达（实测 200），所以这里一律 `trust_env=False` 绕开系统代理。
⚠️ **别把这个 kwarg 传染给 DeepSeek 调用**（`predict.py:_call_llm`）：那个 host
走代理是通的，套上去反而多担一份风险。
"""
import random
import ssl
import time

import httpx
from loguru import logger

from app.core.config import settings

# 这些状态码值得重试：429 限流 + 5xx 服务端抖动
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 传输层异常：httpx 包装过的 + 裸 SSL/socket 错误
# （SSL UNEXPECTED_EOF 有时不被 httpx 包装，直接抛 ssl.SSLError/OSError）
_RETRYABLE_EXC = (httpx.TransportError, ssl.SSLError, OSError)


def api_error(data) -> str | None:
    """提取 API 返回的错误描述；没有错误时返回 None。

    errors 字段可能是 {} / {"plan": "..."} / ["..."] 几种形状。
    """
    if not isinstance(data, dict):
        return None

    errors = data.get("errors")
    if not errors:
        return None

    if isinstance(errors, dict):
        return "; ".join(f"{k}: {v}" for k, v in errors.items())
    if isinstance(errors, list):
        return "; ".join(str(e) for e in errors)
    return str(errors)


def check_api_error(data, context: str) -> bool:
    """检测并记录 API 错误。有错记 ERROR 日志并返回 True，调用方应 continue/break。

    用法：
        if check_api_error(data, f"拉取联赛 {lid} 积分榜"):
            continue
    """
    err = api_error(data)
    if err:
        logger.error(f"{context}: API 拒绝 -> {err}")
        return True
    return False


def raise_if_rejected(data, context: str) -> None:
    """API 层拒绝（HTTP 200 但 errors 非空，如 rateLimit/plan）直接抛异常。

    与 `check_api_error` 的区别：后者返回 True 让调用方 continue，适用于
    「这页没数据就跳过下一批」的分页场景；但**单个对象的取数**不能用它——
    调用方 `_fetch_one` 靠异常判断成败，静默 return 会让失败的对象照样被标记
    「已同步」（fixture_service 2026-09-24 的假绿 bug 就是这么来的）。
    """
    err = api_error(data)
    if err:
        raise RuntimeError(f"{context}: API 拒绝 -> {err}")


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """退避秒数：指数 1s/2s/4s + 抖动；429 带 Retry-After 时优先听服务端的。"""
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except (TypeError, ValueError):
            pass
    return 2 ** (attempt - 1) + random.uniform(0, 0.5)


def api_get(
    path: str,
    params: dict | None = None,
    *,
    context: str = "",
    timeout: float = 30.0,
    attempts: int = 3,
) -> dict:
    """GET API-Football 并返回解析后的 JSON，传输层失败时退避重试。

    path 形如 "/players"（base_url 从 settings 取）。
    重试对象：传输层异常（SSL 断流/连接重置/超时）+ 429 + 5xx；
    其他 4xx 直接抛给调用方（重试也不会变）。
    全部尝试失败后抛最后一个异常，调用方照旧 try/except 即可。
    """
    url = f"{settings.api_football_base_url}{path}"
    headers = {"x-apisports-key": settings.api_football_key}
    label = context or path

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        retry_after = None
        try:
            # trust_env=False: 绕开系统代理直连，理由见模块 docstring
            response = httpx.get(url, headers=headers, params=params, timeout=timeout,
                                 trust_env=False)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _RETRYABLE_STATUS:
                raise
            last_exc = e
            retry_after = e.response.headers.get("Retry-After")
        except _RETRYABLE_EXC as e:
            last_exc = e

        if attempt < attempts:
            delay = _retry_delay(attempt, retry_after)
            logger.warning(
                f"{label}: 第 {attempt}/{attempts} 次失败"
                f"({last_exc.__class__.__name__}: {last_exc})，{delay:.1f}s 后重试"
            )
            time.sleep(delay)

    logger.error(f"{label}: {attempts} 次尝试全部失败 -> {last_exc}")
    raise last_exc
