import asyncio
from datetime import datetime, timedelta

from sqlalchemy import text
from loguru import logger

from app.db.session import SessionLocal
from app.services.league_service import sync_leagues
from app.services.team_service import sync_teams
from app.services.player_service import sync_players
from app.services.standing_service import sync_standings
from app.services.fixture_service import sync_fixtures, sync_live_fixtures
from app.services.prediction_result_service import backfill_results
from app.services.auto_predict_service import auto_predict
from app.services.bet_journal_service import weekly_review_job
from app.services.odds_watch_service import odds_watch_job
from app.services.sfc_archive_service import sfc_archive_job
from app.services.sfc_history_service import sfc_result_sync_job

LIVE_INTERVAL_SECONDS = 2 * 60

# ──── 默认任务定义 ────
_DEFAULT_TASKS = {
    "league_sync":    {"name": "联赛数据同步",   "start_hour": 2,   "fn": sync_leagues},
    "team_sync":      {"name": "球队数据同步",   "start_hour": 2.5, "fn": sync_teams},
    "player_sync":    {"name": "球员数据同步",   "start_hour": 3,   "fn": sync_players},
    "standing_sync":  {"name": "积分榜数据同步", "start_hour": 4,   "fn": sync_standings},
    "fixture_daily":  {"name": "赛程每日同步",   "start_hour": 5,   "fn": sync_fixtures},
    "fixture_live":   {"name": "赛程实时同步",   "start_hour": None, "fn": sync_live_fixtures,
                       "interval_seconds": LIVE_INTERVAL_SECONDS},
    "backfill_pred":  {"name": "预测结果回填",   "start_hour": None, "fn": backfill_results,
                       "interval_seconds": 3600},
    "auto_predict":   {"name": "赛前自动预测",   "start_hour": 12,  "fn": auto_predict,
                       "interval_seconds": None},
    # 周复盘: scheduler 无周粒度, 用每日 09:06 触发, 非周一函数内部直接返回
    "bet_review":     {"name": "竞彩周复盘",     "start_hour": 9.1, "fn": weekly_review_job,
                       "interval_seconds": None},
    # 竞彩/锐盘赔率快照 —— 攒 [[football-jc-pinnacle-bias]] 要判的配对样本。
    #
    # 【变更史，别再来回翻】
    #   2026-09-21 用户：「算了 不要你自己抓 我会告诉你，然后你去抓」→ is_enabled=0，
    #                        理由是他要控制什么时候动数据。当时 comment 写着「别改回 1」。
    #   2026-10-07 用户改口：**改成自动，两个时段**。原因：fixture_id 映射修通之后，
    #                        样本量才是唯一的瓶颈（判 1.5pp 要 ~12 场、判 1.0pp 要 ~26 场），
    #                        手动跑必然漏天。→ 本 comment 与 [football-jc-fixtures-source]
    #                        里那条「不自动抓」的规矩同步作废，**别再照着旧规矩改回 0**。
    #
    # 🔴 为什么是**两条行**而不是一条：`_run_loop` 每轮只算一个 target
    #    （`start_hour` 是 double，DB 里也只有一个列），一个任务一天只能跑一次。
    #    要让同一天跑两回，最省事又不碰共享 loop 逻辑（另 6 个任务在跑）的办法
    #    就是注册两个 id、共用同一个 `odds_watch_job`。合并回一条需要给
    #    `start_hour` 加个 list 语义 + 改 `_run_loop`，风险不值当。
    #
    # 两条时间都刻意避开已在跑的 08:30 / 08:41 / 09:00 / 09:05 / 09:30。
    # 手动触发仍是 `POST /api/scheduler/odds_watch/trigger`（两条都保留）。
    "odds_watch":       {"name": "赔率快照(傍晚)", "start_hour": 17.1, "fn": odds_watch_job,
                         "interval_seconds": None},
    "odds_watch_late":  {"name": "赔率快照(夜间)", "start_hour": 22.1, "fn": odds_watch_job,
                         "interval_seconds": None},
    # ── 胜负彩（传统足彩 14 场）两条数据链 —— 2026-10-16 加 ──────────────
    # 之前这两件事【都没有定时任务】：存档绑在"打开胜负彩页"上、彩果靠手动跑 CLI。
    # 代价是实测 3342 期里只有 7 期有 archived_at，而存档【漏了不可逆】
    # （上游只返回当前期，翻期后对阵/赔率/彩果全没）。详见 sfc_archive_service 模块头。
    #
    # 时刻一律用 .25 —— `minute = int((start_hour % 1) * 60)`，浮点截断方向随数值变：
    # 实测 17.1→17:06 但 9.1→09:05（注释里写的 09:06 是错的）。0.25 在二进制里精确，
    # int(0.25*60) 恒 = 15，不给自己留差一分钟的惊喜。三条都避开了已在跑的
    # 08:30/08:42/08:54/09:00/09:05/09:30/12:00/17:06/22:06。
    #
    # 🔴 存档为什么是【两条】：与 odds_watch 同理 —— `_run_loop` 每轮只算一个 target
    #    （start_hour 是 double，DB 只有一个列），一个 id 一天只能跑一次。
    #    两条共用 sfc_archive_job，别合并。
    #    13:15 兜早停售的期（19:05/20:30/21:00）和当天短窗；21:15 拿停售前最近的价，
    #    并负责把当天跑完的三头收进快照（头块是 COALESCE 填空白，先存后跑也能收）。
    "sfc_result_sync":  {"name": "胜负彩赛果同步", "start_hour": 10.25, "fn": sfc_result_sync_job,
                         "interval_seconds": None},
    "sfc_archive":      {"name": "胜负彩存档(午间)", "start_hour": 13.25, "fn": sfc_archive_job,
                         "interval_seconds": None},
    "sfc_archive_eve":  {"name": "胜负彩存档(晚间)", "start_hour": 21.25, "fn": sfc_archive_job,
                         "interval_seconds": None},
}

