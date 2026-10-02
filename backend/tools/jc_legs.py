# -*- coding: utf-8 -*-
"""把体彩盘口 + ai-football 模型拆成"腿"，逐腿算 市场去水 / 模型概率 / 倍数 / EV。

模型概率来源（只用接口给的，不自己造）：
  胜平负 主胜/平/客胜  → xgb.prob.{home,draw,away}
  让球   让胜          → llm.handicap_pct（口径=让球方赢盘概率），
                         且仅当 llm 的线与竞彩 hhad.goalLine 一致时才可用
  让球   让平/让负     → 接口没有模型概率，留空

用法:
    .venv/bin/python tools/jc_legs.py 2026-09-14 jingzu
"""
import sys, json, pathlib, urllib.request, urllib.parse

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-14"
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "jingzu"

SPORTTERY = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
             "?poolCode=hhad,had&channel=c")
AI_BASE = "http://127.0.0.1:8000/api/predictions"
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
        return {x["basic"]["fixture_id"]: x for x in json.loads(r.read().decode())["data"]}


jc, ai = jc_fetch(), ai_fetch()
legs = []
for num in sorted(jc, key=lambda s: (s[:2], int(s[2:]))):
    m = jc[num]
    fid = JC2FID.get(num)
    if fid is None or fid not in ai:
        continue
    r = ai[fid]
    x, llm = r["xgb"], r["llm"]
    had, hhad = m.get("had") or {}, m.get("hhad") or {}
    name = f"{m.get('homeTeamAbbName')} vs {m.get('awayTeamAbbName')}"
    llm_pct = int(str(llm["handicap_pct"]).rstrip("%")) / 100 if llm.get("handicap_pct") else None

    if had:
        p, _ = devig(had["h"], had["d"], had["a"])
        for lab, odd, mp, mk in [("主胜", had["h"], x["prob"]["home"], p[0]),
                                 ("平", had["d"], x["prob"]["draw"], p[1]),
                                 ("客胜", had["a"], x["prob"]["away"], p[2])]:
            legs.append(dict(num=num, name=name, play="胜平负", leg=lab, odds=float(odd),
                             mkt=mk, model=mp, src="xgb", llm=llm["win"], llmp=llm["win_pct"],
                             draw_mkt=p[1]))
    if hhad:
        p, _ = devig(hhad["h"], hhad["d"], hhad["a"])
        gi = int(float(hhad.get("goalLine", "0") or 0))
        line = f"{abs(gi)}"
        # 竞彩线归一化成"谁让球"：goalLine 负=主队让，正=主队受让(=客队让)
        jc_side = "主队" if gi < 0 else ("客队" if gi > 0 else "平手")
        jc_line = abs(gi)
        # LLM 口径：handicap_num 负=主队让，正=客队让；handicap_pct=让球方赢盘概率
        try:
            lnum = float(llm["handicap_num"])
        except (TypeError, ValueError):
            lnum = None
        llm_side = "平手" if lnum == 0 else ("主队" if (lnum or 0) < 0 else "客队")
        same = (lnum is not None and llm_side == jc_side and abs(abs(lnum) - jc_line) < 1e-6)
        # 让球方=主队 → LLM 的 pct 对应竞彩「让胜」；让球方=客队 → 对应竞彩「让负」
        target = None if not same else ("让胜" if jc_side == "主队" else "让负")
        for lab, odd, mk in [("让胜", hhad["h"], p[0]), ("让平", hhad["d"], p[1]), ("让负", hhad["a"], p[2])]:
            model = llm_pct if lab == target else None
            legs.append(dict(num=num, name=name, play=f"让{line}球", leg=lab, odds=float(odd),
                             mkt=mk, model=model,
                             src=f"llm{target}" if model is not None else "—",
                             llm=llm["win"], llmp=llm["win_pct"], draw_mkt=p[1]))

print(f"■ 腿表  {DATE} / {CATEGORY}    模型概率来源: xgb.prob(胜平负) / llm.handicap_pct(让胜,需同线)")
print(f"{'编号':<9}{'对阵':<24}{'玩法':<8}{'选项':<6}{'赔率':<7}{'市场':<8}{'模型':<8}{'来源':<9}{'倍数':<7}{'EV':<9}{'LLM':<12}标记")
print("─" * 132)
for L in sorted([l for l in legs if l["model"] is not None],
                key=lambda t: -(t["model"] / t["mkt"])):
    mult = L["model"] / L["mkt"]
    ev = L["model"] * L["odds"] - 1
    tags = []
    if L["play"] == "胜平负" and L["draw_mkt"] >= 0.33 and L["leg"] == "主胜":
        tags.append("排除线①高平局价")
    if L["play"].startswith("让") and L["leg"] == "让负":
        gi = float(L["play"][1:-1])
        if gi >= 2: tags.append("排除线②深盘让负")
        if gi == 1: tags.append("排除线③-1让负")
    if int(str(L["llmp"]).rstrip("%")) < 60:
        tags.append("LLM<60%")
    print(f"{L['num']:<9}{L['name'][:22]:<24}{L['play']:<8}{L['leg']:<6}{L['odds']:<7.2f}"
          f"{L['mkt']:<8.1%}{L['model']:<8.1%}{L['src']:<9}{mult:<7.2f}{ev:<+9.1%}"
          f"{(L['llm']+' '+L['llmp']):<12}{' '.join(tags)}")
print("─" * 132)
print(f"共 {len(legs)} 条腿，其中 {sum(1 for l in legs if l['model'] is not None)} 条有模型概率可算 EV")
