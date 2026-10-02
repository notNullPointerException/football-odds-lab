# -*- coding: utf-8 -*-
"""标准线逐场读出器：给 fixture_id，用【赛果】算 mu，读标准线出三路概率。

【为什么】模型头的 λ 在杯赛会爆表（实测：全库 λ 合计均值 3.40 vs 实际 2.98，
近30天 TOP10 有 9 场是杯赛，最大 9.66 那场实际 0-0）。标准线只用已完赛比分推 mu，
是完全独立于模型的第三方读数 —— 两边不一致时，标准线是「赛果说了什么」。

【判据】标准线 --check 月度前推 72267 场：经验表 Brier 0.6235 < 泊松 0.6492 < 频率 0.6458。
所以每个 mu 桶的分布是标定过的，桶内 n 一并打出（n 小 = 这个读数不可信）。

用法:
  .venv/bin/python tools/std_line_pick.py 1588908 1588849 1588823
  .venv/bin/python tools/std_line_pick.py 1588908 --line=-1        # 让球线(负=主队让球)
  .venv/bin/python tools/std_line_pick.py --date=2026-09-23 --category=jingzu

⚠️ 只读，不写任何表，不碰闸门/模型。
"""
import sys, os, argparse, unicodedata
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text
from app.db.session import SessionLocal
from app.services.standard_line_service import (
    expected_margin, fair, latest_build, bucket_ix, bucket_bounds, bucket_dist, DEF_BW,
)


def w(s):
    """显示宽度：CJK 占 2 列。表头表体必须走同一个补法（见记忆 tier-a-tool-table-padding）。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n, right=False):
    s = str(s)
    sp = " " * max(0, n - w(s))
    return sp + s if right else s + sp


def fmt_p(d):
    """{label: p} -> 一行字符串"""
    return "  ".join(f"{k} {v*100:5.1f}%" for k, v in d.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fids", nargs="*", type=int, help="fixture id")
    ap.add_argument("--line", type=float, default=None, help="让球线(负=主队让球)")
    ap.add_argument("--date", default=None, help="按日期取 jingzu 场次")
    ap.add_argument("--category", default=None)
    a = ap.parse_args()

    db = SessionLocal()
    try:
        lb = latest_build(db)
        if not lb:
            print("!! 库里没有标准线，先跑 tools/standard_line.py --build")
            return 1
        bid = lb["build_id"]
        print(f"标准线 {bid}  (n_used={lb['n_used']}  {lb['src_from']} ~ {lb['src_to']})")
        print()

        fids = list(a.fids)
        if a.date:
            d0 = datetime.strptime(a.date, "%Y-%m-%d")
            cond = "date >= :s AND date < :e"
            p = {"s": d0, "e": d0 + timedelta(days=1)}
            if a.category:
                cond += " AND category = :c"
                p["c"] = a.category
            fids += [r[0] for r in db.execute(
                text(f"SELECT id FROM fixtures WHERE {cond} ORDER BY date"), p).fetchall()]

        if not fids:
            print("没给 fixture_id。用法见 docstring。")
            return 1

        hdr = (pad("fixture", 34) + pad("mu", 7, True) + pad("桶", 16, True)
               + pad("n", 8, True) + "  " + pad("标准线 1x2 (主/平/客)", 30))
        print(hdr)
        print("-" * w(hdr))
        for fid in fids:
            r = db.execute(text(
                "SELECT home_id, away_id, home_name, away_name, date, league_name "
                "FROM fixtures WHERE id=:i"), {"i": fid}).mappings().first()
            if not r:
                print(f"{fid} 不在库里"); continue
            mu = expected_margin(db, r["home_id"], r["away_id"], r["date"])
            name = f"{fid} {str(r['home_name'])[:12]} vs {str(r['away_name'])[:12]}"
            if mu is None:
                print(pad(name, 34) + "  历史不足，算不出 mu")
                continue
            ix = bucket_ix(mu, DEF_BW)
            lo, hi = bucket_bounds(ix, DEF_BW)
            dist = fair(bid, mu, "1x2", db=db)
            n = db.execute(text("SELECT n FROM std_line_margin WHERE build_id=:b AND bucket_ix=:i LIMIT 1"),
                           {"b": bid, "i": ix}).scalar()
            print(pad(name, 34) + pad(f"{mu:+.3f}", 7, True)
                  + pad(f"[{lo:+.2f},{hi:+.2f})", 16, True) + pad(n, 8, True)
                  + "  " + fmt_p(dist))
            if a.line is not None:
                hh = fair(bid, mu, "hhad", line=a.line, db=db)
                print(" " * 34 + f"  让{a.line:+.0f} -> " + fmt_p(hh))
            # 该桶内最可能的几个净胜球（注意 dist 是 fair() 的三路结果，这里要的是原始 margin 分布）
            top = sorted(bucket_dist(bid, mu, db=db, bw=DEF_BW).items(),
                         key=lambda kv: -kv[1])[:5]
            print(" " * 34 + "  桶内 TOP5: " + "  ".join(f"k={k:+d} {v*100:.1f}%" for k, v in top))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
