# -*- coding: utf-8 -*-
"""复盘 9-17 周四批次 003-011(8场, 011 缺 fixture): 接口预测 vs 实际赛果 + 市场基准。
本批新增「市场热门命中率」——9-16 批次漏算导致无法判断"模型失准 vs 大盘偏移"。
"""
import json, urllib.request, urllib.parse, pathlib, unicodedata

JC2FID = json.loads(pathlib.Path("tools/jc_map.json").read_text(encoding="utf-8"))["map"]
FID2JC = {v: k for k, v in JC2FID.items()}

# 竞彩整数让球线（体彩接口赛前实测，负=主队让）
JCLINE = {"周四003": +1, "周四004": -1, "周四005": -1, "周四006": +1,
          "周四007": -2, "周四008": -1, "周四009": -1, "周四010": +1}

# 赛前体彩赔率快照（来源 /tmp/sp_0917.txt，2026-09-17 21:23 拉取）。
# 赛后盘口会下架/变价，必须用赛前值算市场基准。
ODDS = {
    "周四003": ((8.80, 5.70, 1.19), (3.53, 3.88, 1.70)),
    "周四004": ((1.42, 3.75, 6.30), (2.58, 3.10, 2.39)),
    "周四005": ((1.23, 5.35, 7.75), (1.75, 4.00, 3.25)),
    "周四006": ((3.00, 3.24, 2.05), (1.57, 3.90, 4.25)),
    "周四007": ((1.08, 7.30, 15.00), (2.09, 3.60, 2.68)),
    "周四008": ((1.64, 3.80, 3.90), (3.06, 3.45, 1.95)),
    "周四009": ((1.45, 4.15, 5.05), (2.34, 3.75, 2.30)),
    "周四010": ((3.65, 3.60, 1.73), (1.86, 3.60, 3.20)),
}


def devig(o):
    inv = [1.0 / float(x) for x in o]
    s = sum(inv)
    return [x / s for x in inv]


def w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n):
    s = str(s)
    return s + " " * max(0, n - w(s))


q = urllib.parse.urlencode({"date": "2026-09-17", "category": "jingzu", "page": 1, "page_size": 50})
with urllib.request.urlopen(f"http://127.0.0.1:8000/api/predictions?{q}", timeout=60) as r:
    api = json.load(r)

rows = []
for b in api["data"]:
    num = FID2JC.get(b["basic"]["fixture_id"])
    if not num:
        continue
    r_ = b["result"] or {}
    if not r_.get("score"):
        continue
    rows.append((num, b))
rows.sort()

print(f"接口 {api['total']} 场，已结出 {len(rows)} 场\n")
print(pad("编号", 8) + pad("对阵", 30) + pad("比分", 6) + pad("实际", 5)
      + pad("市场热门", 13) + pad("XGB顶选", 13) + pad("LLM顶选", 12)
      + pad("竞彩三档", 9) + "大小")
print("-" * 118)

xok = lok = mok = t3ok = ouk = 0
detail = []
for num, b in rows:
    x, l = b["xgb"], b["llm"]
    sc = b["result"]["score"]
    hg, ag = (int(z) for z in sc.split("-"))
    tot, diff = hg + ag, hg - ag
    act = "主胜" if hg > ag else ("平局" if hg == ag else "客胜")

    # --- 市场基准（本批新指标）---
    mk = devig(ODDS[num][0])
    mi = max(range(3), key=lambda i: mk[i])
    mname = ("主胜", "平局", "客胜")[mi]
    mh = mname == act
    mok += mh

    xp = max(x["prob"], key=x["prob"].get)
    xn = {"home": "主胜", "draw": "平局", "away": "客胜"}[xp]
    xh = xn == act
    lw = l["win"]
    lh = lw == act
    xok += xh
    lok += lh

    # --- 竞彩三档 ---
    line = JCLINE[num]
    d = diff + line
    hres = "让胜" if d > 0 else ("让平" if d == 0 else "让负")
    hd = devig(ODDS[num][1])
    hdi = max(range(3), key=lambda i: hd[i])
    hdname = ("让胜", "让平", "让负")[hdi]

    # --- LLM 让球主张：主队让N球 => 主净胜>=N+1；客队让N球 => 客净胜>=N+1 ---
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
        k = int(nv) + 1
        if ht == "主队":
            claim, need = f"主净胜>={k}", (lambda kk: (lambda dd: dd >= kk))(k)
        elif ht == "客队":
            claim, need = f"客净胜>={k}", (lambda kk: (lambda dd: dd <= -kk))(k)
        else:
            claim, need = "?", lambda dd: False
    hh = need(diff)

    t3 = [t["score"] for t in x["top3"]]
    t3h = sc in t3
    t3ok += t3h
    ou_pred = "大" if x["over25"]["over"] >= 0.5 else "小"
    ou_act = "大" if tot >= 3 else "小"
    ouh = ou_pred == ou_act
    ouk += ouh

    # --- λ 与 1x2 概率是否打架 ---
    lh_, la_ = x["lambda"]["home"], x["lambda"]["away"]
    lam_side = "主" if lh_ - la_ > 0.15 else ("客" if la_ - lh_ > 0.15 else "平")
    prob_side = {"home": "主", "draw": "平", "away": "客"}[xp]
    conflicts = (lam_side != "平" and prob_side != "平" and lam_side != prob_side)

    detail.append(dict(num=num, sc=sc, act=act, xh=xh, lh=lh, mh=mh, hh=hh, t3h=t3h, ouh=ouh,
                       tot=tot, diff=diff, line=line, hres=hres, hdname=hdname, hd=hd,
                       ou_pred=ou_pred, ou_act=ou_act, xn=xn, xprob=x["prob"][xp], xp=xp,
                       lw=lw, lp=l["win_pct"], mname=mname, mp=mk[mi], mk=mk,
                       lh_=lh_, la_=la_, conflicts=conflicts, lam_side=lam_side,
                       claim=claim, need_ok=hh))

    flag = " ⚠λ/prob打架" if conflicts else ""
    print(pad(num, 8) + pad(f"{b['basic']['home_name'][:13]} vs {b['basic']['away_name'][:13]}", 30)
          + pad(sc, 6) + pad(act, 5)
          + pad(f"{mname}{mk[mi]:.0%}{'OK' if mh else 'XX'}", 13)
          + pad(f"{xn}{x['prob'][xp]:.0%}{'OK' if xh else 'XX'}", 13)
          + pad(f"{lw}{l['win_pct']}{'OK' if lh else 'XX'}", 12)
          + pad(f"{hres}{'OK' if hres == hdname else ''}", 9)
          + f"{ou_pred}/{ou_act}{'OK' if ouh else 'XX'}{flag}")

