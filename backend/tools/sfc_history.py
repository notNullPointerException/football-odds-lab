# -*- coding: utf-8 -*-
"""胜负彩【开奖历史】全量导入。

用法(cwd = backend):
  .venv/bin/python tools/sfc_history.py fetch              # 全量(3332 期, 约 2 分钟)
  .venv/bin/python tools/sfc_history.py fetch --pages=5    # 只抓最新 5 页(调试用)
  .venv/bin/python tools/sfc_history.py fetch --dry-run    # 只抓不写库
  .venv/bin/python tools/sfc_history.py stats              # 看库里现在有什么

【为什么需要这个脚本】
sfc_archive_service 那套存档只能存【当前期】—— 上游 getFootBallMatchV1.qry 只返回正在卖的
那一期，翻期之后对阵/彩果就再也取不回来了。而开奖历史是另一条上游通道：
    getHistoryPageListV1.qry?gameNo=90   (gameNo 90 = 胜负游戏; 94 是半全场, 别混)
它把每一期的 14 场对阵、彩果、比分、半场比分、infohubMatchId 都留在库里，可以整批回溯。

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
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.services.sfc_archive_service import _dt, ensure_tables  # noqa: E402

# 与 app/api/jc.py:_http_json 同款打点：必须带 UA + Referer，裸请求会被挡
_URL = ("https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
        "?gameNo=90&provinceId=0&isVerify=1&pageSize={size}&pageNo={page}")

# 🔴 "*" 必须照存，不能洗成 NULL —— 它是体彩对【取消/延期场次】的官方裁定
#    （14 场里有一场没踢，体彩按"全对"处理），是「上游说了话」，不是「上游没给」。
#    洗成 NULL 就和「这场根本没数据」混成一类了：库里 276 场 NULL 里其实有 262 场
#    是这种取消场次。要统计彩果分布时用 `cz_result IN ('3','1','0')` 筛，别用 IS NOT NULL。
_KNOWN_RESULT = {"3", "1", "0", "*"}


def _http_json(url: str, timeout: int = 25, retries: int = 3) -> dict:
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
    是因为 import app.api.sfc 会把 FastAPI 的 router 整套拉进来，CLI 不值得。"""
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


def cmd_fetch(args):
    ensure_tables()
    db = SessionLocal()
    n_issue = n_match = n_skip = 0
    seen = set()
    try:
        page, pages = 1, None
        while True:
            url = _URL.format(size=args.page_size, page=page)
            try:
                j = _http_json(url)
            except RuntimeError as e:
                print(f"  ! 第 {page} 页放弃: {e}")
                break
            v = j.get("value") or {}
            if pages is None:
                pages = int(v.get("pages") or 1)
                print(f"共 {v.get('total')} 期 / {pages} 页 (pageSize={args.page_size})")
            lst = v.get("list") or []
            if not lst:
                break
            for rec in lst:
                ir = _issue_row(rec)
                if not ir["issue"]:
                    n_skip += 1
                    continue
                if ir["issue"] in seen:        # 翻页抖动时的去重，保护计数
                    continue
                seen.add(ir["issue"])
                mr = _match_rows(rec)
                if args.dry_run:
                    n_issue += 1
                    n_match += len(mr)
                    continue
                db.execute(text(_ISSUE_SQL), ir)
                if mr:
                    db.execute(text(_MATCH_SQL), mr)
                n_issue += 1
                n_match += len(mr)
            if not args.dry_run:
                db.commit()
            print(f"  第 {page}/{pages} 页 OK  累计 {n_issue} 期 / {n_match} 场")
            page += 1
            if page > pages or (args.pages and page > args.pages):
                break
            time.sleep(args.sleep)
    finally:
        db.close()

    print(f"\n{'[dry-run] ' if args.dry_run else ''}完成: "
          f"{n_issue} 期, {n_match} 场" + (f", 跳过 {n_skip} 条无期号" if n_skip else ""))
    if not args.dry_run:
        cmd_stats(args)


def cmd_stats(args):
    ensure_tables()
    db = SessionLocal()
    try:
        t = db.execute(text("""SELECT COUNT(*) n, MIN(issue) mn, MAX(issue) mx,
            SUM(lottery_prize_json IS NOT NULL) prize,
            SUM(lottery_prize_json LIKE '%任选9场%') rj,
            SUM(total_sale_amount IS NOT NULL) sale,
            SUM(rj_sale_amount IS NOT NULL) rjsale,
            SUM(archived_at IS NOT NULL) live_archived
            FROM sfc_issues""")).mappings().all()[0]
        print(f"sfc_issues       {t['n']} 期   期号 {t['mn']} ~ {t['mx']}")
        print(f"  有奖级奖金 {t['prize']}   有销量 {t['sale']}   有任九销量 {t['rjsale']}   "
              f"【赛前存档过】{t['live_archived']} (历史导入的为 0)")
        # 任九那一档：分界是 19085（详见 _prize_json）。两段应当连续、不断档。
        r = db.execute(text("""SELECT
            SUM(lottery_prize_json LIKE '%任选9场%' AND issue <= '19085') lo,
            SUM(lottery_prize_json LIKE '%任选9场%' AND issue >  '19085') hi
            FROM sfc_issues""")).mappings().all()[0]
        print(f"  任九档: <=19085 {r['lo']} 期   >19085 {r['hi']} 期"
              f"   <- 两段都要有；>19085 全 0 就是又只读了一把键")

        m = db.execute(text("""SELECT COUNT(*) n,
            SUM(cz_result IS NOT NULL) cz,
            SUM(cz_half_score IS NOT NULL) half,
            SUM(infohub_match_id IS NOT NULL) ihub,
            SUM(prob_home IS NOT NULL) prob,
            SUM(fixture_id IS NOT NULL) fid
            FROM sfc_issue_matches""")).mappings().all()[0]
        print(f"sfc_issue_matches {m['n']} 行")
        print(f"  有彩果 {m['cz']}   有半场比分 {m['half']}   "
              f"有 infohubMatchId {m['ihub']}")
        print(f"  有 prob 头快照 {m['prob']}   有 fixture_id {m['fid']}   <- 只有赛前存档过的那期才有")

        bad = db.execute(text("""SELECT issue, COUNT(*) c FROM sfc_issue_matches
            GROUP BY issue HAVING c <> 14 ORDER BY c DESC LIMIT 10""")).fetchall()
        print(f"  场次数不等于 14 的期: {len(bad)}" + (f"  {bad[:5]}" if bad else ""))
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description="胜负彩开奖历史导入")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch", help="拉全量开奖历史并落库")
    p.add_argument("--pages", type=int, default=0, help="只抓前 N 页(0=全部)")
    p.add_argument("--page-size", type=int, default=30, help="每页期数(上游默认 30)")
    p.add_argument("--sleep", type=float, default=0.25, help="翻页间隔秒")
    p.add_argument("--dry-run", action="store_true", help="只抓不写库")
    p.set_defaults(fn=cmd_fetch)
    sub.add_parser("stats", help="看库里现在有什么").set_defaults(fn=cmd_stats)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
