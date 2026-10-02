# -*- coding: utf-8 -*-
"""把 GET /api/predictions 的返回原样排版成模版。不计算、不推断、不加字段。

用法:
    .venv/bin/python tools/render_api.py 2026-09-14 jingzu
"""
import sys, json, urllib.request, urllib.parse

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-14"
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "jingzu"
BASE = "http://127.0.0.1:8000/api/predictions"

q = urllib.parse.urlencode({"date": DATE, "category": CATEGORY, "page": 1, "page_size": 50})
with urllib.request.urlopen(f"{BASE}?{q}", timeout=60) as r:
    resp = json.loads(r.read().decode())

MODE = sys.argv[3] if len(sys.argv) > 3 else "full"
rows = resp["data"]
print(f"■ 接口 GET /api/predictions?date={DATE}&category={CATEGORY}")
print(f"  total={resp['total']}  page={resp['page']}  page_size={resp['page_size']}  返回={len(rows)} 条\n")


def _v(x):
    """None -> '-'"""
    return "-" if x is None else x


def _xg(x):
    """None -> 无数据，有值保留 2 位"""
    return "无数据" if x is None else "%.2f" % x

if MODE == "card":
    for i, r in enumerate(rows, 1):
        b, x, m = r["basic"], r["xgb"], r["llm"]
        t3 = "/".join(f"{t['score']}·{t['prob']:.3f}" for t in x["top3"])
        print(f"[{i:02d}] {b['home_name']} vs {b['away_name']}  《{b['league_name']}》  {b['match_date']}")
        print(f"     XGB  {x['model_group']}  主{x['prob']['home']:.3f} 平{x['prob']['draw']:.3f} 客{x['prob']['away']:.3f}"
              f"  |  λ {x['lambda']['home']:.3f}/{x['lambda']['away']:.3f}"
              f"  |  大2.5 {x['over25']['over']:.3f}")
        print(f"          比分前三 {t3}")
        print(f"          让球标签 {x['handicap']}")
        print(f"     LLM  {m['win']} {m['win_pct']}  |  比分 {m['score']}"
              f"  |  让 {m['handicap']}({m['handicap_team']},num={m['handicap_num']}) {m['handicap_pct']}"
              f"  |  {m['over_under']} {m['ou_pct']}")
        print(f"          依据 {m['brief']}")
        print(f"     RES  {r['result']}")
        c = r.get("ctx")
        if c and c.get("available"):
            rest = "休息 %s天/%s天" % (_v(c["rest_days_home"]), _v(c["rest_days_away"]))
            xg = "xG5 主 %s / 客 %s" % (_xg(c["xg5_home"]), _xg(c["xg5_away"]))
            hh = c.get("h2h") or []
            h2h = "H2H " + (" ".join(f"{x['date'][:10]} {x['score']} {x['result']}"
                                     for x in hh) if hh else "无交锋")
            print(f"     CTX  {rest}  |  {xg}  |  {h2h}")
        print()
    print("═" * 78)
    sys.exit()

for i, r in enumerate(rows, 1):
    b, x, m = r["basic"], r["xgb"], r["llm"]
    print("═" * 78)
    print(f"[{i:02d}] {b['home_name']} vs {b['away_name']}   《{b['league_name']}》")
    print(f"     fixture_id={b['fixture_id']}   match_date={b['match_date']}   status={b['status_short']}   category={b['category']}")
    print("─" * 78)
    print("  ▼ xgb（模型）")
    print(f"    model_group = {x['model_group']}")
    print(f"    prob        主 {x['prob']['home']:.3f} / 平 {x['prob']['draw']:.3f} / 客 {x['prob']['away']:.3f}")
    print(f"    lambda      主 {x['lambda']['home']:.3f} / 客 {x['lambda']['away']:.3f}")
    print(f"    over25      大 {x['over25']['over']:.3f} / 小 {x['over25']['under']:.3f}")
    t3 = "  ".join(f"{t['score']}({t['prob']:.3f})" for t in x["top3"])
    print(f"    top3        {t3}")
    print(f"    handicap    {x['handicap']}")
    print("  ▼ llm（大模型）")
    print(f"    win         {m['win']}  {m['win_pct']}")
    print(f"    score       {m['score']}")
    print(f"    handicap    {m['handicap']}  num={m['handicap_num']}  team={m['handicap_team']}  pct={m['handicap_pct']}")
    print(f"    over_under  {m['over_under']}  line={m['ou_line']}  type={m['ou_type']}  pct={m['ou_pct']}")
    print(f"    brief       {m['brief']}")
    print(f"    core_data   {m['core_data']}")
    if m["deep_report"]:
        print(f"    deep_report {m['deep_report']}")
    print("  ▼ result（未开赛=null）")
    print(f"    {r['result']}")
    print("  ▼ ctx（赛前背景）")
    c = r.get("ctx")
    if c and c.get("available"):
        print(f"    休息天数  主 {_v(c['rest_days_home'])} / 客 {_v(c['rest_days_away'])}")
        print(f"    xG5       主 {_xg(c['xg5_home'])} (n={c['xg5_home_n']}) / 客 {_xg(c['xg5_away'])} (n={c['xg5_away_n']})")
        hh = c.get("h2h") or []
        h2h = "  ".join(f"{x['date'][:10]} {x['score']} {x['result']}"
                        for x in hh) if hh else "无交锋"
        print(f"    H2H(近3)  {h2h}")
    else:
        print("    无数据")
print("═" * 78)
