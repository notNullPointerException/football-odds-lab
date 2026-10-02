# -*- coding: utf-8 -*-
"""完整卡片：体彩接口（编号/玩法/竞彩线/赔率） + ai-football 接口（xgb/llm），逐场排版。

不新增判断，只做三件事：
  1. 原样铺开两个接口的字段
  2. 去水概率（我算的，比例法，标注 [算]）
  3. 把 xgb.top3 的比分按竞彩 goalLine 换算成 让胜/让平/让负（纯换算，标注 [换算]）

用法:
    .venv/bin/python tools/render_cards.py 2026-09-15 jingzu
"""
import sys, json, pathlib, urllib.request, urllib.parse

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-15"
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "jingzu"

SPORTTERY = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
             "?poolCode=hhad,had&channel=c")
AI = "http://127.0.0.1:8000/api/predictions"
JC2FID = json.loads((pathlib.Path(__file__).parent / "jc_map.json")
                    .read_text(encoding="utf-8"))["map"]


def devig(h, d, a):
    inv = [1.0 / float(x) for x in (h, d, a)]
    s = sum(inv)
    return [x / s for x in inv], 1.0 / s


def jc_fetch():
    req = urllib.request.Request(SPORTTERY, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://www.sporttery.cn/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    return {m["matchNumStr"]: m
            for day in d["value"]["matchInfoList"] for m in day["subMatchList"]}


def ai_fetch():
    q = urllib.parse.urlencode({"date": DATE, "category": CATEGORY, "page": 1, "page_size": 50})
    with urllib.request.urlopen(f"{AI}?{q}", timeout=60) as r:
        return {x["basic"]["fixture_id"]: x for x in json.loads(r.read().decode())["data"]}


jc, ai = jc_fetch(), ai_fetch()
rows = []
for num, m in jc.items():
    fid = JC2FID.get(num)
    if fid in ai:
        rows.append((m.get("matchTime", ""), num, m, ai[fid]))
rows.sort(key=lambda t: (t[0], t[1]))

print(f"■ 体彩接口 {len(jc)} 场  ×  ai-football 接口 {len(ai)} 场   ({DATE} / {CATEGORY})")
print(f"  本批 {len(rows)} 场。去水/换算均标注 [算]/[换算]，其余为接口原字段。\n")

for _, num, m, r in rows:
    b, x, l = r["basic"], r["xgb"], r["llm"]
    had, hhad = m.get("had") or {}, m.get("hhad") or {}

    if had and hhad:
        play = "胜平负 + 让球胜平负"
    elif had:
        play = "只有胜平负"
    elif hhad:
        play = "★ 只有让球胜平负（买不到胜平负）"
    else:
        play = "★ 两个玩法都没开"

    print("═" * 96)
    print(f"{num}  {b['home_name']} vs {b['away_name']}   《{m.get('leagueAbbName')}》  "
          f"{m.get('matchTime','')}   [fixture_id={b['fixture_id']}]  status={b['status_short']}")
    print(f"  玩法判定  {play}     （判据：体彩 had/hhad 是否为空）")
    print("  " + "─" * 94)

    # ── 盘口（体彩接口原字段 + 去水）
    if had:
        p, ret = devig(had["h"], had["d"], had["a"])
        print(f"  ▼ 胜平负   主 {had['h']:>6}  平 {had['d']:>6}  客 {had['a']:>6}     "
              f"[算] 去水 主 {p[0]:.1%} / 平 {p[1]:.1%} / 客 {p[2]:.1%}   返还 {ret:.1%}")
    else:
        print(f"  ▼ 胜平负   —— 未开售")
    if hhad:
        p, ret = devig(hhad["h"], hhad["d"], hhad["a"])
        gi = int(float(hhad.get("goalLine", "0") or 0))
        side = f"主队让{abs(gi)}球" if gi < 0 else (f"主队受让{abs(gi)}球" if gi > 0 else "平手")
        print(f"  ▼ 让球     {side}（goalLine={gi}）   让胜 {hhad['h']:>6}  让平 {hhad['d']:>6}  让负 {hhad['a']:>6}     "
              f"[算] 去水 让胜 {p[0]:.1%} / 让平 {p[1]:.1%} / 让负 {p[2]:.1%}   返还 {ret:.1%}")
    else:
        print(f"  ▼ 让球     —— 未开售")
    print("  " + "─" * 94)

    # ── XGB（接口原字段）
    print(f"  ▼ xgb   model_group={x['model_group']}")
    print(f"          prob    主 {x['prob']['home']:.3f} / 平 {x['prob']['draw']:.3f} / 客 {x['prob']['away']:.3f}"
          f"      λ 主 {x['lambda']['home']:.3f} / 客 {x['lambda']['away']:.3f}"
          f"      over25 大 {x['over25']['over']:.3f} / 小 {x['over25']['under']:.3f}")
    t3 = "   ".join(f"{t['score']}·{t['prob']:.4f}" for t in x["top3"])
    print(f"          top3    {t3}")
    print(f"          handicap {x['handicap']}   ← 是 |λh−λa| 的分档文字标签，不是盘口")

    # ── LLM（接口原字段）
    print(f"  ▼ llm   win {l['win']} {l['win_pct']}      score {l['score']}")
    print(f"          handicap {l['handicap']}  让球方={l['handicap_team']}  num={l['handicap_num']}  pct={l['handicap_pct']}")
    print(f"          over_under {l['over_under']}  line={l['ou_line']}  type={l['ou_type']}  pct={l['ou_pct']}")
    if l.get("brief"):
        print(f"          brief      {l['brief']}")

    # ── 换算：xgb.top3 比分 → 竞彩让球结果（纯换算，不是新指标）
    if hhad:
        gi = int(float(hhad.get("goalLine", "0") or 0))
        tgt = -gi
        parts = []
        for t in x["top3"]:
            h, a = map(int, t["score"].split("-"))
            mm = h - a
            out = "让胜" if mm > tgt else ("让平" if mm == tgt else "让负")
            parts.append(f"{t['score']}→{out}({t['prob']:.1%})")
        print(f"  ▼ [换算] xgb.top3 比分按竞彩 goalLine={gi} 折算    " + "   ".join(parts))
        try:
            lnum = float(l["handicap_num"])
        except (TypeError, ValueError):
            lnum = None
        ls = "平手" if lnum == 0 else ("主队" if (lnum or 0) < 0 else "客队")
        js = "主队" if gi < 0 else ("客队" if gi > 0 else "平手")
        same = lnum is not None and ls == js and abs(abs(lnum) - abs(gi)) < 1e-6
        print(f"          LLM 线 vs 竞彩线：{'★ 一致' if same else f'✗ 不一致（竞彩={js}{abs(gi)}球, LLM={ls}{lnum}球）'}"
              + ("" if same else "   → 让球腿拿不到模型概率"))
    print()
