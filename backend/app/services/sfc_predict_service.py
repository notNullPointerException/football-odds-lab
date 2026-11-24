# -*- coding: utf-8 -*-
"""胜负彩赛前批量预测：当前期 14 场逐场走 HTTP，再把结果归档进 sfc_issue_matches。

为什么要有这个 job（2026-11-11 加）：

  · `sfc_archive_job` 只做快照。`archive_payload()` 从 `/api/sfc/matches` 拿到
    `pred` / `llm` 后写库，**自己从不触发预测**；没有 predictions 行时，
    `sfc_issue_matches` 的 prob_* / llm_* 只能保持 NULL。
  · 唯一的自动预测任务 `auto_predict` 自 2026-06-11 起 `is_enabled=0`；即便打开，
    它也只覆盖 `leagues.enabled=1` 且落在「今天 10:10 ~ 次日 10:10」窗口内的比赛。
    胜负彩常含 disabled 联赛（欧国联就是）和 2~3 天后的场次，天然覆盖不到。
  · 所以这里只按【胜负彩自己的 14 场】取数，有 fixture_id 的逐场
    `POST /api/predict/{id}`，跑完再 `POST /api/sfc/archive` 强制归档。
    归档走的是原有的 COALESCE 路径：先有 predictions，再把头块填进快照。

红线：

  · **只预测未开球的场次**（kickoff > now + 安全垫）。停售后补历史期等于拿赛果喂
    模型，模型 vs 盘口的检验会全废；宁可留 NULL，也不写一条事后预测。
  · **没有 fixture_id 的场次只告警、不猜**。根因通常是 fixtures 没同步（上游额度/
    订阅），不是映射算法；重建映射也没用，写个假 id 反而会把别的比赛贴进来。
  · 预测必须走 HTTP `/api/predict/{id}`（[[接口调用铁律]]），
    不 import `predict_fixture`，也不在这里直连 predictions 表。

时间安排（见 scheduler.py）：11:45 覆盖 19:05/20:30 早停售，19:45 覆盖 21:00/22:00
晚停售；13:15 / 21:15 的存档任务随后再把头块 COALESCE 一遍兜底。
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import httpx
from loguru import logger

# 本 job 是在 uvicorn 进程里跑的，默认回环到同一个服务。端口可用环境变量覆盖，
# 避免测试/换端口时写死。
BASE_URL = os.getenv("SFC_API_BASE", "http://127.0.0.1:8000")

# 开球前多久停止预测。给 LLM + 写库留出余量，也避免在开球哨响后补一条。
SAFETY_MINUTES = 30

# 与 tools/predict_http.py 保持一致：3 路并发。单场完整预测约 25~90 秒。
MAX_WORKERS = 3

PREDICT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
FETCH_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _parse_dt(value) -> datetime | None:
    """解析体彩返回的 "YYYY-MM-DD HH:MM:SS" / "YYYY-MM-DD HH:MM"。

    解析不了就返回 None —— 调用方据此跳过，不拿 now 冒充开球时间。
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _fetch_payload(client: httpx.Client) -> dict:
    """强制刷新当前期。`refresh=true` 绕过 /api/sfc/matches 的 120 秒缓存。"""
    r = client.get("/api/sfc/matches", params={"refresh": "true"}, timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _predict_one(client: httpx.Client, fid: int, label: str) -> tuple[str, str, str]:
    """单场预测。返回 (status, label, detail)，status = ok / fail。"""
    try:
        r = client.post(f"/api/predict/{fid}", timeout=PREDICT_TIMEOUT)
    except Exception as e:
        return "fail", label, f"{type(e).__name__}: {e}"

    if r.status_code != 200:
        return "fail", label, f"HTTP {r.status_code}: {r.text[:200]}"

    try:
        data = r.json()
    except ValueError:
        return "fail", label, "HTTP 200 但返回不是 JSON"

    llm = ((data.get("result") or {}).get("llm") or {})
    tag = ""
    if llm.get("win"):
        tag = f"  LLM {llm.get('win')}({llm.get('win_pct') or '?'}) {llm.get('score') or ''}".rstrip()
    return "ok", label, tag


def sfc_predict_job(db=None) -> dict:
    """定时任务入口（scheduler 会传一个 db session，这里不用，预测走 HTTP）。

    返回统计 dict，方便手动触发时看结果；scheduler 会忽略返回值。
    """
    stats = {
        "issue": None,
        "total": 0,
        "mapped": 0,
        "predicted": 0,
        "already": 0,
        "skipped": 0,
        "failed": 0,
        "unmapped": 0,
    }
    now = datetime.now()

    # trust_env=False：本机有失效的系统代理（127.0.0.1:17890）时，
    # Python 的 httpx/urllib 会读它，curl 不读 —— 见 apifootball.py 的同款处理。
    with httpx.Client(base_url=BASE_URL, trust_env=False) as client:
        try:
            payload = _fetch_payload(client)
        except Exception as e:
            logger.error(f"[SFC预测] 拉取当期失败 {type(e).__name__}: {e}")
            return stats

        issue = str(payload.get("issue") or "").strip()
        stats["issue"] = issue or None
        matches = payload.get("matches") or []
        stats["total"] = len(matches)
        if not issue or not matches:
            logger.warning("[SFC预测] 当前期为空（停售/开奖/未开售），跳过")
            return stats

        sale_end = _parse_dt(payload.get("sale_end"))
        if sale_end and sale_end < now:
            # 已经停售的期不补预测。历史期更不补：那会把赛果带进模型。
            logger.warning(
                f"[SFC预测] {issue} 已于 {sale_end} 停售，跳过（不做事后回填）"
            )
            return stats

        targets: list[tuple[int, str]] = []
        for m in matches:
            no = m.get("no")
            label = f"第{no}场 {m.get('home') or '?'}-{m.get('away') or '?'}"
            fid = m.get("fixture_id")
            if not fid:
                stats["unmapped"] += 1
                logger.warning(
                    f"[SFC预测] {issue} {label}: 没有 fixture_id"
                    f"（{m.get('fixture_match') or '原因未知'}）→ 无法预测"
                )
                continue

            stats["mapped"] += 1
            kickoff = _parse_dt(m.get("kickoff"))
            if kickoff is None:
                stats["skipped"] += 1
                logger.warning(f"[SFC预测] {issue} {label}: 没有开球时间，跳过")
                continue
            if kickoff <= now + timedelta(minutes=SAFETY_MINUTES):
                stats["skipped"] += 1
                logger.info(
                    f"[SFC预测] {issue} {label}: 开球 {kickoff} 已进入"
                    f" {SAFETY_MINUTES} 分钟安全垫，跳过"
                )
                continue
            if m.get("pred") and m.get("llm"):
                stats["already"] += 1
                continue

            targets.append((int(fid), label))

        if not targets:
            logger.info(
                f"[SFC预测] {issue} 无需预测："
                f"已预测 {stats['already']} / 跳过 {stats['skipped']} / "
                f"无 fixture_id {stats['unmapped']}"
            )
            return stats

        logger.info(
            f"[SFC预测] {issue} 开始预测 {len(targets)} 场"
            f"（已预测 {stats['already']}，无 id {stats['unmapped']}）"
        )
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(_predict_one, client, fid, label)
                       for fid, label in targets]
            for f in as_completed(futures):
                status, label, detail = f.result()
                if status == "ok":
                    stats["predicted"] += 1
                    logger.info(f"[SFC预测] OK   {label}  {detail}".rstrip())
                else:
                    stats["failed"] += 1
                    logger.warning(f"[SFC预测] FAIL {label}  {detail}")

        # 预测成功后立刻强制归档一次：不依赖 13:15 / 21:15 的存档任务。
        # 存档失败不影响预测结果（predictions 已经落库），定时存档会再 COALESCE。
        # 这里可以走 HTTP 归档：本 job 已经确认 sale_end > now，且只在本期有成功
        # 预测时才调；与 sfc_archive_job 那条「别走 POST /api/sfc/archive」的约束
        # 不冲突——那条针对的是「本期无在售」也返回 502 的歧义，不是禁止所有调用。
        if stats["predicted"]:
            try:
                r = client.post("/api/sfc/archive", timeout=FETCH_TIMEOUT)
                logger.info(
                    f"[SFC预测] {issue} 强制存档 HTTP {r.status_code}: {r.text[:300]}"
                )
            except Exception as e:
                logger.error(f"[SFC预测] {issue} 强制存档失败 {type(e).__name__}: {e}")

    logger.info(
        f"[SFC预测] {issue} 完成：成功 {stats['predicted']} / 失败 {stats['failed']} / "
        f"已预测 {stats['already']} / 跳过 {stats['skipped']} / 无 id {stats['unmapped']}"
    )
    return stats
