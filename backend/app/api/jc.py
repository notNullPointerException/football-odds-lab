# -*- coding: utf-8 -*-
"""体彩（竞彩足球）实时开盘数据 —— 只读代理。

为什么要有这个端点：webapi.sporttery.cn 不带 CORS 头，浏览器直连必被拦，
所以只能后端代取再吐给前端。口径与 tools/jc_odds.py 完全一致（同一 URL、
同一去水公式），避免前后端出现两套数字。

「和体彩保持一致」= 体彩挂几场就返回几场，不做任何筛选/推断。

fixture_id 由 app/services/jc_match_service.py 现场解析 —— 见那里的模块说明。
2026-09-24 之前这里读的是 tools/jc_map.json（每天手工重建的单日文件），
只有 map_date == 销售日那天才敢给 id，其余一律 None，页面于是经常「有比赛
却点不进预测」。现在改成逐场现场解析，jc_map.json 不再是数据源。
"""
import asyncio
import json
import time
import urllib.request

from fastapi import APIRouter, HTTPException
from loguru import logger

from app.db.session import SessionLocal
from app.services import jc_match_service

router = APIRouter()

_JC_URL = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
           "?poolCode=hhad,had&channel=c")

# 极短缓存：防手抖连点把体彩接口打爆，15s 内仍算「实时」
_CACHE_TTL = 15.0
_cache: dict = {"at": 0.0, "payload": None}