_active_loops: dict[str, asyncio.Task] = {}


def _seed_defaults():
    db = SessionLocal()
    try:
        for k, v in _DEFAULT_TASKS.items():
            sh = v.get("start_hour")
            iv = v.get("interval_seconds")
            # 默认启用；个别任务（如手动触发的 odds_watch）自带 is_enabled=0
            db.execute(
                text(
                    """INSERT IGNORE INTO scheduler_tasks
                       (id, name, interval_seconds, start_hour, is_enabled)
                       VALUES (:id, :name, :iv, :sh, :en)"""
                ),
                {"id": k, "name": v["name"], "iv": iv, "sh": sh,
                 "en": v.get("is_enabled", 1)},
            )
        db.commit()
    finally:
        db.close()


def _read_db_row(task_key: str) -> dict | None:
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT * FROM scheduler_tasks WHERE id = :id"), {"id": task_key}
        ).fetchone()
        return dict(row._mapping) if row else None
    finally:
        db.close()


def _update_db(task_key: str, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k} = :{k}" for k in kwargs)
    db = SessionLocal()
    try:
        db.execute(
            text(f"UPDATE scheduler_tasks SET {sets} WHERE id = :id"),
            {"id": task_key, **kwargs},
        )
        db.commit()
    finally:
        db.close()


def run_task_with_log(task_id: str, task_name: str, sync_fn, db=None):
    """执行任务并记录日志（供定时 loop 和手动触发共用）"""
    log_id = _log_start(task_id, task_name)
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        sync_fn(db)
        _log_finish(log_id, 'success', None)
    except Exception as e:
        _log_finish(log_id, 'failed', str(e))
        raise
    finally:
        if own_db:
            db.close()


def get_scheduler_status() -> dict:
    db = SessionLocal()
    try:
        rows = db.execute(text("SELECT * FROM scheduler_tasks ORDER BY id")).fetchall()
        tasks = []
        for row in rows:
            r = dict(row._mapping)
            sh = r.get("start_hour")
            iv = r.get("interval_seconds")
            if sh is not None:
                desc = f"每天 {int(sh):02d}:{int((sh % 1) * 60):02d}"
            else:
                desc = _desc_interval(iv or 0)
            tasks.append({
                "task_id": r["id"],
                "name": r["name"],
                "start_hour": sh,
                "interval_seconds": iv,
                "interval_desc": desc,
                "is_enabled": bool(r["is_enabled"]),
                "last_run": r["last_run"].isoformat() if r["last_run"] else None,
                "next_run": r["next_run"].isoformat() if r["next_run"] else None,
                "is_running": bool(r["is_running"]),
            })
        return {"tasks": tasks}
    finally:
        db.close()


def _log_start(task_id: str, task_name: str) -> int:
    """记录任务开始执行，返回 log id"""
    db = SessionLocal()
    try:
        result = db.execute(
            text(
                """INSERT INTO scheduler_logs (task_id, task_name, status, started_at)
                   VALUES (:tid, :name, 'running', :now)"""
            ),
            {"tid": task_id, "name": task_name, "now": datetime.now()},
        )
        db.commit()
        return result.lastrowid
    finally:
        db.close()


def _log_finish(log_id: int, status: str, message: str | None):
    db = SessionLocal()
    try:
        db.execute(
            text(
                """UPDATE scheduler_logs SET status=:st, message=:msg, finished_at=:now
                   WHERE id=:id"""
            ),
            {"st": status, "msg": message, "now": datetime.now(), "id": log_id},
        )
        db.commit()
    finally:
        db.close()


def _desc_interval(sec: int) -> str:
    if sec < 120:
        return f"每 {sec} 秒"
    if sec < 3600:
        return f"每 {sec // 60} 分钟"
    return f"每 {sec // 3600} 小时"


