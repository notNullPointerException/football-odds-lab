# -*- coding: utf-8 -*-
"""刷新冷门日快照：按 match_date(北京时间) 的 weekday 分组，统计主胜率/平率 + XGB高置信腿命中。

口径见记忆 football-coldday-stats：
  - match_date 存的是北京时间，星期分组 = 竞彩批次日口径（不是 kickoff 的 UTC 星期）
  - 「XGB 高置信腿」= max(win_home, win_away) >= 0.55，按预测方向判命中
用法：.venv/bin/python tools/cold_snapshot.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def dow_name(dw):
    """MySQL DAYOFWEEK: 1=Sun .. 7=Sat -> Mon..Sun。"""
    return WD[(dw + 5) % 7]


db = SessionLocal()
try:
    rows = db.execute(text("""
        SELECT DAYOFWEEK(match_date) dw,
               COUNT(*) n,
               SUM(actual_home_goals > actual_away_goals) hw,
               SUM(actual_home_goals = actual_away_goals) dr,
               SUM(actual_home_goals < actual_away_goals) aw
        FROM predictions WHERE actual_home_goals IS NOT NULL
        GROUP BY DAYOFWEEK(match_date)
    """)).fetchall()
    print(f"总已完结 {sum(r[1] for r in rows)} 场\n")
    print(f"{'星期':<5}{'场次':>7}{'主胜率':>9}{'平率':>8}{'客胜率':>9}")
    for dw, n, hw, dr, aw in sorted(rows, key=lambda r: (r[0] + 5) % 7):
        hw, dr, aw = int(hw or 0), int(dr or 0), int(aw or 0)
        print(f"{dow_name(dw):<5}{n:>7}{hw/n:>9.1%}{dr/n:>8.1%}{aw/n:>9.1%}")

    print()
    r2 = db.execute(text("""
        SELECT DAYOFWEEK(match_date) dw, COUNT(*) n,
               SUM(CASE WHEN win_home >= win_away AND actual_home_goals > actual_away_goals THEN 1
                        WHEN win_away >  win_home AND actual_home_goals < actual_away_goals THEN 1
                        ELSE 0 END) hit
        FROM predictions
        WHERE actual_home_goals IS NOT NULL
          AND GREATEST(win_home, win_away) >= 0.55
        GROUP BY DAYOFWEEK(match_date)
    """)).fetchall()
    print(f"{'星期':<5}{'高置信腿':>9}{'命中':>7}{'命中率':>9}")
    for dw, n, hit in sorted(r2, key=lambda r: (r[0] + 5) % 7):
        print(f"{dow_name(dw):<5}{n:>9}{int(hit):>7}{int(hit)/n:>9.1%}")
finally:
    db.close()