def _http_json(url: str, timeout: int = 20) -> dict:
    """打体彩 webapi。必须带 UA + Referer，裸请求会被挡。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.sporttery.cn/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _fetch_raw() -> dict:
    return _http_json(_JC_URL)


def _devig(h, d, a):
    """按赔率倒数归一化去水。返回 (概率三元组, 返还率)。与 jc_odds.py 同公式。"""
    try:
        inv = [1.0 / float(x) for x in (h, d, a)]
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None
    s = sum(inv)
    if not s:
        return None, None
    return [x / s for x in inv], 1.0 / s


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 3)


def _parse_pool(pool: dict, with_line: bool = False) -> dict | None:
    """把一个赔率池（had / hhad）转成前端直接可用的结构。空 {} = 体彩没开这个玩法。"""
    if not pool:
        return None
    h, d, a = pool.get("h"), pool.get("d"), pool.get("a")
    p, payback = _devig(h, d, a)

    out = {
        "h": _num(h), "d": _num(d), "a": _num(a),
        "p_h": round(p[0], 4) if p else None,
        "p_d": round(p[1], 4) if p else None,
        "p_a": round(p[2], 4) if p else None,
        "payback": round(payback, 4) if payback else None,
        "updated": f"{pool.get('updateDate', '')} {pool.get('updateTime', '')}".strip() or None,
    }
    if with_line:
        gl = pool.get("goalLine", "")
        try:
            gi = int(float(gl)) if gl not in ("", None) else 0
        except (TypeError, ValueError):
            gi = 0
        out["line"] = gi
        # 负 = 主队让球，与 jc_odds.py 的 side 口径一致
        out["side"] = "主队让" if gi < 0 else ("主队受让" if gi > 0 else "平手")
    return out


def _pool_close(m: dict) -> str | None:
    """该场所有玩法池里最早的停售时刻，取不到返回 None。

    体彩 schema 里【确实有】poolCloseDate / poolCloseTime（每个玩法池一份），
    但实测 96 个池子全返回空串；而且场次一旦停售就直接从 matchInfoList 消失
    （实测 51→50，18:30 开赛那场开赛后立刻不见了），所以在售期间永远看不到它被填。
    这里照样接住：哪天体彩真填了，前端立刻就能用上，不用再改代码。
    """
    best = None
    for p in m.get("poolList") or []:
        d = (p.get("poolCloseDate") or "").strip()
        if not d:
            continue
        s = f"{d} {(p.get('poolCloseTime') or '').strip()}".strip()
        best = s if best is None else min(best, s)
    return best


def _resolve_fixture_ids(days: list) -> None:
    """就地给每个销售日的每场比赛填 fixture_id / fixture_match。

    不抛异常：这个端点的本职是「把体彩的盘口如实转出来」，DB 挂了盘口照样要看，
    只是那几场点不进预测页 —— 降级要降得有信息，不能整页 500。
    """
    db = None
    try:
        db = SessionLocal()
        for day in days:
            jc_match_service.resolve_batch(db, day["matches"], day["business_date"])
    except Exception as e:
        logger.error(f"解析 fixture_id 失败(体彩盘口不受影响): {type(e).__name__}: {e}")
        for day in days:
            for m in day["matches"]:
                if "fixture_match" not in m:
                    m["fixture_id"] = None
                    m["fixture_match"] = f"解析失败: {type(e).__name__}"
    finally:
        if db is not None:
            db.close()


def _build_payload() -> dict:
    data = _fetch_raw()
    val = data.get("value") or {}
    # ⚠️ 空 matchInfoList 是【合法状态】（当天全部停售，深夜必现），不是错误。
    # 早先这里抛 502，造成两个后果：① 用户看到红色报错而不是「暂无场次」
    # ② 走 except HTTPException 分支绕过 stale 兜底，把好缓存白白丢掉。
    # 「和体彩保持一致」意味着体彩说没有，我们就该说没有 —— 不能拿旧盘冒充。
    days_raw = val.get("matchInfoList") or []

    days = []
    total = 0
    for day in days_raw:
        business_date = day.get("businessDate")

        matches = []
        for m in day.get("subMatchList") or []:
            num = m.get("matchNumStr", "")
            matches.append({
                "match_num": num,
                "league": m.get("leagueAbbName", ""),
                "home": m.get("homeTeamAbbName", ""),
                "away": m.get("awayTeamAbbName", ""),
                # 体彩的三字母缩写码（与 homeTeamAbbEnName 恒等，实测 24/24 队位）。
                # 队名判不出时 jc_match_service 拿它兜底 —— 中文比拉丁是没有信号的，
                # 码是独立于语言的第二路。同时透出去给前端排查用。
                "home_code": m.get("homeTeamCode") or "",
                "away_code": m.get("awayTeamCode") or "",
                "kickoff": f"{m.get('matchDate', '')} {m.get('matchTime', '')}".strip(),
                # 体彩给了停售时刻就用它，没给（现状）前端回落到开球时间
                "close_time": _pool_close(m),
                "match_status": m.get("matchStatus", ""),
                "sell_status": m.get("sellStatus"),
                "home_rank": m.get("homeRank") or None,
                "away_rank": m.get("awayRank") or None,
                "had": _parse_pool(m.get("had") or {}),
                "hhad": _parse_pool(m.get("hhad") or {}, with_line=True),
                # 下面由 _resolve_fixture_ids 现场填，这里不放占位值 ——
                # 填不上就是 None，语义是「这场没关联上」，不会误关联到别的场次。
            })
            total += 1

        days.append({
            "business_date": business_date,
            "weekday": day.get("weekday", ""),
            "count": len(matches),
            "matches": matches,
        })

    _resolve_fixture_ids(days)

    # 配不上的场次单独计数：页面点不进预测页时，先看这个数，再看每场的
    # fixture_match 说明 —— 别再去怀疑「jc_map 是不是过期了」，那套已经没有了。
    unmatched = sum(1 for d in days for m in d["matches"] if not m.get("fixture_id"))

    return {
        "last_update": val.get("lastUpdateTime"),
        "total_count": total,
        "unmatched_count": unmatched,
        "days": days,
    }


@router.get("/jc/matches")
async def jc_matches(refresh: bool = False):
    """体彩竞彩足球实时赛程 + 开盘赔率（去水概率）。与体彩官网保持一致。"""
    now = time.time()
    if not refresh and _cache["payload"] is not None and now - _cache["at"] < _CACHE_TTL:
        return {**_cache["payload"], "cached": True}

    try:
        payload = await asyncio.to_thread(_build_payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"体彩接口拉取失败: {type(e).__name__}: {e}")
        # 拉不到时退回上一次结果，避免页面直接空白
        if _cache["payload"] is not None:
            return {**_cache["payload"], "cached": True, "stale": True,
                    "error": f"{type(e).__name__}: {e}"}
        raise HTTPException(status_code=502, detail=f"体彩接口拉取失败：{e}")

    _cache.update(at=now, payload=payload)
    return {**payload, "cached": False}