def stop_scheduled_task(task_key: str) -> bool:
    row = _read_db_row(task_key)
    if row is None:
        return False
    task = _active_loops.get(task_key)
    if task and not task.done():
        task.cancel()
    _update_db(task_key, is_enabled=0, is_running=0, next_run=None)
    logger.info(f"[Scheduler] {row['name']} 已停止")
    return True


def start_scheduled_task(task_key: str) -> bool:
    row = _read_db_row(task_key)
    if row is None:
        return False
    if row["is_enabled"]:
        return False
    _update_db(task_key, is_enabled=1)
    _start_loop(task_key)
    logger.info(f"[Scheduler] {row['name']} 已启动")
    return True


def update_task(task_key: str, start_hour: float = None, interval_seconds: int = None,
                is_enabled: bool = None) -> bool:
    row = _read_db_row(task_key)
    if row is None:
        return False

    kwargs = {}
    if start_hour is not None:
        kwargs["start_hour"] = start_hour
    if interval_seconds is not None:
        kwargs["interval_seconds"] = interval_seconds
    if is_enabled is not None:
        kwargs["is_enabled"] = 1 if is_enabled else 0
    if kwargs:
        _update_db(task_key, **kwargs)

    # 重启 loop
    task = _active_loops.get(task_key)
    if task and not task.done():
        task.cancel()

    if is_enabled is False:
        _update_db(task_key, is_running=0, next_run=None)
    elif is_enabled is True or kwargs:
        new_row = _read_db_row(task_key)
        if new_row and new_row["is_enabled"]:
            _start_loop(task_key)

    logger.info(f"[Scheduler] {row['name']} 已更新: {kwargs}")
    return True


# ──── Loop ────

def _start_loop(task_key: str):
    definition = _DEFAULT_TASKS.get(task_key)
    if not definition:
        return
    loop = asyncio.create_task(_run_loop(task_key))
    _active_loops[task_key] = loop


async def _run_loop(task_key: str):
    definition = _DEFAULT_TASKS[task_key]
    sync_fn = definition["fn"]

    while True:
        row = _read_db_row(task_key)
        if not row or not row["is_enabled"]:
            break

        sh = row.get("start_hour")
        iv = row.get("interval_seconds")

        # 🔴 纯防御闸：两个都没配的任务不能进循环。
        # 必须在这里挡住：下面 interval 分支会退化成 `delay = iv or 60`，
        # 也就是【每 60 秒抓一次】—— 一旦被 enabled 就会打爆外部接口。
        # ⚠️ 这里原来写的例子是「手动任务（如 odds_watch）」，2026-10-07 odds_watch
        #    改成自动（17:06 + 22:06）之后**这个例子失效了**，别再拿它当「手动任务长这样」的样板。
        #    闸门本身仍然要留：它的作用是「配置缺失时宁可停机也不空转」，与有没有手动任务无关。
        if sh is None and iv is None:
            logger.info(f"[Scheduler] {row['name']} 是手动任务，不自动循环")
            break

        # 计算下一执行时间
        if sh is not None:
            # 定时任务：每天固定时间
            now = datetime.now()
            target = now.replace(hour=int(sh), minute=int((sh % 1) * 60), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            delay = (target - now).total_seconds()
            next_run = target
        else:
            # 间隔任务
            delay = iv or 60
            next_run = datetime.now() + timedelta(seconds=delay)

        _update_db(task_key, next_run=next_run)
        logger.info(
            f"[Scheduler] {row['name']} 已调度"
            + (f", 下次 {next_run.strftime('%H:%M')}" if sh else f", 间隔 {_desc_interval(delay)}")
        )

        await asyncio.sleep(delay)

        row = _read_db_row(task_key)
        if not row or not row["is_enabled"]:
            break

        _update_db(task_key, is_running=1, last_run=datetime.now())
        logger.info(f"[Scheduler] 触发: {row['name']}")

        try:
            await asyncio.to_thread(run_task_with_log, task_key, row['name'], sync_fn)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[Scheduler] {row['name']} 异常: {e}")
        finally:
            _update_db(task_key, is_running=0)


# ──── 初始化 ────

def init_scheduler():
    _seed_defaults()
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id FROM scheduler_tasks WHERE is_enabled = 1")
        ).fetchall()
        for row in rows:
            _start_loop(row[0])
            logger.info(f"[Scheduler] 加载: {row[0]}")
    finally:
        db.close()


async def shutdown_scheduler():
    for key, task in list(_active_loops.items()):
        if not task.done():
            task.cancel()
    for key, task in list(_active_loops.items()):
        try:
            await task
        except asyncio.CancelledError:
            pass
    _active_loops.clear()
    logger.info("[Scheduler] 全部任务已取消")


league_sync_loop = None
team_sync_loop = None
player_sync_loop = None
standing_sync_loop = None
fixture_daily_loop = None
fixture_live_loop = None
