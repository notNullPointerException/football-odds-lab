# -*- coding: utf-8 -*-
"""一场一块：胜平负 / 让球，按概率降序，带横条。

横条和分隔线全部用 ASCII（# - =）—— 中文终端里 U+2588/U+2550 这类
"东亚模糊宽度"字符会被当双宽渲染，把排版撑歪，所以一律不用。

排版按终端宽度自动选：
  宽屏(>=108列)  一场 3 行：标题 + 胜平负 + 让球
  窄屏(默认)     一场 7 行：标题 + 每个选项一行

用法:
    .venv/bin/python tools/prob_line.py 2026-09-15 jingzu
    .venv/bin/python tools/prob_line.py 2026-09-15 jingzu wide|tall   # 强制排版
    .venv/bin/python tools/prob_line.py 2026-09-15 jingzu num|prob    # 方块顺序
"""
import sys, json, pathlib, shutil, unicodedata, urllib.request, urllib.parse

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-15"
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "jingzu"
MODE = sys.argv[3] if len(sys.argv) > 3 else "auto"      # auto | wide | tall
ORDER = sys.argv[4] if len(sys.argv) > 4 else "num"      # num | prob

SP = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
      "?poolCode=hhad,had&channel=c")
JC2FID = json.loads((pathlib.Path(__file__).parent / "jc_map.json")
                    .read_text(encoding="utf-8"))["map"]

BAR_W = 16       # 横条格数
BAR_FULL = 16    # 满格 = 100%
CELLW = 43       # 宽屏模式单个选项格


def w(s):
    """显示宽度。只对 CJK 宽字符计 2，ASCII 计 1。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n):
    s = str(s)
    return s + " " * max(0, n - w(s))


def cut(s, n):
    """按显示宽度截断到不超过 n 列，避免长队名把后面的列顶歪。"""
    out, wd = "", 0
    for c in str(s):
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if wd + cw > n:
            break
        out += c
        wd += cw
    return out


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


def bar(mkt):
    n = max(1, round(mkt * BAR_FULL)) if mkt > 0 else 0
    return "#" * n


def star_of(mkt, model, odd):
    return "*" if (model is not None and model / mkt >= 1.2 and odd >= 1.80) else ""


def pct_of(mkt, model):
    return f"{mkt:.1%}/-" if model is None else f"{mkt:.1%}/{model:.1%}"


def cell(lab, odd, mkt, model, play):
    tag = "胜" if play == "胜平负" else "让"
    s = (f"{tag}{pad(lab, 4)} {odd:>5.2f} {pad(bar(mkt), BAR_W)} "
         f"{pad(pct_of(mkt, model), 12)}{star_of(mkt, model, odd)}")
    return pad(s, CELLW)


blocks = []
for num in sorted(jc, key=lambda s: (s[:2], int(s[2:]))):
    fid = JC2FID.get(num)
    if fid not in ai:
        continue
    r = ai[fid]
    x, l, b = r["xgb"], r["llm"], r["basic"]
    had, hhad = jc[num].get("had") or {}, jc[num].get("hhad") or {}
    nm = f"{b['home_name'][:14]} vs {b['away_name'][:14]}"

    sfb, rq = [], []
    if had:
        p = devig(had["h"], had["d"], had["a"])
        for lab, odd, mp in [("主胜", had["h"], x["prob"]["home"]),
                             ("平", had["d"], x["prob"]["draw"]),
                             ("客胜", had["a"], x["prob"]["away"])]:
            sfb.append((lab, float(odd), p.pop(0), mp))
        sfb.sort(key=lambda t: -t[2])

    line, gi = "", None
    if hhad:
        p = devig(hhad["h"], hhad["d"], hhad["a"])
        gi = int(float(hhad.get("goalLine", "0") or 0))
        line = f"主队让{abs(gi)}球" if gi < 0 else (f"主队受让{abs(gi)}球" if gi > 0 else "平手")
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
            rq.append((lab, float(odd), p.pop(0), lp if lab == tgt else None))
        rq.sort(key=lambda t: -t[2])

    # 胜平负 + 让球 合成一条，按市场% 整体降序
    opts = [(lab, odd, mkt, model, "胜平负") for lab, odd, mkt, model in sfb]
    opts += [(lab, odd, mkt, model, "让球") for lab, odd, mkt, model in rq]
    opts.sort(key=lambda t: -t[2])
    top = opts[0][2] if opts else 0.0
    blocks.append(dict(num=num, nm=nm, line=line, gi=gi, opts=opts, top=top))

blocks.sort(key=(lambda k: (k["num"][:2], int(k["num"][2:]))) if ORDER == "num"
            else (lambda k: -k["top"]))

if MODE == "auto":
    MODE = "flat"

print(f"■ {DATE} / {CATEGORY}   本批 {len(blocks)} 场   (排版={MODE})")
print(f"  市场% = 体彩赔率去水[算]   模型% = 接口给的(xgb/llm)，没有写 -    * = 模型>=1.2x市场 且 赔率>=1.80")
print()

if MODE == "flat":
    allopts = [(k["num"], k["nm"], k["gi"], lab, odd, mkt, model, play)
               for k in blocks for lab, odd, mkt, model, play in k["opts"]]
    allopts.sort(key=lambda t: -t[5])
    print(f"  全批 {len(allopts)} 个选项，按「市场%（去水）」从高到低总榜 —— 从第 1 名往下挑")
    print()
    print(f"  {'#':>3}  {pad('编号', 8)}{pad('对阵', 24)}{pad('玩法', 7)}{pad('选项', 5)}"
          f"{pad('赔率', 6)}{pad('概率', 11)}{pad('市场%/模型%', 12)}")
    print("  " + "-" * 82)
    for i, (num, nm, gi, lab, odd, mkt, model, play) in enumerate(allopts, 1):
        tag = "胜平负" if play == "胜平负" else f"让{gi:+d}"
        print((f"  {i:>3}  {pad(num, 8)}{pad(cut(nm, 22), 24)}{pad(tag, 7)}{pad(lab, 5)}"
               f"{pad(f'{odd:.2f}', 6)}{pad(bar(mkt), 11)}"
               f"{pad(pct_of(mkt, model), 12)}{star_of(mkt, model, odd)}").rstrip())
    print()
    raise SystemExit

LINE = "=" * 84
for k in blocks:
    print(LINE)
    print(f" {k['num']}  {k['nm']}" + (f"      [{k['line']}]" if k["line"] else "      [未开让球]"))
    opts = k["opts"]
    if not opts:
        print("  两个玩法都没开")
    elif MODE == "wide":
        for i in range(0, len(opts), 3):
            print(("  " + "".join(cell(*t) for t in opts[i:i + 3])).rstrip())
    else:
        for lab, odd, mkt, model, play in opts:
            print((f"  {pad(lab, 4)} {odd:>5.2f}  {pad(bar(mkt), BAR_W)}  "
                   f"{pad(pct_of(mkt, model), 12)}{pad(star_of(mkt, model, odd), 3)}"
                   f"{play}").rstrip())
    print()
print(LINE)
