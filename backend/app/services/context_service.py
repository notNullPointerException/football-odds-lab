# -*- coding: utf-8 -*-
"""预测卡片「ctx 赛前背景」批量查询（纯读，展示层专用）。

给 /api/predictions 每页批量补三个赛前数：
  1. 休息天数  rest_days_home/away —— 上一场与本场的日历日差向下取整
  2. 近5场xG  xg5_home/away(+_n)  —— 本场之前最近 5 场里 xG 有值的均值
  3. 最近交锋  h2h                 —— 本场之前 1100 天内的交锋，最多 3 场

与 prediction/features.py 的 h2h_*/h_xg5 同源同语义，但这里只展示、不重训。
全部批量 SQL（每页 4 次 round-trip，无 N+1）；查询失败由调用方 try 兜底。
"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import text

FORM_WINDOW_DAYS = 400   # 近期比赛窗口
H2H_WINDOW_DAYS = 1100   # 交锋窗口
RECENT_N = 8             # 每队拉几场近期（第 1 场算休息，前 5 场算 xG5）
XG_N = 5
H2H_N = 3
FINISHED = ("FT", "AET", "PEN")


def _ctx(available=False):
    return {"available": available,
            "rest_days_home": None, "rest_days_away": None,
            "xg5_home": None, "xg5_away": None,
            "xg5_home_n": 0, "xg5_away_n": 0,
            "h2h": []}


EMPTY_CTX = _ctx(False)


def _pf(v):
    """stat_value -> float；容忍 NULL/'None'/带 % 的串（同 features.py 的 pf()）"""
    if v is None or v == "None":
        return None
    try:
        return float(str(v).replace("%", ""))
    except (TypeError, ValueError):
        return None


def _rest_days(fdate, pdate):
    """上一场 -> 本场的休息天数：日历日差向下取整。
    fixtures.date 是 UTC 存成北京时间墙钟，与 10:10 分界最多差 1 天，展示可接受；
    以后要改口径只改这里。"""
    return max(0, (fdate - pdate).days)


def context_for_fixtures(db, fixture_ids):
    """返回 {fixture_id: ctx dict}；查不到元数据的场 = available:false。"""
    fids = sorted({f for f in fixture_ids if f})
    out = {f: _ctx(False) for f in fids}
    if not fids:
        return out

    # ── A. 元数据 ──
    meta_rows = db.execute(
        text("SELECT id, date, home_id, away_id FROM fixtures WHERE id IN :fids"),
        {"fids": tuple(fids)},
    ).fetchall()
    metas = {m["id"]: m for m in (dict(r._mapping) for r in meta_rows)}
    for f in fids:
        if f in metas:
            out[f] = _ctx(True)
    if not metas:
        return out

    fdates = [m["date"] for m in metas.values() if m["date"]]
    if not fdates:
        return out
    max_date, min_date = max(fdates), min(fdates)
    tids = sorted({m["home_id"] for m in metas.values() if m["home_id"]}
                  | {m["away_id"] for m in metas.values() if m["away_id"]})
    if not tids:
        return out

    # ── B. 两队近期比赛：主/客两侧按本队视角展开，Python 里取最近 RECENT_N 场 ──
    form_rows = db.execute(text("""
        SELECT id AS fid, date AS dt, home_id AS team_id, away_id AS opp_id,
               goals_home AS gf, goals_away AS ga
        FROM fixtures
        WHERE home_id IN :tids AND date >= :ws AND date <= :we
          AND status_short IN :st
          AND goals_home IS NOT NULL AND goals_away IS NOT NULL
        UNION ALL
        SELECT id AS fid, date AS dt, away_id AS team_id, home_id AS opp_id,
               goals_away AS gf, goals_home AS ga
        FROM fixtures
        WHERE away_id IN :tids AND date >= :ws AND date <= :we
          AND status_short IN :st
          AND goals_home IS NOT NULL AND goals_away IS NOT NULL
    """), {"tids": tuple(tids), "ws": max_date - timedelta(days=FORM_WINDOW_DAYS),
           "we": max_date, "st": FINISHED}).fetchall()

    form = defaultdict(list)  # team_id -> 按时间倒序的近期比赛
    for r in form_rows:
        form[r.team_id].append(dict(r._mapping))
    for lst in form.values():
        lst.sort(key=lambda m: (m["dt"], m["fid"]), reverse=True)
        del lst[RECENT_N:]

    # ── C. 这些近期场次的 xG（库中 43% stat_value 为 NULL，必须过滤）──
    cand = {m["fid"] for lst in form.values() for m in lst}
    xg_map = {}
    if cand:
        for r in db.execute(text("""
            SELECT fixture_id, team_id, stat_value
            FROM fixture_statistics
            WHERE fixture_id IN :fids AND stat_type = 'expected_goals'
              AND stat_value IS NOT NULL AND stat_value <> 'None'
        """), {"fids": tuple(cand)}).fetchall():
            m = dict(r._mapping)
            v = _pf(m["stat_value"])
            if v is not None:
                xg_map[(m["fixture_id"], m["team_id"])] = v

    # ── D. 页内球队两两交锋（1100 天窗，Python 里按对阵分组）──
    h2h_rows = db.execute(text("""
        SELECT id, date, home_id, away_id, goals_home, goals_away
        FROM fixtures
        WHERE home_id IN :tids AND away_id IN :tids
          AND date >= :hs AND date <= :he
          AND status_short IN :st
          AND goals_home IS NOT NULL AND goals_away IS NOT NULL
    """), {"tids": tuple(tids), "hs": min_date - timedelta(days=H2H_WINDOW_DAYS),
           "he": max_date, "st": FINISHED}).fetchall()
    h2h_by_pair = defaultdict(list)
    for r in h2h_rows:
        m = dict(r._mapping)
        h2h_by_pair[frozenset((m["home_id"], m["away_id"]))].append(m)
    for lst in h2h_by_pair.values():
        lst.sort(key=lambda m: (m["date"], m["id"]), reverse=True)

    # ── 组装 ──
    for f, m in metas.items():
        ctx = out[f]
        fdate = m["date"]
        hid, aid = m["home_id"], m["away_id"]
        if fdate is None:
            continue

        for side, tid in (("home", hid), ("away", aid)):
            prior = [x for x in form.get(tid, []) if x["dt"] < fdate]
            if not prior:
                continue
            ctx["rest_days_" + side] = _rest_days(fdate, prior[0]["dt"])
            vals = [xg_map.get((x["fid"], tid)) for x in prior[:XG_N]]
            vals = [v for v in vals if v is not None]
            if vals:
                ctx["xg5_" + side] = round(sum(vals) / len(vals), 3)
                ctx["xg5_" + side + "_n"] = len(vals)

        hh = []
        for x in h2h_by_pair.get(frozenset((hid, aid)), []):
            d = x["date"]
            if not (d < fdate and d >= fdate - timedelta(days=H2H_WINDOW_DAYS)):
                continue
            if x["home_id"] == hid:          # 以本场主队视角给 W/D/L
                g1, g2 = x["goals_home"], x["goals_away"]
            else:
                g1, g2 = x["goals_away"], x["goals_home"]
            res = "W" if g1 > g2 else ("D" if g1 == g2 else "L")
            hh.append({"date": d.isoformat(), "score": "%s-%s" % (g1, g2),
                       "result": res, "home_id": x["home_id"], "away_id": x["away_id"]})
            if len(hh) >= H2H_N:
                break
        ctx["h2h"] = hh

    return out
