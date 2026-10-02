# -*- coding: utf-8 -*-
"""胜负彩 14 场按期存档 / 每期复盘 CLI

用法(cwd = backend):
  .venv/bin/python tools/sfc_archive.py init            # 建表(幂等)
  .venv/bin/python tools/sfc_archive.py archive         # 拉一次上游, 把当前期存下来
  .venv/bin/python tools/sfc_archive.py list            # 已归档的期号 + 每个头错几场
  .venv/bin/python tools/sfc_archive.py show 26134      # 某一期的完整复盘

⚠️ 上游【只返回当前期】。翻期之后这一期的对阵/赔率/彩果就再也取不回来了 ——
   存档的时机只有"这一期还在卖的时候"。网页端打开胜负彩页会自动存档(120秒防抖),
   这个 CLI 是给不开浏览器、或者想手动补一炮的场景用的。

🔴 输出只用 ASCII 画表：λ / μ / — / ← 这些是东亚【模糊宽度】(Ambiguous)，中文终端
   按 2 列算、西文终端按 1 列算，放进表格必然错位。所以表头写 lam 不写 λ。
   正文(不参与对齐的句子)里可以正常写中文。
"""
import argparse
import sys
import unicodedata

sys.path.insert(0, ".")

from app.api.sfc import _build_payload  # noqa: E402
from app.services.sfc_archive_service import (  # noqa: E402
    archive_payload, ensure_tables, get_issue_review, list_issues,
)


def w(s):
    """东亚全角字符算 2 宽，用于对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n, right=False):
    s = str(s)
    fill = " " * max(0, n - w(s))
    return (fill + s) if right else (s + fill)


# 表格里必须用 ASCII 名（理由见文件头）。顺序与服务端 head_order 一致。
_COLS = [("prob", "prob"), ("lam", "lam"), ("llm", "llm"),
         ("mkt", "MKT"), ("had", "HAD")]
_RESULT_ZH = {"3": "胜", "1": "平", "0": "负"}          # 胜负彩彩果记法
_DIR_ASCII = {"home": "H", "draw": "D", "away": "A", None: "-"}


def cmd_init(args):
    ensure_tables()
    print("ok: sfc_issues / sfc_issue_matches ready")


def cmd_archive(args):
    payload = _build_payload()
    issue = payload.get("issue")
    if not issue:
        print("上游这一刻没有可存档的期次(本期已停售/未开售)。空不等于失败。")
        return
    r = archive_payload(payload)
    print(f"ok: 期号 {r['issue']}  场次 {r['matches']}  "
          f"有关联 id {r['with_fixture']}  有预测快照 {r['with_pred']}")
    print(f"    停售 {payload.get('sale_end') or '-'}  "
          f"开奖 {payload.get('draw_time') or '-'}  "
          f"已出彩果 {payload.get('drawn_count')} 场")


def _print_heads(heads):
    line = []
    for key, name in _COLS:
        h = heads.get(key)
        if not h or not h["n"]:
            line.append(f"{name} -")
        else:
            line.append(f"{name} {h['wrong']}/{h['n']}")
    print("  头级(错/已判): " + "  ".join(line))


def _print_matches(rows):
    # 列宽先量好再打表头 —— 表头也必须走 pad()，混用 {'中文':<20} 会错位
    head = ["no", "对阵", "联赛", "赛果", "比分", "src"] + [c for _, c in _COLS]
    out_rows = []
    for m in rows:
        out_rows.append([
            str(m["no"]),
            f"{m['home']} vs {m['away']}",
            (m["league"] or "")[:8],
            _RESULT_ZH.get(m["result"], "-"),
            m["score"] or "-",
            (m["result_src"] or "-")[:10],
        ] + [
            (f"{_DIR_ASCII.get(m['cells'][k]['pick'])}"
             f"{'' if m['cells'][k]['hit'] is None else ('+' if m['cells'][k]['hit'] else '-')}")
            for k, _ in _COLS
        ])
    # 没有数据行时不能写成 max(w(head[i]), *()) —— 那会退化成 max(单个 int) 直接抛
    # TypeError。先算列宽再打表头，表头也要走 pad()。
    widths = [max([w(head[i])] + [w(r[i]) for r in out_rows]) for i in range(len(head))]
    print("  " + "  ".join(pad(head[i], widths[i]) for i in range(len(head))))
    print("  " + "  ".join("-" * widths[i] for i in range(len(head))))
    for r in out_rows:
        print("  " + "  ".join(pad(r[i], widths[i]) for i in range(len(head))))
    print("  (H/D/A=主胜/平/客胜, 后缀 +=判对 -=判错 空=没数据; src=赛果来源)")


def cmd_list(args):
    d = list_issues(page=1, size=args.limit)
    if not d["data"]:
        print("还没有归档过任何一期。先在胜负彩页打开一次，或跑 archive。")
        return
    print(f"已归档 {d['total']} 期\n")
    for it in d["data"]:
        print(f"期号 {it['issue']}  {it['lottery_name'] or ''}  "
              f"停售 {it['sale_end'] or '-'}  开奖 {it['draw_time'] or '-'}  "
              f"已出赛果 {it['scored_count']}/{it['total_count']}")
        _print_heads(it["heads"])
        print()


def cmd_show(args):
    it = get_issue_review(args.issue)
    if not it:
        print(f"没有期号 {args.issue} 的存档。先跑 archive，或者 list 看看有哪些期。")
        return
    print(f"第 {it['issue']} 期  {it['lottery_name'] or ''}")
    print(f"停售 {it['sale_end'] or '-'}   开奖 {it['draw_time'] or '-'}   "
          f"存档于 {it['archived_at'] or '-'}")
    print(f"{it['total_count']} 场，已出赛果 {it['scored_count']} 场\n")
    _print_heads(it["heads"])
    print()
    for b in it["bullets"]:
        print(f"  * {b}")
    print()
    _print_matches(it["matches"])


def main():
    ap = argparse.ArgumentParser(description="胜负彩按期存档 / 复盘")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="建表(幂等)").set_defaults(fn=cmd_init)
    sub.add_parser("archive", help="拉上游并存下当前期").set_defaults(fn=cmd_archive)
    p = sub.add_parser("list", help="列出已归档的期号 + 头级战绩")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show", help="某一期的完整复盘")
    p.add_argument("issue")
    p.set_defaults(fn=cmd_show)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
