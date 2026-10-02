# -*- coding: utf-8 -*-
"""从腿池里筛出合格腿，枚举 2串1 / 3串1，并展开容错结构与敏感性扫描。

筛选规则（全部来自已固化纪律，不新增判断）：
  ① LLM 的 win 方向必须与该腿方向一致      —— 挡掉 xgb 平腿那批虚高 EV
  ② LLM 概率 ≥60%                          —— 串关腿门槛（≤58% 只配单关小注）
  ③ 赔率 ≥1.80                             —— 不吸蚊子血
  ④ 不触发排除线①(高平局价) ②(深盘让负) ③(-1线让负)
  ⑤ 让球腿必须是 LLM 线与竞彩线一致时才纳入（否则没有模型概率）

输出六段：
  [0] lambda 头体检     「谁是热门」二项检验，不过关则本批 e_lam 全部作废
  [1] 近失腿 / 合格腿池 / 2串1 / 3串1        —— 原有行为，未改动
  [1b]换头对照          同样一份闸门，model 头 prob -> lam，看池子的 EV 怎么变（诊断段）
  [2] 三串过关结构对比  3串1 vs 3串3 vs 3串4  —— 含「容错代价」
  [3] 敏感性扫描        p_true = k x 模型 + (1-k) x 市场
  [4] 让平专项          「恰好赢N球」是结构上唯一不靠模型的头，只走市场价 + 经验率

关于 [1b]：
  闸门 ①-⑤ 只判 `model is None`，不看 model 的**数值** -> 换头【必然】不改池成员。
  所以 [1b] 不列「新增/掉出」（恒为 0），只列 EV 翻号的腿 —— 那才是换头唯一的作用：
  让 EV 数字不再骗人。实例（9-20）：019 客胜 prob +25.5% -> lam -6.4%，池子由「1 条」变成
  「0 条真正合格」。prob 头是 102 场实测里唯一 Brier 劣于频率基准的头（见 [1b] 抬头）。

关于 [2][3] 的核心结论（2026-09-20 实测固化）：
  容错是个方差工具，按你的 edge 大小收费。
    腿越水  -> 容错代价越小（3串3 与 3串1 的 EV/单位几乎持平）
    腿越有水 -> 容错代价越大（实测可达 -110pp/单位）
  且容错只挡「恰好挂一条腿」。若几条腿同源（同一个坏头出来的），它们会一起挂，
  那个分支下 3串3 三注全挂、亏 N 倍 —— 容错零保护。

用法:
    .venv/bin/python tools/jc_combos.py 2026-09-14 jingzu
    .venv/bin/python tools/jc_combos.py 2026-09-20 jingzu --top=3 --emp=0.375
    .venv/bin/python tools/jc_combos.py 2026-09-20 jingzu --emp=off   # 关掉第 [4] 段
"""
import sys, re, math, json, pathlib, urllib.request, urllib.parse
from itertools import combinations

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-14"
CATEGORY = sys.argv[2] if len(sys.argv) > 2 else "jingzu"
MIN_ODDS = 1.80
MIN_LLM = 60
TOP_N_TRIPLES = 5           # 第 [2] 段展开几组三腿

# 让平经验头：9-16 + 9-19 两批实测 12/32 = 37.5%。改这里或走 --emp=0.xxx
EMP_DRAW_HITS, EMP_DRAW_N = 12, 32
EMP_DRAW = EMP_DRAW_HITS / EMP_DRAW_N

LEGS_OVERRIDE = None        # --legs=017让平,028让平,030让平  显式指定 [2][3] 段用哪几条腿
for _a in sys.argv[3:]:
    _m = re.match(r"--emp=([\d.]+|off)$", _a)
    if _m:
        EMP_DRAW = None if _m.group(1) == "off" else float(_m.group(1))
    _m = re.match(r"--top=(\d+)$", _a)
    if _m:
        TOP_N_TRIPLES = int(_m.group(1))
    _m = re.match(r"--legs=(.+)$", _a)
    if _m:
        LEGS_OVERRIDE = [s.strip() for s in _m.group(1).split(",") if s.strip()]

SPORTTERY = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
             "?poolCode=hhad,had&channel=c")
AI_BASE = "http://127.0.0.1:8000/api/predictions"
JC2FID = json.loads((pathlib.Path(__file__).parent / "jc_map.json")
                    .read_text(encoding="utf-8"))["map"]


