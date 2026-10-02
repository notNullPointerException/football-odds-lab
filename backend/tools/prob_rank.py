# -*- coding: utf-8 -*-
"""百分比降序表：胜平负 / 让球 两个玩法各一块，按赢面（市场去水）从高到低排。

每行 = 一个可下注选项。列：
  赔率     体彩接口原字段
  市场%    去水后概率 [算]，比例法归一
  模型%    xgb.prob（胜平负）/ llm.handicap_pct（让球，仅当 LLM 线 = 竞彩线）
  倍数     模型% ÷ 市场%

用法:
    .venv/bin/python tools/prob_rank.py 2026-09-15 jingzu
    .venv/bin/python tools/prob_rank.py 2026-09-15 jingzu model   # 改按模型%降序
"""
import sys, json, pathlib, unicodedata, urllib.request, urllib.parse

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-15"
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "jingzu"
SORT_BY = sys.argv[3] if len(sys.argv) > 3 else "market"   # market | model

SP = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
      "?poolCode=hhad,had&channel=c")
JC2FID = json.loads((pathlib.Path(__file__).parent / "jc_map.json")
                    .read_text(encoding="utf-8"))["map"]


def w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n):
    s = str(s)
    return s + " " * max(0, n - w(s))


def devig(h, d, a):
    inv = [1.0 / float(x) for x in (h, d, a)]
    s = sum(inv)
    return [x / s for x in inv]


req = urllib.request.Request(SP, headers={"User-Agent": "Mozilla/5.0",
                                          "Referer": "https://www.sporttery.cn/"})
with urllib.request.urlopen(req, timeout=30) as r:
    jd = json.loads(r.read().decode())
jc = {m["matchNumStr"]: m
      for day in jd["value"]["matchInfoList"] for m in day["subMatchList"]}

q = urllib.parse.urlencode({"date": DATE, "category": CATEGORY, "page": 1, "page_size": 50})
with urllib.request.urlopen(f"http://127.0.0.1:8000/api/predictions?{q}", timeout=60) as r:
    ai = {x["basic"]["fixture_id"]: x for x in json.loads(r.read().decode())["data"]}

rows = []
for num in sorted(jc, key=lambda s: (s[:2], int(s[2:]))):
    fid = JC2FID.get(num)
    if fid not in ai:
        continue
    r = ai[fid]; x, l, b = r["xgb"], r["llm"], r["basic"]
    nm = f"{b['home_name'][:12]} vs {b['away_name'][:12]}"
    had, hhad = jc[num].get("had") or {}, jc[num].get("hhad") or {}

    if had:
        p = devig(had["h"], had["d"], had["a"])
        for lab, odd, mp in [("主胜", had["h"], x["prob"]["home"]),
                             ("平", had["d"], x["prob"]["draw"]),
                             ("客胜", had["a"], x["prob"]["away"])]:
            rows.append(dict(play="胜平负", num=num, nm=nm, lab=lab, odd=float(odd),
                             mkt=p.pop(0), model=mp, line="—"))
    if hhad:
        p = devig(hhad["h"], hhad["d"], hhad["a"])
        gi = int(float(hhad.get("goalLine", "0") or 0))
        line = f"主让{abs(gi)}" if gi < 0 else (f"主受{abs(gi)}" if gi > 0 else "平手")
        try:
            ln = float(l["handicap_num"])
        except (TypeError, ValueError):
            ln = None
        ls = "平手" if ln == 0 else ("主队" if (ln or 0) < 0 else "客队")
        js = "主队" if gi < 0 else ("客队" if gi > 0 else "平手")
        same = ln is not None and ls == js and abs(abs(ln) - abs(gi)) < 1e-6
        lp = int(str(l["handicap_pct"]).rstrip("%")) / 100 if l.get("handicap_pct") else None
        tgt = ("让胜" if js == "主队" else "让负") if same else None
        for lab, odd in [("让胜", hhad["h"]), ("让平", hhad["d"]), ("让负", hhad["a"])]:
            rows.append(dict(play="让球", num=num, nm=nm, lab=lab, odd=float(odd),
                             line=line, mkt=p.pop(0), model=lp if lab == tgt else None))

key = (lambda r: -(r["model"] if r["model"] is not None else -1)) if SORT_BY == "model" \
    else (lambda r: -r["mkt"])
print(f"■ 赢面降序  {DATE} / {CATEGORY}   排序依据：{'模型%' if SORT_BY == 'model' else '市场%（去水）'}")
print(f"  [算] 市场% = 比例法去水；模型% 在让球上仅当 LLM 线 = 竞彩线时才有\n")

for play in sorted({r["play"] for r in rows}, key=lambda s: (s != "胜平负", s)):
    grp = sorted([r for r in rows if r["play"] == play], key=key)
    print("═" * 104)
    print(f"【{play}】{len(grp)} 个选项")
    print("═" * 104)
    print(f"  {pad('编号',8)}{pad('对阵',28)}{pad('盘口线',9)}{pad('选项',7)}{pad('赔率',8)}"
          f"{pad('市场%（去水）',14)}{pad('模型%',9)}{pad('倍数',8)}备注")
    print("  " + "─" * 100)
    for r in grp:
        mk = r["mkt"]
        if r["model"] is None:
            ms, mult, note = "—", "—", "无模型概率"
        else:
            ms = f"{r['model']:.1%}"
            mult = f"{r['model'] / mk:.2f}"
            note = f"[模型 {r['model']/mk:.2f}× 市场]" if r["model"] / mk >= 1.2 else ""
        star = "★" if (r["model"] is not None and r["model"] / mk >= 1.2
                       and r["odd"] >= 1.80) else ""
        print(f"  {pad(r['num'],8)}{pad(r['nm'],28)}{pad(r['line'],9)}{pad(r['lab'],7)}{pad(f'{r['odd']:.2f}',8)}"
              f"{pad(f'{mk:.1%}',14)}{pad(ms,9)}{pad(mult,8)}{note} {star}")
    print()
