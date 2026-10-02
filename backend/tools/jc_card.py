# -*- coding: utf-8 -*-
"""合并体彩接口 + ai-football 接口，逐场判定「胜平负 / 让球盘」并排版。

判定只认体彩返回的 pool 是否为空：
    had={}  且 hhad非空  → 只有让球盘（禁止给胜平负方向）
    had非空 且 hhad={}   → 只有胜平负
    两者都非空            → 两个玩法都开

用法:
    .venv/bin/python tools/jc_card.py 2026-09-14 jingzu
"""
import sys, json, pathlib, urllib.request, urllib.parse

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-14"
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "jingzu"

SPORTTERY = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
             "?poolCode=hhad,had&channel=c")
AI_BASE = "http://127.0.0.1:8000/api/predictions"

# 体彩编号 -> ai-football fixture_id（来自用户提供的 002-012 票面编号）
JC2FID = json.loads((pathlib.Path(__file__).parent / "jc_map.json")
                    .read_text(encoding="utf-8"))["map"]


def devig(h, d, a):
    inv = [1.0 / float(x) for x in (h, d, a)]
    s = sum(inv)
    return [x / s for x in inv], s


def jc_fetch():
    req = urllib.request.Request(SPORTTERY, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://www.sporttery.cn/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    out = {}
    for day in d["value"]["matchInfoList"]:
        for m in day["subMatchList"]:
            out[m["matchNumStr"]] = m
    return out


def ai_fetch():
    q = urllib.parse.urlencode({"date": DATE, "category": CATEGORY, "page": 1, "page_size": 50})
    with urllib.request.urlopen(f"{AI_BASE}?{q}", timeout=60) as r:
        d = json.loads(r.read().decode())
    return {x["basic"]["fixture_id"]: x for x in d["data"]}


jc, ai = jc_fetch(), ai_fetch()
print(f"■ 体彩接口 {len(jc)} 场   +   ai-football 接口 {len(ai)} 场   ({DATE} / {CATEGORY})\n")

for num in sorted(jc, key=lambda s: (s[:2], int(s[2:]))):
    m = jc[num]
    fid = JC2FID.get(num)
    if fid is None or fid not in ai:
        print(f"{num}  {m.get('homeTeamAbbName')} vs {m.get('awayTeamAbbName')}  —— ai-football 无对应，跳过")
        continue
    r = ai[fid]
    x, llm, b = r["xgb"], r["llm"], r["basic"]
    had, hhad = m.get("had") or {}, m.get("hhad") or {}

    if had and hhad:
        play = "胜平负 + 让球胜平负"
    elif had:
        play = "只有胜平负"
    elif hhad:
        play = "★ 只有让球胜平负（买不到胜平负）"
    else:
        play = "★ 两个玩法都没开"

    print("═" * 92)
    print(f"{num}  {m.get('homeTeamAbbName')} vs {m.get('awayTeamAbbName')}  "
          f"《{m.get('leagueAbbName')}》 {m.get('matchTime','')}   [ai: {b['home_name']} vs {b['away_name']}]")
    print(f"   玩法   {play}")

    if had:
        p, s = devig(had["h"], had["d"], had["a"])
        print(f"   ├ 胜平负   主{had['h']:>6} 平{had['d']:>6} 客{had['a']:>6}   "
              f"去水 主{p[0]:.1%} 平{p[1]:.1%} 客{p[2]:.1%}  (返还{1/s:.1%})")
        print(f"   │          XGB 主{x['prob']['home']:.1%} 平{x['prob']['draw']:.1%} 客{x['prob']['away']:.1%}"
              f"    LLM {llm['win']} {llm['win_pct']}")
    else:
        print(f"   ├ 胜平负   —— 未开")

    if hhad:
        p, s = devig(hhad["h"], hhad["d"], hhad["a"])
        gi = int(float(hhad.get("goalLine", "0") or 0))
        side = f"主队让{abs(gi)}球" if gi < 0 else (f"主队受让{abs(gi)}球" if gi > 0 else "平手")
        print(f"   └ 让球     {side}   让胜{hhad['h']:>6} 让平{hhad['d']:>6} 让负{hhad['a']:>6}   "
              f"去水 让胜{p[0]:.1%} 让平{p[1]:.1%} 让负{p[2]:.1%}  (返还{1/s:.1%})")
        t3 = " / ".join(f"{t['score']}·{t['prob']}" for t in x["top3"])
        print(f"              LLM线 {llm['handicap_team']} {llm['handicap_num']} ({llm['handicap_pct']})"
              f"   XGB让球标签 {x['handicap']}")
        print(f"              XGB比分前三 {t3}")
    else:
        print(f"   └ 让球     —— 未开")
print("═" * 92)
