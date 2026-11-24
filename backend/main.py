import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.core.log_config import setup_logger
from app.api.leagues import router as leagues_router
from app.api.teams import router as teams_router
from app.api.players import router as players_router
from app.api.standings import router as standings_router
from app.api.fixtures import router as fixtures_router
from app.api.scheduler import router as scheduler_router
from app.api.predictions import router as predictions_router
from app.api.jc import router as jc_router
from app.api.sfc import router as sfc_router
from app.api.auth import router as auth_router, touch_token
from app.services.scheduler import init_scheduler, shutdown_scheduler

# 初始化日志
setup_logger()


# asyncio.to_thread 的 worker 数。默认是 min(32, cpu+4)，这台 10 核只有 14 个 ——
# 而「跑 14 场预测」正好要 14 个并发，定时任务（每 2 分钟一次的赛程同步也走 to_thread）
# 一占，最后那几场就得排队。实测见过 14 场被切成两波、中间断 17 分钟。
#
# 为什么可以调大：预测的瓶颈是等 LLM / api-football 的网络返回，不占 GIL，多给线程是安全的。
# 为什么不能无限大：**每个预测会全程独占一条 DB 连接**（predict_fixture 从 SessionLocal()
# 一直持有到 finally 才 close，中间跨着最长 90 秒的 LLM 调用），而连接池是
# app/db/session.py 的 pool_size=10 + max_overflow=20 = 30。取 24：装得下 14 场并发
# 外加定时任务和几个普通接口，又给连接池留 6 条余量。改这个数先回头看连接池。
_THREAD_WORKERS = 24


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 注册定时任务"""
    # 注意: 禁用了 alembic 自动迁移，因为 autogenerate 会删除没有 SQLAlchemy 模型的表
    # （predictions、scheduler_tasks、scheduler_logs 使用 raw SQL 管理）
    # 新增表请手动建表或手动编写迁移

    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=_THREAD_WORKERS, thread_name_prefix="worker"))
    logger.info(f"线程池 worker 数 = {_THREAD_WORKERS}（asyncio.to_thread 用）")

    logger.info("正在注册定时任务...")
    init_scheduler()

    try:
        yield
    except BaseException:
        pass
    finally:
        await shutdown_scheduler()
        logger.info("定时任务已取消")


app = FastAPI(
    title="Football API Proxy",
    description="API-Football v3.9.3 代理服务",
    version="1.0.0",
    lifespan=lifespan,
)

# ⚠️ 不再开后门 CORS：前端与 API 同源（同一个 8000 端口），不需要跨域。
# 原来的 allow_origins=["*"] + allow_credentials=True 等于允许任意网站带凭据调本 API。


@app.middleware("http")
async def security_headers(request, call_next):
    """基础安全响应头（防 MIME 嗅探 / 点击劫持 / 外链脚本注入）。"""
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    # 页面全部脚本/样式内联，所以 'unsafe-inline' 必须留；但外域脚本一律禁止。
    h.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: blob: https:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'",
    )
    return response


@app.middleware("http")
async def sliding_session(request, call_next):
    """滑动的会话过期（Spring Session 式空转超时，不是登录后固定 30 分钟）。

    只要有请求带着 Bearer token，就把 Redis 里该 token 的 TTL 拉回 30 分钟
    （剩余 < 25 分钟才写，见 touch_token）。放在中间件而不是 get_current_user：
    赛程/竞彩/体彩这些公开浏览接口没有 auth 依赖，用户一直翻它们也必须算活跃，
    否则正在用站的人还是会被登录满 30 分钟硬踢下线。

    Redis 操作丢线程池，不阻塞事件循环；续期失败只记日志，不影响正常请求。
    """
    authorization = request.headers.get("authorization") or ""
    if authorization.startswith("Bearer "):
        try:
            await asyncio.to_thread(touch_token, authorization[7:])
        except Exception:
            pass
    return await call_next(request)

# 注册路由
app.include_router(leagues_router, prefix="/api", tags=["Leagues"])
app.include_router(teams_router, prefix="/api", tags=["Teams"])
app.include_router(players_router, prefix="/api", tags=["Players"])
app.include_router(standings_router, prefix="/api", tags=["Standings"])
app.include_router(fixtures_router, prefix="/api", tags=["Fixtures"])
app.include_router(scheduler_router, prefix="/api", tags=["Scheduler"])
app.include_router(predictions_router, prefix="/api", tags=["Predictions"])
app.include_router(jc_router, prefix="/api", tags=["JC"])
app.include_router(sfc_router, prefix="/api", tags=["SFC"])
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])


@app.get("/api/health")
async def health():
    """健康检查"""
    return {"status": "ok"}


# 前端(零构建静态页) —— 必须挂在所有 /api 路由之后，
# 否则 mount("/") 会先匹配，把 /api/* 也当成静态文件去磁盘找。
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    # 音乐等媒体文件
    _MEDIA_DIR = Path(__file__).resolve().parent / "media"
    if _MEDIA_DIR.exists():
        app.mount("/media", StaticFiles(directory=str(_MEDIA_DIR)), name="media")

    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
else:
    logger.warning(f"静态目录不存在，前端不可用: {_STATIC_DIR}")
