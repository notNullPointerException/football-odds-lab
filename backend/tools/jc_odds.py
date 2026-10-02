# -*- coding: utf-8 -*-
"""体彩官方接口：拉竞彩足球当日赛程 + 玩法 + 赔率。

接口返回每场带 had（胜平负）与 hhad（让球胜平负）两个池子：
  had  为空 {}  → 该场没开胜平负
  hhad 为空 {}  → 该场没开让球胜平负
hhad.goalLine 是竞彩整数让球线（负=主队让）。

用法:
    .venv/bin/python tools/jc_odds.py                 # 全部场次
    .venv/bin/python tools/jc_odds.py 周一003 周一005  # 只看指定编号
"""
import sys, json, urllib.request

URL = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
       "?poolCode=hhad,had&channel=c")


def fetch():
    req = urllib.request.Request(URL, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.sporttery.cn/",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def devig(h, d, a):
    """按赔率倒数归一化去水，返回 (主, 平, 客) 概率。"""
    inv = [1.0 / float(x) for x in (h, d, a)]
    s = sum(inv)
    return [x / s for x in inv], s


data = fetch()
want = set(sys.argv[1:])
subs = []
for day in data["value"]["matchInfoList"]:
    subs.extend(day["subMatchList"])

print(f"■ 体彩接口 {URL.split('?')[0]}")
print(f"  返回 {len(subs)} 场   businessDate={data['value']['matchInfoList'][0]['businessDate']}\n")

for m in subs:
    num = m.get("matchNumStr", "")
    if want and num not in want:
        continue
    home = m.get("homeTeamAbbName", "")
    away = m.get("awayTeamAbbName", "")
    lg = m.get("leagueAbbName", "")
    had, hhad = m.get("had") or {}, m.get("hhad") or {}
    print("─" * 78)
    print(f"{num}  {home} vs {away}  《{lg}》  {m.get('matchTime','')}  [{m.get('matchStatus','')}]")
    if had:
        p, s = devig(had["h"], had["d"], had["a"])
        print(f"  胜平负     主 {had['h']:>5}  平 {had['d']:>5}  客 {had['a']:>5}"
              f"   去水 主{p[0]:.1%} 平{p[1]:.1%} 客{p[2]:.1%}   (返还率 {1/s:.1%})")
    else:
        print(f"  胜平负     ——  未开")
    if hhad:
        p, s = devig(hhad["h"], hhad["d"], hhad["a"])
        gl = hhad.get("goalLine", "")
        gi = int(float(gl)) if gl not in ("", None) else 0
        side = "主队让" if gi < 0 else ("主队受让" if gi > 0 else "平手")
        print(f"  让球胜平负 {side}{abs(gi)}球   让胜 {hhad['h']:>5}  让平 {hhad['d']:>5}  让负 {hhad['a']:>5}"
              f"   去水 让胜{p[0]:.1%} 让平{p[1]:.1%} 让负{p[2]:.1%}   (返还率 {1/s:.1%})")
    else:
        print(f"  让球胜平负 ——  未开")
print("─" * 78)
