# -*- coding: utf-8 -*-
"""全批汇总表：体彩接口（盘口）+ ai-football 接口（模型），一次铺开。

三张表：
  表1  全部场次 · 体彩盘口        —— 编号/开球/对阵/联赛/玩法/胜平负赔率+去水/让球线+赔率+去水
  表2  有模型的场次 · XGB + LLM   —— 模型三向/λ/大小/前三比分 + LLM 方向/比分/让球/大小
  表3  让球线一致性 + 折算        —— LLM线 vs 竞彩线 + xgb.top3 比分折算成 让胜/让平/让负

[算]=去水比例法，[换算]=按 goalLine 折算，其余为接口原字段。

用法:
    .venv/bin/python tools/summary_table.py 2026-09-15 jingzu
"""
import sys, json, pathlib, unicodedata, urllib.request, urllib.parse

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-15"
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "jingzu"
ONLY_PRED = len(sys.argv) > 3 and sys.argv[3] == "pred"
SP = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
      "?poolCode=hhad,had&channel=c")
JC2FID = json.loads((pathlib.Path(__file__).parent / "jc_map.json")
                    .read_text(encoding="utf-8"))["map"]


def w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n, right=False):
    s = str(s)
    gap = max(0, n - w(s))
    return (" " * gap + s) if right else (s + " " * gap)


def devig(h, d, a):
    inv = [1.0 / float(x) for x in (h, d, a)]
    s = sum(inv)
    return [x / s for x in inv], 1.0 / s


req = urllib.request.Request(SP, headers={"User-Agent": "Mozilla/5.0",
                                          "Referer": "https://www.sporttery.cn/"})
with urllib.request.urlopen(req, timeout=30) as r:
    jd = json.loads(r.read().decode())
jc = {m["matchNumStr"]: m
      for day in jd["value"]["matchInfoList"] for m in day["subMatchList"]}

q = urllib.parse.urlencode({"date": DATE, "category": CATEGORY, "page": 1, "page_size": 50})
with urllib.request.urlopen(f"http://127.0.0.1:8000/api/predictions?{q}", timeout=60) as r:
    ai = {x["basic"]["fixture_id"]: x for x in json.loads(r.read().decode())["data"]}

order = sorted(jc, key=lambda s: (s[:2], int(s[2:])))
print(f"■ 汇总  {DATE}  batch={CATEGORY}    体彩接口 {len(jc)} 场 / 其中有模型 {len(ai)} 场")
print(f"  businessDate={jd['value'].get('businessDate')}    去水标 [算]，比分工式标 [换算]\n")

# ── 表1 盘口 ─────────────────────────────────────────────────────────────
print("═" * 150)
print("表1 · 体彩盘口" + ("（仅已跑批场次）" if ONLY_PRED else "（全部场次）"))
print("═" * 150)
hdr = (f"{pad('编号', 9)}{pad('开球', 8)}{pad('对阵', 28)}{pad('联赛', 12)}{pad('玩法', 14)}"
       f"{pad('胜平负 主/平/客', 22)}{pad('去水 主/平/客', 20)}"
       f"{pad('让球线', 10)}{pad('让球 胜/平/负', 22)}{pad('去水 胜/平/负', 20)}")
print(hdr)
print("─" * 150)
for num in order:
    m = jc[num]
    had, hhad = m.get("had") or {}, m.get("hhad") or {}
    nm = f"{m.get('homeTeamAbbName','')} vs {m.get('awayTeamAbbName','')}"
    if had and hhad:
        play = "两个都开"
    elif had:
        play = "只有胜平负"
    elif hhad:
        play = "★只有让球"
    else:
        play = "★都没开"
    if had:
        p, _ = devig(had["h"], had["d"], had["a"])
        sp = f"{had['h']}/{had['d']}/{had['a']}"
        dp = f"{p[0]:.1%}/{p[1]:.1%}/{p[2]:.1%}"
    else:
        sp = dp = "—"
    if hhad:
        p, _ = devig(hhad["h"], hhad["d"], hhad["a"])
        gi = int(float(hhad.get("goalLine", "0") or 0))
        line = f"主让{abs(gi)}" if gi < 0 else (f"主受{abs(gi)}" if gi > 0 else "平手")
        sp2 = f"{hhad['h']}/{hhad['d']}/{hhad['a']}"
        dp2 = f"{p[0]:.1%}/{p[1]:.1%}/{p[2]:.1%}"
    else:
        line = sp2 = dp2 = "—"
    has_model = JC2FID.get(num) in ai
    if ONLY_PRED and not has_model:
        continue
    tag = "" if has_model else "  ←无模型"
    print(f"{pad(num, 9)}{pad(str(m.get('matchTime',''))[:5], 8)}{pad(nm[:26], 28)}{pad(str(m.get('leagueAbbName',''))[:10], 12)}"
          f"{pad(play, 14)}{pad(sp, 22)}{pad(dp, 20)}{pad(line, 10)}{pad(sp2, 22)}{pad(dp2, 20)}{tag}")

