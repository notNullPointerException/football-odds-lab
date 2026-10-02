# -*- coding: utf-8 -*-
"""竞彩 vs 锐盘(Pinnacle) 赔率快照 + EV 面板。

【这个工具解决什么】
选腿一直靠「模型比市场准」。633 场回测把这个依据推翻了（prob 劣于频率基准、
λ ≈ 零 skill、LLM 47%）—— edge 不在预测层。

于是换一个「水」的来源：不靠模型，靠「竞彩自己定价偏」。拿 Pinnacle（全球最锐）
的公平价当尺子量竞彩：ev = 竞彩赔率 x 锐盘公平价 - 1。ev > 0 = 竞彩给多了。

【凭什么锐盘能给竞彩的整数让球线定价】
Pinnacle 只开 ±1.5/±2.5/±3.5，竞彩开整数线 -1/-2。看着对不上，但这一族够还原：
    让胜 = P(m >= 1-g)      g = 竞彩 goalLine(负=主队让球)，m = 净胜球
    让负 = 1 - P(m >= -g)
    让平 = P(m >= -g) - P(m >= 1-g)
其中 P(m >= k) 从锐盘取（k=1 用 Match Winner，其余用 AH "Home -(k-0.5)"）。
⚠️ API-Football 的 bookmaker id：**3=Betfair，4=Pinnacle**（曾把 3 当成 Pinnacle）。
推导和已知近似见 app/services/odds_watch_service.py 的模块 docstring。

用法:
    .venv/bin/python tools/odds_watch.py             # 抓一轮 + 落库 + 打面板
    .venv/bin/python tools/odds_watch.py --show      # 只打面板，不落库
    .venv/bin/python tools/odds_watch.py --no-pin    # 只抓竞彩，跳过锐盘（快/省额度）

🔴 本工具只做【标注】，不改任何选腿规则。闸门 ①-⑤ 与三个模型头一行不动。
"""
import argparse
import sys
import unicodedata
from datetime import datetime

sys.path.insert(0, ".")

from app.services.odds_watch_service import (  # noqa: E402
    collect, latest_fetch, diagnose, MARKET_1X2, MARKET_HHAD,
)

LINE = "-" * 104


def w(s):
    """东亚全角字符算 2 宽，用于对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n, right=False):
    s = str(s)
    fill = " " * max(0, n - w(s))
    return (fill + s) if right else (s + fill)


def fit(s, n):
    """超宽就截断（按显示宽度），末尾加 ~。"""
    s = str(s)
    if w(s) <= n:
        return s
    out = ""
    for c in s:
        if w(out) + w(c) > n - 1:
            break
        out += c
    return out + "~"


def fmt_pct(v, width=6):
    return pad(f"{v:.1%}" if v is not None else "-", width, right=True)


def fmt_ev(v, width=8):
    if v is None:
        return pad("-", width, right=True)
    return pad(f"{v*100:+.1f}%", width, right=True)


def market_label(m):
    return "胜平负" if m == MARKET_1X2 else "让球"


def print_panel(rows, stats, saved, db_latest):
    print("=" * 104)
    head = f"== 竞彩 vs 锐盘(Pinnacle)   {datetime.now():%Y-%m-%d %H:%M}"
    print(head)
    print(f"   覆盖 {stats['days']} 个销售日 / {stats['matches']} 场   "
          f"锐盘配对 {stats['paired']} 场（失败 {stats['pin_fail']}）   "
          f"入库 {saved} 行")
    if db_latest:
        age_h = (datetime.now() - db_latest).total_seconds() / 3600
        flag = "  <<< 超过 24h 没抓到，测量在丢数据" if age_h > 24 else ""
        print(f"   最近一次快照: {db_latest:%Y-%m-%d %H:%M}（{age_h:.1f} 小时前）{flag}")
    print("=" * 104)

    paired = [r for r in rows if r.get("ev") is not None]
    if not paired:
        print()
        for msg in diagnose(stats):
            print(f"  {msg}")
        print()
        return

    paired.sort(key=lambda r: -r["ev"])

    # ── [1] EV > 0：这才是「竞彩给多了」的候选水 ──
    pos = [r for r in paired if r["ev"] > 0]
    print()
    print(f"[1] EV > 0 —— 竞彩赔率 > 锐盘公平价（候选水）  {len(pos)} 条 / {len(paired)} 条配对腿")
    if not pos:
        print("    （无。今天竞彩没有一条腿比锐盘公平价更值）")
    else:
        _table(pos, mark=True)

    # ── [2] 全批 EV 降序总榜 ──
    print()
    print(f"[2] 全批 EV 降序总榜（只列配上锐盘的腿，共 {len(paired)} 条）")
    _table(paired[:60], mark=False)

    unpinned = len([r for r in rows if r.get("ev") is None])
    print()
    print(f"    另有 {unpinned} 条腿只有竞彩价、没配锐盘（非 map_date 当日，或锐盘未开）")
    print(f"    ev = 竞彩赔率 x 锐盘公平价 - 1    线=竞彩 goalLine（负=主队让球）")
    print()


def _table(rows, mark):
    print(f"  {pad('号',8)}{pad('比赛',30)}{pad('玩法',8)}{pad('选择',8)}"
          f"{pad('线',6)}{pad('赔率',8)}{pad('竞彩%',8)}{pad('锐盘%',8)}{pad('EV',9)}")
    print("  " + LINE[:100])
    for r in rows:
        flag = "  *" if (mark and r["ev"] > 0) else ""
        line = r["line_val"]
        line_s = f"{line:+.0f}" if line is not None else "-"
        odds = r["odds"]
        odds_s = f"{odds:.2f}" if odds is not None else "-"
        label = fit(r["match_label"] or "-", 30)
        print(f"  {pad(r['jc_no'] or '-', 8)}"
              f"{pad(label, 30)}"
              f"{pad(market_label(r['market']), 8)}"
              f"{pad(r['selection'], 8)}"
              f"{pad(line_s, 6)}"
              f"{pad(odds_s, 8, right=True)}"
              f"{fmt_pct(r['jc_prob'], 8)}"
              f"{fmt_pct(r['pin_fair'], 8)}"
              f"{fmt_ev(r['ev'], 9)}{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只打面板，不落库")
    ap.add_argument("--no-pin", action="store_true", help="跳过锐盘抓取（只存竞彩价）")
    args = ap.parse_args()

    rows, stats = collect(dry_run=args.show, fetch_pin=not args.no_pin)
    print_panel(rows, stats, stats.get("saved", 0), latest_fetch())


if __name__ == "__main__":
    main()