n = len(rows)
print()
print(f"■ 胜平负顶选   **市场 {mok}/{n} = {mok/n:.0%}**   XGB {xok}/{n} = {xok/n:.0%}   LLM {lok}/{n} = {lok/n:.0%}")
print(f"■ 让球主张     LLM 按其自报线命中 {sum(d['need_ok'] for d in detail)}/{n} = {sum(d['need_ok'] for d in detail)/n:.0%}")
print(f"■ 竞彩三档     市场热门(让球池)命中 {sum(d['hres']==d['hdname'] for d in detail)}/{n} = {sum(d['hres']==d['hdname'] for d in detail)/n:.0%}")
print(f"■ 比分         XGB top3 含实际 {t3ok}/{n} = {t3ok/n:.0%}")
print(f"■ 大小球       XGB over25 {ouk}/{n} = {ouk/n:.0%}  "
      f"(说大 {sum(1 for d in detail if d['ou_pred']=='大')} 中 {sum(1 for d in detail if d['ou_pred']=='大' and d['ouh'])}"
      f" | 说小 {sum(1 for d in detail if d['ou_pred']=='小')} 中 {sum(1 for d in detail if d['ou_pred']=='小' and d['ouh'])})")

bt = 0
for num, b in rows:
    hg, ag = (int(z) for z in b["result"]["score"].split("-"))
    act = "home" if hg > ag else ("draw" if hg == ag else "away")
    p = b["xgb"]["prob"]
    bt += sum((p[k] - (1.0 if k == act else 0.0)) ** 2 for k in ("home", "draw", "away"))
print(f"■ Brier(XGB 1x2) = {bt/n:.4f}   (无信息基准 0.6667)")

print(f"■ 平局 {sum(1 for d in detail if d['act']=='平局')}/{n}；场均进球 {sum(d['tot'] for d in detail)/n:.2f}；"
      f"实际大球 {sum(1 for d in detail if d['tot']>=3)}/{n}")
print(f"■ 让球三档实际：让胜 {sum(1 for d in detail if d['hres']=='让胜')} / "
      f"让平 {sum(1 for d in detail if d['hres']=='让平')} / 让负 {sum(1 for d in detail if d['hres']=='让负')}")
print(f"■ 让平场次：" + (", ".join(f"{d['num']}({d['sc']})" for d in detail if d['hres'] == '让平') or "无"))

print()
print("■ λ/prob 打架的场次（赛前标记的数据问题是否应验）:")
for d in detail:
    if d["conflicts"]:
        lam = f"λ说{d['lam_side']}胜" if d['lam_side'] != '平' else "λ持平"
        ok = "λ对" if (d['lam_side'] == d['act'][0] and d['xp'] != {'主':'home','平':'draw','客':'away'}[d['lam_side']]) else "prob对"
        print(f"   {d['num']} {d['sc']} 实际{d['act']:<3} | λ {d['lh_']:.2f}/{d['la_']:.2f} → {lam} | "
              f"prob说{d['xn']}({d['xprob']:.0%}) | 市场说{d['mname']} → **{ok}**")

print()
print("■ 市场平局均价 vs XGB 平局均价:")
avg_mk_draw = sum(d["mk"][1] for d in detail) / n
avg_x_draw = sum(d["xp" == 'draw'] if False else 0 for d in detail)
xdraws = []
for num, b in rows:
    xdraws.append(b["xgb"]["prob"]["draw"])
print(f"   市场 {avg_mk_draw:.1%}   XGB {sum(xdraws)/n:.1%}   实际 {sum(1 for d in detail if d['act']=='平局')}/{n} = "
      f"{sum(1 for d in detail if d['act']=='平局')/n:.1%}")