# ── 表2 模型 ─────────────────────────────────────────────────────────────
print()
print("═" * 150)
print("表2 · 模型（XGB + LLM，仅本批已跑批场次）")
print("═" * 150)
print(f"{pad('编号', 9)}{pad('对阵', 28)}{pad('XGB 主/平/客', 20)}{pad('λ 主/客', 14)}{pad('大2.5', 8)}"
      f"{pad('XGB 前三比分', 26)}{pad('LLM 胜平负', 12)}{pad('LLM 比分', 16)}{pad('LLM 让球', 22)}{pad('LLM 大小', 16)}")
print("─" * 150)
for num in order:
    fid = JC2FID.get(num)
    if fid not in ai:
        continue
    r = ai[fid]; x, l, b = r["xgb"], r["llm"], r["basic"]
    t3 = " ".join(f"{t['score']}({t['prob']:.1%})" for t in x["top3"])
    hc = f"{l['handicap_team']}{l['handicap_num']} {l['handicap_pct']}"
    ou = f"{l['over_under']} {l['ou_pct']}"
    print(f"{pad(num, 9)}{pad((b['home_name'][:12]+' vs '+b['away_name'][:12]), 28)}"
          + pad(f"{x['prob']['home']:.1%}/{x['prob']['draw']:.1%}/{x['prob']['away']:.1%}", 20)
          + pad(f"{x['lambda']['home']:.2f}/{x['lambda']['away']:.2f}", 14)
          + pad(f"{x['over25']['over']:.1%}", 8)
          + pad(t3, 34)
          + f"{pad(l['win'] + ' ' + str(l['win_pct']), 12)}"
          + f"{pad(l['score'][:14], 16)}"
          + f"{pad(hc, 22)}{pad(ou, 16)}")

# ── 表3 让球线一致性 + 折算 ───────────────────────────────────────────────
print()
print("═" * 150)
print("表3 · 让球线一致性 + xgb.top3 按竞彩 goalLine 折算（标 [换算]）")
print("═" * 150)
print(f"{pad('编号', 9)}{pad('对阵', 28)}{pad('竞彩线', 12)}{pad('LLM线', 16)}{pad('一致', 8)}{pad('xgb.top3 折算结果', 46)}{'说明'}")
print("─" * 150)
for num in order:
    fid = JC2FID.get(num)
    m = jc[num]
    hhad = m.get("hhad") or {}
    if fid not in ai or not hhad:
        continue
    r = ai[fid]; x, l, b = r["xgb"], r["llm"], r["basic"]
    gi = int(float(hhad.get("goalLine", "0") or 0))
    try:
        ln = float(l["handicap_num"])
    except (TypeError, ValueError):
        ln = None
    ls = "平手" if ln == 0 else ("主队" if (ln or 0) < 0 else "客队")
    js = "主队" if gi < 0 else ("客队" if gi > 0 else "平手")
    same = ln is not None and ls == js and abs(abs(ln) - abs(gi)) < 1e-6
    tgt = -gi
    parts = []
    for t in x["top3"]:
        h, a = map(int, t["score"].split("-"))
        mm = h - a
        out = "让胜" if mm > tgt else ("让平" if mm == tgt else "让负")
        parts.append(f"{t['score']}→{out}({t['prob']:.1%})")
    jline = f"主让{abs(gi)}" if gi < 0 else (f"主受{abs(gi)}" if gi > 0 else "平手")
    note = "" if same else "让球腿无模型概率"
    print(f"{pad(num, 9)}{pad((b['home_name'][:12]+' vs '+b['away_name'][:12]), 28)}{pad(jline, 12)}"
          f"{pad(f'{ls}{ln}', 16)}" + f"{pad('★' if same else '✗', 8)}"
          + pad(" ".join(parts), 52) + note)
print("═" * 150)
