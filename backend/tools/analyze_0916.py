# -*- coding: utf-8 -*-
"""复盘分析：接口预测 vs 实际赛果，四层命中率 + Brier。"""
import json, urllib.request, urllib.parse, pathlib, unicodedata

JC2FID = json.loads(pathlib.Path("tools/jc_map.json").read_text(encoding="utf-8"))["map"]
FID2JC = {v: k for k, v in JC2FID.items()}

# 竞彩整数让球线（体彩接口实测，负=主队让）
JCLINE = {"周三004": +1, "周三005": +1, "周三006": -1, "周三007": -1, "周三008": -2,
          "周三009": -1, "周三010": +1, "周三011": +1, "周三012": +1, "周三013": -3,
          "周三014": +1, "周三015": -1, "周三016": -1, "周三017": -1}


def w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n):
    s = str(s)
    return s + " " * max(0, n - w(s))


q = urllib.parse.urlencode({"date": "2026-09-16", "category": "jingzu", "page": 1, "page_size": 50})
with urllib.request.urlopen(f"http://127.0.0.1:8000/api/predictions?{q}", timeout=60) as r:
    api = json.load(r)

rows = []
for b in api["data"]:
    num = FID2JC.get(b["basic"]["fixture_id"])
    if not num:
        continue
    r_ = b["result"]
    if not r_ or not r_.get("score"):
        continue
    rows.append((num, b))
rows.sort()

print(f"接口 {api['total']} 场，已结出 {len(rows)} 场\n")
print(pad("编号", 8) + pad("对阵", 26) + pad("比分", 6) + pad("实际", 5)
      + pad("XGB顶选", 11) + pad("LLM顶选", 11)
      + pad("LLM让球(自己线->要求净胜)", 28) + pad("竞彩三档", 9) + "大小")
print("-" * 122)

xok = lok = handok = t3ok = ouk = 0
detail = []
for num, b in rows:
    x, l = b["xgb"], b["llm"]
    sc = b["result"]["score"]
    hg, ag = (int(z) for z in sc.split("-"))
    tot = hg + ag
    diff = hg - ag
    act = "主胜" if hg > ag else ("平局" if hg == ag else "客胜")
    xp = max(x["prob"], key=x["prob"].get)
    xn = {"home": "主胜", "draw": "平局", "away": "客胜"}[xp]
    xh = xn == act
    lw = l["win"]
    lh = lw == act
    xok += xh
    lok += lh

    # 竞彩三档
    line = JCLINE[num]
    d = diff + line
    hres = "让胜" if d > 0 else ("让平" if d == 0 else "让负")
    # LLM 让球主张：主队让N球 => 主净胜>=N+1；客队让N球 => 客净胜>=N+1；平手盘 => 净胜==0
    hn, ht = l.get("handicap_num"), l.get("handicap_team")
    try:
        nv = abs(float(hn)) if hn is not None else None
    except (TypeError, ValueError):
        nv = None
    if nv is None:
        claim, need = "?", lambda dd: False
    elif nv == 0:
        claim, need = "平手盘", lambda dd: dd == 0
    else:
        k = int(nv) + 1          # 让 N 球 → 需净胜 ≥ N+1（0.5 线需净胜≥1）
        if ht == "主队":
            claim, need = f"主净胜>={k}", (lambda kk: (lambda dd: dd >= kk))(k)
        elif ht == "客队":
            claim, need = f"客净胜>={k}", (lambda kk: (lambda dd: dd <= -kk))(k)
        else:
            claim, need = "?", lambda dd: False
    hh = need(diff)
    handok += hh

    t3 = [t["score"] for t in x["top3"]]
    t3h = sc in t3
    t3ok += t3h
    ou_pred = "大" if x["over25"]["over"] >= 0.5 else "小"
    ou_act = "大" if tot >= 3 else "小"
    ouh = ou_pred == ou_act
    ouk += ouh

    detail.append(dict(num=num, sc=sc, act=act, xh=xh, lh=lh, hh=hh, t3h=t3h, ouh=ouh,
                       tot=tot, diff=diff, line=line, hres=hres, ou_pred=ou_pred, ou_act=ou_act,
                       xn=xn, xprob=x["prob"][xp], lw=lw, lp=l["win_pct"]))

    print(pad(num, 8) + pad(f"{b['basic']['home_name'][:11]} vs {b['basic']['away_name'][:11]}", 26)
          + pad(sc, 6) + pad(act, 5)
          + pad(f"{xn}{x['prob'][xp]:.0%} {'OK' if xh else 'XX'}", 11)
          + pad(f"{lw}{l['win_pct']} {'OK' if lh else 'XX'}", 11)
          + pad(f"{l['handicap']}({l['handicap_pct']}) 要求{claim} {'OK' if hh else 'XX'}", 28)
          + pad(f"{hres}{'OK' if (hres==('让胜' if lw=='主胜' else '让平' if lw=='平局' else '让负')) else ''}", 9)
          + f"{ou_pred}/{ou_act}{'OK' if ouh else 'XX'}")

n = len(rows)
print()
print(f"■ 胜平负顶选   XGB {xok}/{n} = {xok/n:.0%}    LLM {lok}/{n} = {lok/n:.0%}")
print(f"■ 让球主张     LLM 按其自报线全部命中 {handok}/{n} = {handok/n:.0%}")
print(f"■ 比分         XGB top3 含实际比分 {t3ok}/{n} = {t3ok/n:.0%}")
print(f"■ 大小球       XGB over25 {ouk}/{n} = {ouk/n:.0%}  (说大 {sum(1 for d in detail if d['ou_pred']=='大')} 中 {sum(1 for d in detail if d['ou_pred']=='大' and d['ouh'])} | 说小 {sum(1 for d in detail if d['ou_pred']=='小')} 中 {sum(1 for d in detail if d['ou_pred']=='小' and d['ouh'])})")

bt = 0
for num, b in rows:
    hg, ag = (int(z) for z in b["result"]["score"].split("-"))
    act = "home" if hg > ag else ("draw" if hg == ag else "away")
    p = b["xgb"]["prob"]
    bt += sum((p[k] - (1.0 if k == act else 0.0)) ** 2 for k in ("home", "draw", "away"))
print(f"■ Brier(XGB 1x2) = {bt/n:.4f}   (无信息基准 0.6667)")
print(f"■ 平局 {sum(1 for d in detail if d['act']=='平局')}/{n}；场均进球 {sum(d['tot'] for d in detail)/n:.2f}；实际大球 {sum(1 for d in detail if d['tot']>=3)}/{n}")
print(f"■ 最深净胜 {max(d['diff'] for d in detail)}（013 巴萨 7-2）；净胜恰好=让-平 的场次：" +
      ",".join(f"{d['num']}({d['sc']})" for d in detail if d['hres'] == '让平'))