def devig(h, d, a):
    inv = [1.0 / float(x) for x in (h, d, a)]
    s = sum(inv)
    return [x / s for x in inv]


def wilson(k, n, z=1.96):
    """二项比例 Wilson 置信区间。"""
    if not n:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def pois(k, l):
    return math.exp(-l) * l ** k / math.factorial(k)


def lam_1x2(lh, la):
    """由 lambda.home/away 推 1x2 三元组（泊松网格，0..10 截断）。"""
    t = [0.0, 0.0, 0.0]
    for i in range(11):
        for j in range(11):
            p = pois(i, lh) * pois(j, la)
            t[0 if i > j else (1 if i == j else 2)] += p
    s = sum(t)
    return [x / s for x in t]


def lam_hcap(lh, la, line):
    """由 lambda.home/away 推让球盘三元组 [让胜, 让平, 让负]。
    line = 带符号竞彩线（负 = 主队让 N 球），调整后净胜 = (主-客) + line。"""
    t = [0.0, 0.0, 0.0]
    for i in range(11):
        for j in range(11):
            p = pois(i, lh) * pois(j, la)
            d = (i - j) + line
            t[0 if d > 0 else (1 if d == 0 else 2)] += p
    s = sum(t)
    return [x / s for x in t]


def leg_why(play, leg, odds, model, llm_p, llm_win, line, draw_mkt):
    """🔴 闸门 ①-⑤ 的唯一定义处。prob 池和 lam 池共用这一份，严禁出现第二份副本。
    返回空列表 = 该腿过闸进池。"""
    why = []
    if model is None:
        why.append("无模型概率")
    if llm_p < MIN_LLM:
        why.append(f"LLM{llm_p}%<{MIN_LLM}")
    if float(odds) < MIN_ODDS:
        why.append(f"赔{odds}<{MIN_ODDS}(蚊子肉)")
    # 方向一致：line 为带符号竞彩线（负=主队让，正=主队受让）
    if play == "胜平负":
        want = {"主胜": {"主胜"}, "平": {"平局"}, "客胜": {"客胜"}}[leg]
    elif play.startswith("让"):
        g = float(line)
        if leg == "让胜":
            want = {"主胜"} if g < 0 else {"主胜", "平局"}   # 主队让→须赢；主队受让→不输即可
        elif leg == "让负":
            want = {"客胜"} if g > 0 else {"平局", "客胜"}   # 主队受让→须大败；主队让→不赢即可
        else:
            want = None
    else:
        want = None
    if want and llm_win not in want:
        why.append(f"LLM给{llm_win}方向不一致")
    if play == "胜平负" and leg == "主胜" and draw_mkt is not None and draw_mkt >= 0.33:
        why.append("排除线①")
    if play.startswith("让") and leg == "让负":
        g = abs(float(line))
        if g >= 2: why.append("排除线②")
        if g == 1: why.append("排除线③")
    return why


def argmax_tie(v, tol=0.04):
    """返回 (最大项下标, 是否接近平手)。"""
    s = sorted(range(3), key=lambda i: -v[i])
    return s[0], (v[s[0]] - v[s[1]]) < tol


def jc_fetch():
    req = urllib.request.Request(SPORTTERY, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://www.sporttery.cn/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    return {m["matchNumStr"]: m
            for day in d["value"]["matchInfoList"] for m in day["subMatchList"]}


def ai_fetch():
    q = urllib.parse.urlencode({"date": DATE, "category": CATEGORY, "page": 1, "page_size": 50})
    with urllib.request.urlopen(f"{AI_BASE}?{q}", timeout=60) as r:
        return {x["basic"]["fixture_id"]: x for x in json.loads(r.read().decode())["data"]}


# ---------------------------------------------------------------- 过关结构工具
def struct_stats(legs, notes, probs):
    """notes = [腿下标元组, ...]（一注一个元组）。返回 EV/单位、盈利率、全挂率、全挂亏。"""
    odds = [l["odds"] for l in legs]
    stake, ev = len(notes), 0.0
    for note in notes:
        po = pp = 1.0
        for i in note:
            po *= odds[i]
            pp *= probs[i]
        ev += po * pp
    p_profit = p_lose = 0.0
    for mask in range(1 << len(legs)):
        pay = 0.0
        for note in notes:
            if all((mask >> i) & 1 for i in note):
                o = 1.0
                for i in note:
                    o *= odds[i]
                pay += o
        p = 1.0
        for i in range(len(legs)):
            p *= probs[i] if (mask >> i) & 1 else (1 - probs[i])
        if pay > stake:
            p_profit += p
        elif pay == 0:
            p_lose += p
    return dict(stake=stake, evu=ev / stake - 1.0, profit=p_profit,
                lose=p_lose, lose_amt=stake * p_lose)


