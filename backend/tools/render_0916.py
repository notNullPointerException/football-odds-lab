# -*- coding: utf-8 -*-
"""9-16 周三批次：合并体彩官方玩法 + ai-football 接口三块(basic/xgb/llm) 出分场卡片。
排版全 ASCII / 按 CJK 宽度对齐（见记忆 presentation-flat-ranking-board）。
"""
import json, unicodedata, urllib.request, urllib.parse, pathlib

DATE = "2026-09-16"
SP = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
      "?poolCode=hhad,had&channel=c")
JC2FID = json.loads((pathlib.Path(__file__).parent / "jc_map.json").read_text(encoding="utf-8"))["map"]
FID2JC = {v: k for k, v in JC2FID.items()}


def w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n):
    s = str(s)
    return s + " " * max(0, n - w(s))


def cut(s, n):
    """按显示宽度截断，留余量。"""
    s, out, cur = str(s), "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if cur + cw > n:
            break
        out += c
        cur += cw
    return out


# --- 体彩 ---
req = urllib.request.Request(SP, headers={"User-Agent": "Mozilla/5.0",
                                          "Referer": "https://www.sporttery.cn/"})
with urllib.request.urlopen(req, timeout=30) as r:
    sp = json.loads(r.read().decode())
subs = []
for day in sp["value"]["matchInfoList"]:
    subs.extend(day["subMatchList"])

JC = {}
for m in subs:
    num = m.get("matchNumStr", "")
    if num in FID2JC.values() or num not in JC2FID:
        pass
    JC[num] = m

# --- 接口 ---
q = urllib.parse.urlencode({"date": DATE, "category": "jingzu", "page": 1, "page_size": 50})
with urllib.request.urlopen(f"http://127.0.0.1:8000/api/predictions?{q}", timeout=60) as r:
    api = json.load(r)

blocks = sorted(api["data"], key=lambda x: FID2JC.get(x["basic"]["fixture_id"], "zzz"))

print(f"■ {DATE} / jingzu   接口 {api['total']} 场   (字段全部来自接口返回，未加工)")
print("=" * 96)
for b in blocks:
    fid = b["basic"]["fixture_id"]
    num = FID2JC.get(fid, "?")
    m = JC.get(num, {})
    had, hhad = m.get("had") or {}, m.get("hhad") or {}
    x, l = b["xgb"], b["llm"]
    ko = b["basic"]["match_date"][11:16]
    lg = b["basic"]["league_name"]

    if had and hhad:
        play = "胜平负+让球"
    elif had:
        play = "仅胜平负"
    elif hhad:
        play = "仅让球"
    else:
        play = "未开售"

    print(f"{num}  {pad(cut(b['basic']['home_name'],20),22)} vs "
          f"{pad(cut(b['basic']['away_name'],20),22)}"
          f"{pad(lg,10)} {ko}  [玩法:{play}]".rstrip())
    # 体彩
    if had:
        inv = [1/float(had[k]) for k in "hda"]; s = sum(inv)
        print(f"  体彩 胜平负   主{had['h']:>5} 平{had['d']:>5} 客{had['a']:>5}"
              f"   去水 主{inv[0]/s:.1%} 平{inv[1]/s:.1%} 客{inv[2]/s:.1%}   [算]")
    else:
        print("  体彩 胜平负   —— 未开售")
    if hhad:
        inv = [1/float(hhad[k]) for k in ("h", "d", "a")]; s = sum(inv)
        print(f"  体彩 让球({pad(hhad['goalLine'],3)}) 让胜{hhad['h']:>5} 让平{hhad['d']:>5} 让负{hhad['a']:>5}"
              f"   去水 让胜{inv[0]/s:.1%} 让平{inv[1]/s:.1%} 让负{inv[2]/s:.1%}   [算]")
    else:
        print("  体彩 让球     —— 未开售")
    # xgb
    t3 = " ".join(f"{t['score']} {t['prob']:.1%}" for t in x["top3"])
    print(f"  XGB  {pad(x['model_group'],7)} 主{x['prob']['home']:.1%} 平{x['prob']['draw']:.1%} 客{x['prob']['away']:.1%}"
          f" | 大2.5 {x['over25']['over']:.1%} | λ {x['lambda']['home']:.2f}/{x['lambda']['away']:.2f}"
          f" | 球差档 {x['handicap']}")
    print(f"       比分 {t3}")
    # llm
    print(f"  LLM  {l['win']} {l['win_pct']:>4}  比分 {l['score']}  | 让球 {l['handicap']} ({l['handicap_pct']})"
          f"  | {l['over_under']} ({l['ou_pct']})")
    print(f"       brief: {l['brief']}")
    print("-" * 96)
