# -*- coding: utf-8 -*-
"""胜负彩【开奖历史】同步 —— 定时任务与 CLI 共用同一份逻辑。

本模块是从 tools/sfc_history.py 抽出来的：那份逻辑原来只在 CLI 里，
而 tools/ 不是包（没有 __init__.py）、app 里也不 import tools（依赖方向是 tools→app），
所以定时任务够不着它。搬进来之后 CLI 变成薄壳，两边跑的是同一份代码。

两条上游通道，别混：
  · getFootBallMatchV1.qry   【当前期】—— 只能存正在卖的那一期（见 sfc_archive_service）
  · getHistoryPageListV1.qry 【开奖历史】—— gameNo=90 是胜负游戏, **94 是半全场, 别混**

【上游给什么 / 不给什么】
  给：期号、14 场彩果、每场 czScore(0:3) / czHalfScore(0:2) / result(3|1|0) /
      主客队名 / matchName / startTime(只有日期) / infohubMatchId /
      销量(胜负彩 totalSaleAmount + 任九 totalSaleAmountRj 两个池子) / 奖级奖金
  🔴 奖级奖金的键【19086 起换过】：之前任九在 prizeLevelList，之后在 prizeLevelListRj。
     只读一把键，库里的任九就会恰好停在 19085 —— 看起来与"停售"一模一样（踩过，见 _prize_json）。
  🔴 不给：**历史期的 h/d/a 赔率全是空字符串**。体彩不保留历史赔率 ——
     所以历史期只有彩果，没有盘口，任何「市场热门命中率」类复盘在历史期都做不了。

🔴 两条不能碰的红线：
  1. 只写历史列。头块(prob_/lam_/llm_/model_group/predict_at)、赔率列(had_/mkt_)、
     fixture_id、kickoff_at、snap_at 【一个都不写】—— 那些是"赛前快照"，是
     sfc_archive_service 在开球前抓的，一旦被历史导入覆盖就永久污染，且看不出来。
  2. archived_at 只在 INSERT 时留 NULL，不进 ON DUPLICATE 列表 ——
     它记的是"我们赛前存档的时刻"，历史导入没有这个时刻。新行 created_at 自动记导入时间。
"""
import json
import re
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

from loguru import logger
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.sfc_archive_service import _dt, ensure_tables

# 与 app/api/jc.py:_http_json 同款打点：必须带 UA + Referer，裸请求会被挡
_URL = ("https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
        "?gameNo=90&provinceId=0&isVerify=1&pageSize={size}&pageNo={page}")

# 🔴 "*" 必须照存，不能洗成 NULL —— 它是体彩对【取消/延期场次】的官方裁定
#    （14 场里有一场没踢，体彩按"全对"处理），是「上游说了话」，不是「上游没给」。
#    洗成 NULL 就和「这场根本没数据」混成一类了：库里 276 场 NULL 里其实有 262 场
#    是这种取消场次。要统计彩果分布时用 `cz_result IN ('3','1','0')` 筛，别用 IS NOT NULL。
_KNOWN_RESULT = {"3", "1", "0", "*"}

# 增量同步的默认规模：1 页 = 30 期 ≈ 近两个月，够覆盖"今天该开的那期"
_DEFAULT_PAGES = 1
_DEFAULT_PAGE_SIZE = 30


