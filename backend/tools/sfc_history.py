# -*- coding: utf-8 -*-
"""胜负彩【开奖历史】全量导入 —— CLI 薄壳。

用法(cwd = backend):
  .venv/bin/python tools/sfc_history.py fetch              # 全量(约 2 分钟)
  .venv/bin/python tools/sfc_history.py fetch --pages=5    # 只抓最新 5 页(调试用)
  .venv/bin/python tools/sfc_history.py fetch --dry-run    # 只抓不写库
  .venv/bin/python tools/sfc_history.py stats              # 看库里现在有什么

🔴 逻辑本身（取数/解析/落库 SQL/两条红线）已搬到
   `app/services/sfc_history_service.py` —— 因为定时任务够不着 tools/
   （tools 不是包、app 里也不 import tools）。改行为去改那个文件，这边只解析参数。
   赛果同步任务 `sfc_result_sync` 每天 10:15 跑的就是同一个 `sync_sfc_history()`。
"""
import argparse
import sys

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.services.sfc_archive_service import ensure_tables  # noqa: E402
from app.services.sfc_history_service import sync_sfc_history  # noqa: E402


def cmd_fetch(args):
    r = sync_sfc_history(pages=args.pages, page_size=args.page_size,
                         sleep=args.sleep, dry_run=args.dry_run)
    print(f"\n{'[dry-run] ' if args.dry_run else ''}完成: "
          f"{r['issues']} 期, {r['matches']} 场"
          + (f", 跳过 {r['skipped']} 条无期号" if r["skipped"] else "")
          + (f", 第 {r['failed_page']} 页失败" if r["failed_page"] else ""))
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
        # 任九那一档：分界是 19085（详见 service 的 _prize_json）。两段应当连续、不断档。
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
    p = sub.add_parser("fetch", help="拉开奖历史并落库")
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