def triple_structures():
    """3 条腿的三种过关结构。"""
    idx = (0, 1, 2)
    pairs = list(combinations(idx, 2))
    return [("3串1", [idx]),
            ("3串3", pairs),
            ("3串4", pairs + [idx])]


def breakeven_k(legs, notes, models, mkts):
    """EV/单位 = 0 时的 k（p_i = k*model_i + (1-k)*mkt_i）。二分，范围 [-3, 4]。"""
    lo, hi = -3.0, 4.0
    def evu(k):
        pr = [min(max(k * m + (1 - k) * q, 1e-4), 0.9999) for m, q in zip(models, mkts)]
        return struct_stats(legs, notes, pr)["evu"]
    if evu(lo) * evu(hi) > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if evu(lo) * evu(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


jc, ai = jc_fetch(), ai_fetch()
pool, rejected = [], []
all_legs = {}           # key -> row，供 --legs= 显式点腿
draw_legs = []          # [4] 段用：所有「让平」腿，不走模型池
draw_rejected = []      # [1] 段用：让平腿单独计数，不混进「差一道闸」近失表
lam_audit = []          # [0] 段用：lambda 头体检
pool_lam = []           # [1b] 段用：把 model 换成 lam 头后过闸的腿（诊断，非可下注池）
lam_rows = []           # [1b] 段用：所有带 lam 头的腿，用于对照表
for num in sorted(jc, key=lambda s: (s[:2], int(s[2:]))):
    m = jc[num]
    r = ai.get(JC2FID.get(num))
    if not r:
        continue
    x, llm = r["xgb"], r["llm"]
    had, hhad = m.get("had") or {}, m.get("hhad") or {}
    name = f"{m.get('homeTeamAbbName')} vs {m.get('awayTeamAbbName')}"
    llm_p = int(str(llm["win_pct"]).rstrip("%"))
    draw_mkt = devig(had["h"], had["d"], had["a"])[1] if had else None
    _lam = x.get("lambda") or {}
    _lh, _la = _lam.get("home"), _lam.get("away")
    lam_p = lam_1x2(_lh, _la) if (_lh and _la) else None
    if had and lam_p:
        lam_audit.append((num, name, lam_p, devig(had["h"], had["d"], had["a"])))

    def consider(play, leg, odds, mkt, model, line=None, model_lam=None):
        why = leg_why(play, leg, odds, model, llm_p, llm["win"], line, draw_mkt)
        row = dict(num=num, name=name, play=play, leg=leg, odds=float(odds), mkt=mkt,
                   model=model, model_lam=model_lam, llmp=llm_p, line=line)
        why_lam = None
        if model_lam is not None:
            why_lam = leg_why(play, leg, odds, model_lam, llm_p, llm["win"], line, draw_mkt)
            lam_rows.append((row, why_lam))
        all_legs.setdefault(f"{num[-3:]}{play}{leg}", row)
        all_legs.setdefault(f"{num[-3:]}{leg}", row)      # 简写别名，--legs=017让平
        # 让平：池的 EV 用 model 概率算，让平结构上没有 model 头（LLM 那条是 0.5 球盘，
        # 与整数竞彩线永不匹配）。它不是「差一道闸」，是不在本池的 EV 定义里 —— 单独计数走 [4] 段。
        if play.startswith("让") and leg == "让平":
            draw_rejected.append(row)
            return
        if why:
            rejected.append((row, why))
        else:
            pool.append(row)
        if why_lam is not None and not why_lam:
            pool_lam.append(row)

    if had:
        p = devig(had["h"], had["d"], had["a"])
        _L = lam_p or [None, None, None]
        consider("胜平负", "主胜", had["h"], p[0], x["prob"]["home"], model_lam=_L[0])
        consider("胜平负", "平", had["d"], p[1], x["prob"]["draw"], model_lam=_L[1])
        consider("胜平负", "客胜", had["a"], p[2], x["prob"]["away"], model_lam=_L[2])
    if hhad:
        p = devig(hhad["h"], hhad["d"], hhad["a"])
        gi = int(float(hhad.get("goalLine", "0") or 0))
        try:
            lnum = float(llm["handicap_num"])
        except (TypeError, ValueError):
            lnum = None
        lside = "平手" if lnum == 0 else ("主队" if (lnum or 0) < 0 else "客队")
        jside = "主队" if gi < 0 else ("客队" if gi > 0 else "平手")
        same = lnum is not None and lside == jside and abs(abs(lnum) - abs(gi)) < 1e-6
        lp = int(str(llm["handicap_pct"]).rstrip("%")) / 100 if llm.get("handicap_pct") else None
        target = ("让胜" if jside == "主队" else "让负") if same else None
        _cap = lam_hcap(_lh, _la, gi) if lam_p else None
        _cidx = {"让胜": 0, "让平": 1, "让负": 2}
        for lab, odd, mk in [("让胜", hhad["h"], p[0]), ("让平", hhad["d"], p[1]), ("让负", hhad["a"], p[2])]:
            consider(f"让{abs(gi)}球", lab, odd, mk, lp if lab == target else None, line=gi,
                     model_lam=_cap[_cidx[lab]] if _cap else None)
            if lab == "让平" and float(odd) >= MIN_ODDS:
                draw_legs.append(dict(num=num, name=name, odds=float(odd), mkt=mk, line=gi))

# ============================================================ [0] lambda 头体检
if lam_audit:
    print("=" * 96)
    print("== [0] lambda 头体检   「谁是热门」二项检验   口径见 记忆/football-lambda-head-audit.md")
    print("   方法: lambda.home/away 推泊松 1x2，和市场去水比谁最大。平手(前二差<4pp)不计。")
    print("   判读: 一致度 ~95%+ = 在抄市场; 65~75% = 有 skill; ~50% = 纯噪声, 本批 e_lam 全部作废。")
    print("=" * 96)
    print(f"{'编号':<9}{'对阵':<24}{'lam 主/平/客':>20}{'市场 主/平/客':>20}{'判':>5}")
    print("-" * 96)
    _ok = _bad = _tie = 0
    for _num, _name, _L, _MK in lam_audit:
        _li, _lt = argmax_tie(_L)
        _mi, _mt = argmax_tie(_MK)
        if _lt or _mt:
            _v, _tie = "平", _tie + 1
        elif _li == _mi:
            _v, _ok = "对", _ok + 1
        else:
            _v, _bad = "反", _bad + 1
        print(f"{_num:<9}{_name[:22]:<24}"
              f"{_L[0]:>7.1%}{_L[1]:>6.1%}{_L[2]:>7.1%}"
              f"{_MK[0]:>7.1%}{_MK[1]:>6.1%}{_MK[2]:>7.1%}{_v:>5}"
              f"{'  ***' if _v == '反' else ''}")
    _n = _ok + _bad
    _p = sum(math.comb(_n, k) * 0.5 ** _n for k in range(_ok, _n + 1)) if _n else 1.0
    _rate = _ok / _n if _n else 0.0
    print("-" * 96)
    print(f"  判对 {_ok} / 判反 {_bad} / 平手不计 {_tie}     一致度 {_rate:.0%}"
          f"     二项检验 P(X>={_ok} | n={_n}, p=0.5) = {_p:.3f}")
    if not _n:
        print(f"  ?  样本不足，无法判定。")
    elif _rate < 0.60:
        print(f"  *** lam 头未通过体检（一致度 <60%）：本批所有 e_lam 数字作废。")
        print(f"      「模型背离」类腿一律不碰，只认非模型头（让平经验头，见 [4] 段）。")
    elif _rate < 0.70:
        print(f"  !   lam 头勉强（60~70%）：e_lam 只能当参考，不能当主依据。")
    else:
        print(f"  ok  lam 头通过体检（>=70%）：e_lam 可看。")
    print()

near = [(r, w) for r, w in rejected if len(w) == 1]
if near:
    print(f"== 差一道闸的近失腿（{len(near)} 条）—— 只是被砍的原因摊开，不是推荐")
    print(f"{'编号':<9}{'对阵':<24}{'玩法':<8}{'选项':<6}{'赔率':<7}{'市场':<8}{'模型':<8}{'LLM':<6}{'卡在哪':<16}")
    print("-" * 92)
    for r, why in sorted(near, key=lambda t: -t[0]["mkt"]):
        ms = f"{r['model']:.1%}" if r["model"] is not None else "-"
        print(f"{r['num']:<9}{r['name'][:22]:<24}{r['play']:<8}{r['leg']:<6}{r['odds']:<7.2f}"
              f"{r['mkt']:<8.1%}{ms:<8}{r['llmp']:<6}{why[0][:14]:<16}")
    print()

if draw_rejected:
    print(f"== 另有 {len(draw_rejected)} 条「让平」腿：不是「差一道闸」，是结构上不进本池")
    print(f"   池的 EV 用 model 概率算，让平没有 model 头（LLM 那条是 0.5 球盘，与整数竞彩线永不匹配）。")
    print(f"   让平走 [4] 段独立口径（市场价 + 经验率），门槛 ①-⑤ 对它不适用，不是把它砍了。")
    print()

print(f"== 合格腿池  {DATE}/{CATEGORY}   门槛: LLM>={MIN_LLM}% . 赔率>={MIN_ODDS} . 方向一致 . 无排除线命中")
print(f"{'编号':<9}{'对阵':<24}{'玩法':<8}{'选项':<6}{'赔率':<7}{'市场':<8}{'模型':<8}{'LLM':<6}{'倍数':<7}{'EV':<9}")
print("-" * 92)
for L in sorted(pool, key=lambda t: -(t["model"] / t["mkt"])):
    print(f"{L['num']:<9}{L['name'][:22]:<24}{L['play']:<8}{L['leg']:<6}{L['odds']:<7.2f}"
          f"{L['mkt']:<8.1%}{L['model']:<8.1%}{L['llmp']:<6}{L['model']/L['mkt']:<7.2f}"
          f"{L['model']*L['odds']-1:<+9.1%}")
print("-" * 92)

# ============================================================ [1b] 换头对照
if lam_rows:
    print()
    print("=" * 96)
    print("== [1b] 换头对照（诊断段，不是可下注池）   model: prob 头 -> lam 头，闸门 ①-⑤ 一字未改")
    print("   102 场实测(9-10~9-17): prob 头 Brier 0.6790 / 首选 47.1%   劣于频率基准 0.6365 / 45.1%")
    print("                          lam  头 Brier 0.6151 / 首选 52.0%   优于基准")
    print("   两池共用 leg_why() 同一份闸门 —— 差异 100% 来自换头，不含任何门槛改动。")
    print("=" * 96)
    if pool_lam:
        print(f"{'编号':<9}{'对阵':<24}{'玩法':<8}{'选项':<6}{'赔率':<7}{'市场':<8}{'prob':<8}{'lam':<8}{'LLM':<6}{'倍数':<7}{'EV':<9}")
        print("-" * 96)
        for L in sorted(pool_lam, key=lambda t: -(t["model_lam"] / t["mkt"])):
            pm = f"{L['model']:.1%}" if L["model"] is not None else "-"
            print(f"{L['num']:<9}{L['name'][:22]:<24}{L['play']:<8}{L['leg']:<6}{L['odds']:<7.2f}"
                  f"{L['mkt']:<8.1%}{pm:<8}{L['model_lam']:<8.1%}{L['llmp']:<6}"
                  f"{L['model_lam']/L['mkt']:<7.2f}{L['model_lam']*L['odds']-1:<+9.1%}")
    else:
        print("  lam 池为空（0 条过闸）")
    print("-" * 96)
    _k = lambda r: (r["num"], r["play"], r["leg"])
    _kp, _kl = {_k(r) for r in pool}, {_k(r) for r in pool_lam}
    _add = [r for r in pool_lam if _k(r) not in _kp]
    _drop = [r for r in pool if _k(r) not in _kl]
    print(f"  prob 池 {len(pool)} 条  ->  lam 池 {len(pool_lam)} 条")
    print(f"  🔴 闸门只判 model 是否为 None、不看其数值 -> 换头【必然】不改池成员"
          f"（本次新增 {len(_add)} / 掉出 {len(_drop)}）。")
    print(f"     换头唯一的作用是让 EV 数字不再骗人：同样的腿，两个头可能算出不同符号。")
    _flip = []
    for r in pool:
        if r["model_lam"] is None or r["model"] is None:
            continue
        e_p, e_l = r["model"] * r["odds"] - 1, r["model_lam"] * r["odds"] - 1
        if (e_p > 0) != (e_l > 0):
            _flip.append((r, e_p, e_l))
    if _flip:
        print(f"  EV 翻号的腿 {len(_flip)} 条（这才是换头的全部效果）:")
        for r, ep, el in sorted(_flip, key=lambda t: -abs(t[1] - t[2])):
            _tail = "   *** 由正翻负 —— prob 池里的[水]是假的 ***" if el < 0 else "   (由负翻正)"
            print(f"    {r['num']} {r['leg']}@{r['odds']:.2f}   prob {ep:+.1%}  ->  lam {el:+.1%}{_tail}")
    else:
        print(f"  EV 翻号的腿: 无（两个头对今天这批的符号判断一致）")
    _blk = [(r, w) for r, w in lam_rows if w]
    if _blk:
        print(f"\n  lam 头认为最有水、但仍被挡的前 8 条（看是什么在挡 λ 的腿）:")
        print(f"  {'编号':<9}{'对阵':<24}{'玩法':<8}{'选项':<6}{'赔率':<7}{'市场':<8}{'lam':<8}{'倍数':<7}{'卡在哪':<22}")
        print("  " + "-" * 92)
        for r, w in sorted(_blk, key=lambda t: -(t[0]["model_lam"] / t[0]["mkt"]))[:8]:
            print(f"  {r['num']:<9}{r['name'][:22]:<24}{r['play']:<8}{r['leg']:<6}{r['odds']:<7.2f}"
                  f"{r['mkt']:<8.1%}{r['model_lam']:<8.1%}{r['model_lam']/r['mkt']:<7.2f}"
                  f"{'; '.join(w)[:20]:<22}")
    print()

def dump_flat_parlays(rows, k):
    combos = list(combinations(rows, k))
    if not combos:
        return
    print(f"\n=== {k}串1 ({len(combos)} 组) ===")
    print(f"{'组合':<46}{'总赔率':<9}{'市场联合':<10}{'模型联合':<10}{'EV市场':<10}{'EV模型':<10}")
    print("-" * 96)
    for c in combos:
        if len({x["num"] for x in c}) != len(c):
            continue
        odds = math.prod(x["odds"] for x in c)
        pm = math.prod(x["mkt"] for x in c)
        pw = math.prod(x["model"] for x in c)
        label = " x ".join(f"{x['num'][2:]}{x['play']}{x['leg']}@{x['odds']}" for x in c)
        print(f"{label[:44]:<46}{odds:<9.2f}{pm:<10.1%}{pw:<10.1%}"
              f"{pm*odds-1:<+10.1%}{pw*odds-1:<+10.1%}")


if len(pool) < 2:
    print("合格腿不足 2 条，跳过 2串1/3串1（[2][3][4] 段不受影响）。")
else:
    for k in (2, 3):
        dump_flat_parlays(pool, k)

# ---- [2][3] 段的腿来源：--legs= 显式指定 > 合格腿池。
#      让平腿在池里拿不到模型概率（规则⑤），显式点它时改用经验头，这只是给结构分析用，不改闸门。
struct_pool = list(pool)
if LEGS_OVERRIDE:
    struct_pool, _miss = [], []
    for key in LEGS_OVERRIDE:
        row = all_legs.get(key)
        if row is None:
            _miss.append(key)
            continue
        if row["model"] is None and row["leg"] == "让平" and EMP_DRAW:
            row = dict(row, model=EMP_DRAW)
        if row["model"] is None:
            print(f"!! {key} 没有模型概率（让平腿请开 --emp=0.375），本次按市场价代用")
            row = dict(row, model=row["mkt"])
        struct_pool.append(row)
    if _miss:
        print(f"!! --legs= 找不到这些腿（键形如 017让平 / 017让1球让平）: {', '.join(_miss)}")
    print(f"   [2][3] 段腿来源: --legs= 显式指定 {len(struct_pool)} 条")

# ============================================================ [2] 三串过关结构对比
trip = [c for c in combinations(struct_pool, 3) if len({x["num"] for x in c}) == 3]
if trip:
    def trip_ev(c):
        return math.prod(x["model"] * x["odds"] for x in c)
    trip.sort(key=trip_ev, reverse=True)
    print(f"\n{'=' * 96}")
    print(f"== 三串过关结构对比   池内 EV 最高的 {min(TOP_N_TRIPLES, len(trip))} 组三腿 / 共 {len(trip)} 组")
    print(f"   EV/单位 已按注数摊平。盈利率 = 总回收 > 总注数的概率。全挂亏 = 全挂率 x 注数。")
    print(f"{'=' * 96}")
    for c in trip[:TOP_N_TRIPLES]:
        legs = list(c)
        labs = "  x  ".join(f"{x['num'][2:]}{x['play']}{x['leg']}@{x['odds']}" for x in legs)
        print(f"\n  组合: {labs}")
        print(f"  {'结构':<7}{'注数':>5}{'EV/单位(模型)':>14}{'EV/单位(市场)':>14}"
              f"{'盈利率':>9}{'全挂率':>9}{'全挂亏':>8}{'容错代价':>10}")
        base = None
        for sname, notes in triple_structures():
            st_m = struct_stats(legs, notes, [x["model"] for x in legs])
            st_k = struct_stats(legs, notes, [x["mkt"] for x in legs])
            if base is None:
                base = st_m["evu"]
            cost = "-" if sname == "3串1" else f"{st_m['evu'] - base:+.1%}"
            print(f"  {sname:<7}{st_m['stake']:>5}{st_m['evu']:>+14.1%}{st_k['evu']:>+14.1%}"
                  f"{st_m['profit']:>9.1%}{st_m['lose']:>9.1%}{-st_m['lose_amt']:>8.1f}{cost:>10}")

    # ======================================================== [3] 敏感性扫描
    print(f"\n{'=' * 96}")
    print(f"== 敏感性扫描   p_true = k x 模型 + (1-k) x 市场   (k=0 全信市场, k=1 全信模型)")
    print(f"   腿越水 -> 容错代价越小；腿越有水 -> 容错代价越大。看 k 变小的时候谁先翻负。")
    print(f"{'=' * 96}")
    for c in trip[:min(TOP_N_TRIPLES, len(trip))]:
        legs, base = list(c), None
        labs = " x ".join(f"{x['num'][2:]}{x['play']}{x['leg']}" for x in legs)
        models = [x["model"] for x in legs]
        mkts = [x["mkt"] for x in legs]
        best_pair = max(combinations(range(3), 2), key=lambda p: math.prod(
            models[i] * legs[i]["odds"] for i in p))
        rows = [("2串1", [best_pair]), ("3串1", [(0, 1, 2)]),
                ("3串3", list(combinations(range(3), 2))),
                ("3串4", list(combinations(range(3), 2)) + [(0, 1, 2)])]
        print(f"\n  组合: {labs}   (2串1 取 EV 最高那注: "
              f"{' x '.join(legs[i]['num'][2:] + legs[i]['play'] + legs[i]['leg'] for i in best_pair)})")
        print(f"  {'k':>6}" + "".join(f"{nm:>11}" for nm, _ in rows))
        for k in (0.0, 0.25, 0.5, 0.75, 1.0, 1.25):
            pr = [min(max(k * m + (1 - k) * q, 1e-4), 0.9999) for m, q in zip(models, mkts)]
            print(f"  {k:>6.2f}" + "".join(
                f"{struct_stats(legs, nt, pr)['evu']:>+11.1%}" for _, nt in rows))
        # 混合*：单一 k 表达不了「1x2 腿不可信、让平腿可信」这种分头假设，单列一行。
        # 组合里没有让平腿时它恒等于 k=0，没有信息量，跳过。
        if any(l["leg"] == "让平" for l in legs):
            pr_mix = [m if l["leg"] == "让平" else q for m, q, l in zip(models, mkts, legs)]
            print(f"  {'混合*':>6}" + "".join(
                f"{struct_stats(legs, nt, pr_mix)['evu']:>+11.1%}" for _, nt in rows))
        be = []
        for nm, nt in rows:
            kx = breakeven_k(legs, nt, models, mkts)
            be.append(f"{nm} {kx:.2f}" if kx is not None else f"{nm} -")
        print(f"  平衡点 k* (EV/单位=0): " + "   ".join(be))
        if any(l["leg"] == "让平" for l in legs):
            print(f"  混合* = 让平腿走模型(经验)头、其余腿全走市场价 —— 即「模型头无 skill、只认统计头」的场景。")

# ============================================================ [4] 让平专项
if EMP_DRAW and len(draw_legs) >= 2:
    e = EMP_DRAW
    lo, hi = wilson(EMP_DRAW_HITS, EMP_DRAW_N)
    print(f"\n{'=' * 96}")
    print(f"== 让平专项   「恰好赢 N 球」是结构上唯一不靠模型的头，只看 市场价 + 经验率")
    print(f"   经验头 = {EMP_DRAW_HITS}/{EMP_DRAW_N} = {e:.1%}   Wilson 95% [{lo:.1%}, {hi:.1%}]")
    print(f"   注意「让平」同时杀死让胜与让负，是整数线上独有的那个比分格；本段不引用 lambda/prob 头。")
    print(f"{'=' * 96}")
    print(f"  {'编号':<9}{'对阵':<24}{'线':>4}{'赔率':>7}{'市场':>8}{'EV@经验':>10}{'赔率隐含':>10}")
    print("  " + "-" * 90)
    for L in sorted(draw_legs, key=lambda t: t["odds"], reverse=True):
        print(f"  {L['num']:<9}{L['name'][:22]:<24}{L['line']:>4}{L['odds']:>7.2f}"
              f"{L['mkt']:>8.1%}{e * L['odds'] - 1:>+10.1%}{1 / L['odds']:>10.1%}")
    _mkt_mean = sum(x["mkt"] for x in draw_legs) / len(draw_legs)
    _imp_mean = sum(1 / x["odds"] for x in draw_legs) / len(draw_legs)
    print(f"  注：「市场」列 = 「赔率隐含」列 x 0.885（竞彩返奖率恒定），两列不是两条独立证据。")
    print(f"  赔率隐含均值 {_imp_mean:.1%}   <- EV 的真实平衡点（你付的是原赔率里的抽水，不是去水价）")
    print(f"  经验头 {e:.1%} / {_imp_mean:.1%} = {e / _imp_mean:.2f}x  ->  {e / _imp_mean - 1:+.1%}")
    print(f"  参考：{e:.1%} / 去水市场 {_mkt_mean:.1%} = {e / _mkt_mean:.2f}x"
          f"（{e / _mkt_mean - 1:+.1%}）—— 这是「比市场准」的说法，不等于能赚同样多")

    pairs = sorted(combinations(draw_legs, 2), key=lambda c: -math.prod(x["odds"] for x in c))
    trips = sorted(combinations(draw_legs, 3), key=lambda c: -math.prod(x["odds"] for x in c))
    print(f"\n  让平二串一 EV 降序 前5     EV = 经验率^2 x 串赔 - 1")
    print(f"  {'串赔':>8}{'EV':>10}   腿")
    for c in pairs[:5]:
        o = math.prod(x["odds"] for x in c)
        print(f"  {o:>8.2f}{e * e * o - 1:>+10.1%}   "
              + " x ".join(f"{x['num']}{x['name'][:10]}让平@{x['odds']}" for x in c))
    print(f"\n  让平三串一 EV 降序 前3")
    print(f"  {'串赔':>8}{'EV':>10}   腿")
    for c in trips[:3]:
        o = math.prod(x["odds"] for x in c)
        print(f"  {o:>8.2f}{e ** 3 * o - 1:>+10.1%}   "
              + " x ".join(f"{x['num']}{x['name'][:10]}让平@{x['odds']}" for x in c))

    print(f"\n  敏感性：让平真实率 r 换掉之后（只用最高 EV 的二串一和三串一）")
    p1, p3 = pairs[0], trips[0]
    o1, o3 = math.prod(x["odds"] for x in p1), math.prod(x["odds"] for x in p3)
    print(f"  {'r':>8}{'二串一@%.2f' % o1:>16}{'三串一@%.2f' % o3:>16}   备注")
    for r in (lo, (1 / o1) ** 0.5, (1 / o3) ** (1 / 3), 0.30, e, 0.45, hi):
        note = ""
        if abs(r - lo) < 1e-9: note = "<- Wilson 下界"
        elif abs(r - (1 / o1) ** 0.5) < 1e-9: note = "<- 二串一平衡点"
        elif abs(r - (1 / o3) ** (1 / 3)) < 1e-9: note = "<- 三串一平衡点"
        elif abs(r - e) < 1e-9: note = "<- 经验头"
        elif abs(r - hi) < 1e-9: note = "<- Wilson 上界"
        print(f"  {r:>8.1%}{r * r * o1 - 1:>+16.1%}{r ** 3 * o3 - 1:>+16.1%}   {note}")
    print(f"  平衡点：二串一需 r > {math.sqrt(1 / o1):.1%}   三串一需 r > {(1 / o3) ** (1 / 3):.1%}")
    print(f"  含义：二串一是「赌让平真实率 > 市场价」，不需要模型对；腿数越多，同一个假设被乘方放大，")
    print(f"        所以 r 的估计误差在长串上伤害更大。")
