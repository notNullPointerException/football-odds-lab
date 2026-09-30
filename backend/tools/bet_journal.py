# -*- coding: utf-8 -*-
"""竞彩注单记账 CLI

用法(cwd = backend):
  .venv/bin/python tools/bet_journal.py init
  .venv/bin/python tools/bet_journal.py log --json-file /tmp/ticket.json
  .venv/bin/python tools/bet_journal.py settle
  .venv/bin/python tools/bet_journal.py report --week last
  .venv/bin/python tools/bet_journal.py list
  .venv/bin/python tools/bet_journal.py config --bankroll 10000 --unit-pct 1

注单 JSON 结构(赛前填, 赛后不改):
{
  "bet_date": "2026-09-09", "batch": "9-9周三", "ticket_type": "2串1",
  "is_main": 1, "stake": 200, "note": "唯一主推",
  "legs": [
    {"fixture_id": 1550091, "jc_no": "周三006", "match_label": "里斯本竞技vs...",
     "market": "1x2", "selection": "主胜", "line_val": null, "odds": 1.50,
     "model_prob": 0.62, "market_prob": 0.55, "signal_tag": "LLM65%+模型∥市场"}
  ]
}
market: 1x2(主胜/平/客胜) / handicap(让胜/让平/让负, line_val=主队让球线) / ou(大球/小球)
"""
import argparse
import json
import sys

sys.path.insert(0, ".")

from app.services.bet_journal_service import (  # noqa: E402
    ensure_tables, log_ticket, settle_bets, weekly_report, save_report, set_config,
)
from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402


def cmd_list(args):
    db = SessionLocal()
    try:
        rows = db.execute(text("""SELECT * FROM bet_journal ORDER BY bet_date DESC, id DESC
            LIMIT :n"""), {"n": args.limit}).mappings().all()
        for r in rows:
            tag = "★主推" if r["is_main"] else "备选"
            print(f"[{r['id']}] {r['bet_date']} {r['batch'] or ''} {r['ticket_type']} "
                  f"@{r['total_odds']} {tag} [{r['status']}] stake={r['stake']} pnl={r['pnl']}")
            for g in db.execute(text("""SELECT * FROM bet_legs WHERE ticket_id=:t"""),
                                {"t": r["id"]}).mappings().all():
                print(f"      {g['jc_no'] or ''} {g['match_label']} {g['market']} "
                      f"{g['selection']}{'' if g['line_val'] is None else ' ' + str(g['line_val'])} "
                      f"@{g['odds']} edge={g['edge']} → {g['leg_result']} {g['actual_score'] or ''}"
                      f"  [{g['signal_tag'] or ''}]")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p_log = sub.add_parser("log")
    p_log.add_argument("--json-file", required=True)

    sub.add_parser("settle")

    p_rep = sub.add_parser("report")
    p_rep.add_argument("--week", default="this", help="this/last/YYYY-Www/周一日期")
    p_rep.add_argument("--save", action="store_true")

    p_ls = sub.add_parser("list")
    p_ls.add_argument("--limit", type=int, default=20)

    p_cfg = sub.add_parser("config")
    p_cfg.add_argument("--bankroll", type=float)
    p_cfg.add_argument("--unit-pct", type=float)

    args = ap.parse_args()

    if args.cmd == "init":
        ensure_tables()
        print("✅ bet_journal / bet_legs / bet_config 就绪")
    elif args.cmd == "log":
        with open(args.json_file, encoding="utf-8") as f:
            payload = json.load(f)
        tid = log_ticket(payload)
        print(f"✅ 注单 #{tid} 已记录（赛前依据已锁定，赛后只回填结果）")
    elif args.cmd == "settle":
        r = settle_bets()
        print(f"✅ 腿 {r['legs_updated']} 条 / 注单 {r['tickets_settled']} 张已结算")
        if r["pool_tickets_manual"]:
            print(f"⚠️  {len(r['pool_tickets_manual'])} 张彩池制注单（胜负彩复式）"
                  f"【未自动结算】: {r['pool_tickets_manual']}")
            print("    名次(一等/二等)与奖金只能来自体彩奖金表，票面 odds 算不出来。")
            print("    腿级结果已回填，注单级请人工填 status/payout/pnl 后收工。")
    elif args.cmd == "report":
        md = weekly_report(args.week)
        print(md)
        if args.save:
            print(f"\n📄 已保存 {save_report(md)}")
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "config":
        if args.bankroll is not None:
            set_config("bankroll", args.bankroll)
        if args.unit_pct is not None:
            set_config("unit_pct", args.unit_pct)
        db = SessionLocal()
        try:
            for r in db.execute(text("SELECT k, v FROM bet_config")).mappings().all():
                print(f"{r['k']} = {r['v']}")
        finally:
            db.close()


if __name__ == "__main__":
    main()