def _http_json(url: str, timeout: int = 25, retries: int = 3) -> dict:
    """带重试的取数。比 app/api/jc.py 那个同名的强 —— 无人值守的任务不能一撞就死。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.sporttery.cn/",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"拉取失败 {url}: {last}")


def _name(s) -> str:
    """体彩对两字队名做【视觉对齐填充】（"瑞  士"、"蒙  扎"），中间空格是排版不是名字。

    与 app/api/sfc.py:_name 同一套规则 —— 这里抄一份而不是 import，
    是因为 import app.api.sfc 会把 FastAPI 的 router 整套拉进来，非 web 上下文不值得。
    """
    return re.sub(r"\s+", "", s or "")


def _dec(v):
    """'18,163,390' -> Decimal。带千分位逗号，直接 float() 会炸。"""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _score(s):
    """czScore -> str 原样返回，但【裸冒号存成 NULL】。

    "2:1" 留、"延" 留（那是状态词，有信息量）；但 ":" / "  : " 是上游
    「分隔符在、两边都空」的产物 —— 它没有任何比分信息，留成 ':' 会让
    `cz_score IS NOT NULL` 把它数成「有比分」，又一个看起来正常的假阳性。
    """
    v = str(s or "").strip()
    if not v or set(v) <= {":", " "}:
        return None
    return v


# 🔴 `_score` 返回 None 只能管【首次插入】。_MATCH_SQL 的 ON DUPLICATE 是
#    `cz_score=COALESCE(VALUES(cz_score), cz_score)` —— COALESCE 的语义就是
#    「新值为 NULL 时保留旧值」，所以**改判据也清不掉已经写进库的脏值**，得手动 UPDATE。
#    （存档路径 _MATCH_UPSERT 同款写法，是两边共有的取舍：不许一次"没数据"的运行
#    把已有的彩果/比分抹掉。历史导入对同一期上游是全量快照，这条保护其实用不上，
#    但保持两边一致比省这 4 行更重要。）


def _infohub(v):
    """infohubMatchId -> int，但【0 存成 NULL】。

    🔴 0 不是 id，是体彩「这场没开竞彩」的哨兵值（对应"永远不会有赔率"那档，
    见 [[football-sporttery-api]]）。存成 0 会让 `IS NOT NULL` 这种写法把"没开竞彩"
    数成"开了" —— 一个看起来正常的假阳性。转成 NULL 后这一列的语义就是
    "体彩给的真实 id，没有就是 NULL"，可以放心当连接键用。
    """
    n = _int(v)
    return n or None


# ── 落库 SQL ────────────────────────────────────────────────────────────
# 🔴 ON DUPLICATE 的列清单就是"红线 1"的体现：ON DUPLICATE 列表里没出现的列，
#    在冲突时一律不动。所以头块/赔率/fixture_id/kickoff_at/snap_at 全都不在里面。
_ISSUE_SQL = """INSERT INTO sfc_issues
    (issue, lottery_name, sale_begin, sale_end, draw_time, total_count,
     total_sale_amount, rj_sale_amount, lottery_prize_json, lottery_draw_result)
    VALUES (:issue, :lottery_name, :sale_begin, :sale_end, :draw_time, :total_count,
            :total_sale_amount, :rj_sale_amount, :lottery_prize_json, :lottery_draw_result)
    ON DUPLICATE KEY UPDATE
        lottery_name=COALESCE(VALUES(lottery_name), lottery_name),
        sale_begin=COALESCE(VALUES(sale_begin), sale_begin),
        sale_end=COALESCE(VALUES(sale_end), sale_end),
        draw_time=COALESCE(VALUES(draw_time), draw_time),
        total_count=VALUES(total_count),
        total_sale_amount=COALESCE(VALUES(total_sale_amount), total_sale_amount),
        rj_sale_amount=COALESCE(VALUES(rj_sale_amount), rj_sale_amount),
        lottery_prize_json=COALESCE(VALUES(lottery_prize_json), lottery_prize_json),
        lottery_draw_result=COALESCE(VALUES(lottery_draw_result), lottery_draw_result)"""

# 🔴 infohub_match_id 那一列用【覆盖】(=VALUES) 而不是 COALESCE，其余历史列用 COALESCE。
#    理由：这一列只有本脚本写，覆盖才能让重跑修正旧值 —— 哨兵 0 改成 NULL 就是这么
#    修回来的。用 COALESCE 的话第一次写进去的 0 会永远留在那儿，正好是要修的东西。
_MATCH_SQL = """INSERT INTO sfc_issue_matches
    (issue, no, league, home, away, match_date,
     cz_result, cz_score, cz_half_score, infohub_match_id)
    VALUES (:issue, :no, :league, :home, :away, :match_date,
            :cz_result, :cz_score, :cz_half_score, :infohub_match_id)
    ON DUPLICATE KEY UPDATE
        league=COALESCE(VALUES(league), league),
        home=VALUES(home), away=VALUES(away),
        match_date=COALESCE(VALUES(match_date), match_date),
        cz_result=COALESCE(VALUES(cz_result), cz_result),
        cz_score=COALESCE(VALUES(cz_score), cz_score),
        cz_half_score=COALESCE(VALUES(cz_half_score), cz_half_score),
        infohub_match_id=VALUES(infohub_match_id)"""


def _prize_json(rec: dict):
    """两把键里的奖级【合并成一份】JSON -> str；一条都没有就 None。

    🔴 上游在期号 19085 换过键，不是停售：
        04028~19085  【任选9场】是 prizeLevelList 的第三档
        19086 起     任九挪进 prizeLevelListRj
      只读前者的后果很阴险 —— 库里的任九**正好停在 19085**，看起来与
      「2019 年年中停售」一模一样（我据这个假象误判过一次，见
      [[football-sfc-prize-mechanics]]）。所以两把键都要读。
      实测两个 list 不会同时出现任九，去重只是兜底。
    ⚠️ 顺序是「先 prizeLevelList 后 Rj」—— 让合并后的数组与 19085 之前的老行
      完全同形（一等奖/二等奖/任选9场），前端「有几档画几档」不用改。
    """
    merged = {}
    for lst in (rec.get("prizeLevelList"), rec.get("prizeLevelListRj")):
        for x in lst or []:
            lv = str(x.get("prizeLevel") or "").strip()
            if not lv:
                continue
            # 同名档位只留一条：后出现的那条若带了金额就覆盖前者，没带就保留前者。
            if lv in merged and not x.get("stakeAmount"):
                continue
            merged[lv] = x
    return json.dumps(list(merged.values()), ensure_ascii=False) if merged else None


def _issue_row(rec: dict) -> dict:
    """上游一条开奖记录 -> sfc_issues 一行的参数字典。"""
    ms = rec.get("matchList") or []
    return {
        "issue": str(rec.get("lotteryDrawNum") or "").strip(),
        "lottery_name": rec.get("lotteryGameName") or None,
        "sale_begin": _dt(rec.get("lotterySaleBeginTime")),
        "sale_end": _dt(rec.get("lotterySaleEndtime")),
        # 开奖日上游只给到日（"2026-09-28"），_dt 的三个格式里有 %Y-%m-%d，能吃到
        "draw_time": _dt(rec.get("lotteryDrawTime")),
        "total_count": len(ms) or None,
        "total_sale_amount": _dec(rec.get("totalSaleAmount")),
        # 任九是【另一个奖池】—— 26133 期它是胜负彩的 73%（1318万 vs 1816万），
        # 两个销量相加没有意义，所以分成两列存。
        "rj_sale_amount": _dec(rec.get("totalSaleAmountRj")),
        "lottery_prize_json": _prize_json(rec),
        # 开奖公告原文，就是开奖页上那串 "3 3 0 0 3 3 1 3 3 0 3 0 3 *"。
        # 与 matchList[].result 冗余(已抽 26133 对过账)，但它是【整期一个字段】——
        # 2004~2009 那 6216 行没有队名没有比分，这是那批数据唯一的完整彩果表达。
        "lottery_draw_result": (str(rec.get("lotteryDrawResult") or "").strip() or None),
    }


def _match_rows(rec: dict) -> list:
    """上游一条开奖记录 -> sfc_issue_matches 的 14 行。"""
    issue = str(rec.get("lotteryDrawNum") or "").strip()
    out = []
    for m in rec.get("matchList") or []:
        no = _int(m.get("matchNum"))
        if no is None:
            continue          # 没有场次号就没法落进 (issue, no) 唯一键，跳过
        r = str(m.get("result") or "").strip()
        out.append({
            "issue": issue,
            "no": no,
            # 历史期上游不给联赛字段名，matchName 就是"国际赛/英超"这类
            "league": _name(m.get("matchName")) or None,
            "home": _name(m.get("masterTeamName")) or "",
            "away": _name(m.get("guestTeamName")) or "",
            "match_date": (str(m.get("startTime") or "").strip() or None),
            "cz_result": r if r in _KNOWN_RESULT else None,
            "cz_score": _score(m.get("czScore")),
            "cz_half_score": _score(m.get("czHalfScore")),
            "infohub_match_id": _infohub(m.get("infohubMatchId")),
        })
    return out


def sync_sfc_history(pages: int = _DEFAULT_PAGES, page_size: int = _DEFAULT_PAGE_SIZE,
                     sleep: float = 0.25, dry_run: bool = False) -> dict:
    """拉开奖历史并落库。**【幂等】**：同一期重复导入只是把同样的值再 upsert 一遍。

    这就是「赛后彩果」那条链的入口 —— 彩果/奖金/销量全从这儿来，
    而赛前快照那几列（头块/赔率/fixture_id/kickoff_at/snap_at）**永远不碰**（红线 1）。

    pages=0 表示不限页数（全量，约 2 分钟）；默认 1 页 = 最新 30 期。
    单页失败不抛错、只停在那里 —— 已经落库的页照常保留，下次任务自动补。
    """
    stats = {"issues": 0, "matches": 0, "skipped": 0, "pages": 0, "failed_page": None}
    ensure_tables()
    db = SessionLocal()
    seen = set()
    try:
        page, total_pages = 1, None
        while True:
            try:
                j = _http_json(_URL.format(size=page_size, page=page))
            except RuntimeError as e:
                logger.warning(f"胜负彩历史: 第 {page} 页放弃: {e}")
                stats["failed_page"] = page
                break
            v = j.get("value") or {}
            if total_pages is None:
                total_pages = int(v.get("pages") or 1)
                logger.info(f"胜负彩历史: 共 {v.get('total')} 期 / {total_pages} 页 "
                            f"(pageSize={page_size})")
            lst = v.get("list") or []
            if not lst:
                break
            for rec in lst:
                ir = _issue_row(rec)
                if not ir["issue"]:
                    stats["skipped"] += 1
                    continue
                if ir["issue"] in seen:        # 翻页抖动时的去重，保护计数
                    continue
                seen.add(ir["issue"])
                mr = _match_rows(rec)
                stats["issues"] += 1
                stats["matches"] += len(mr)
                if dry_run:
                    continue
                db.execute(text(_ISSUE_SQL), ir)
                if mr:
                    db.execute(text(_MATCH_SQL), mr)
            if not dry_run:
                db.commit()
            stats["pages"] = page
            if page >= total_pages or (pages and page >= pages):
                break
            page += 1
            time.sleep(sleep)
    finally:
        db.close()
    return stats


def sfc_result_sync_job(db=None):
    """scheduler 入口：fn(db=None) 签名，与 odds_watch_job / weekly_review_job 一致。

    为什么挑早上：胜负彩开球集中在 00:30~03:00，约 05:00 全部结束，所以彩果早间即齐。
    抓不到也不要紧 —— 这是全量 upsert，第二天那枪会把同样的期再导一遍、把缺的补上。
    """
    r = sync_sfc_history()
    logger.info(f"胜负彩赛果同步: {r['pages']} 页 / {r['issues']} 期 / {r['matches']} 场"
                + (f" (第 {r['failed_page']} 页失败)" if r["failed_page"] else ""))
    return r