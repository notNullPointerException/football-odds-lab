# -*- coding: utf-8 -*-
"""胜负彩当期 14 场：市场盘口 + 三个模型头，平铺成总榜。

只读两个 HTTP 接口，不直连 DB、不算模型、不推断：
    GET /api/sfc/matches        当期 14 场 + 体彩胜平负赔率（没开就是 None）
    GET /api/predictions        预测结果（按 match_date 分两天取，接口的日期是 10:10~次日10:10）

用法：
    .venv/bin/python tools/sfc_board.py            # 总榜
    .venv/bin/python tools/sfc_board.py -v         # 另附逐场明细
"""
import json
import sys
import unicodedata
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api"


# ---------- 排版：按【显示宽度】补齐，别用 {'中文':<20}（那是字符数） ----------
def _w(s) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n, al="l") -> str:
    s = str(s)
    g = max(0, n - _w(s))
    return (" " * g + s) if al == "r" else (s + " " * g)


def name(s: str, n: int) -> str:
    """截断到显示宽度 n（超出补 '..'）。"""
    s = str(s)
    if _w(s) <= n:
        return s
    out = ""
    for c in s:
        if _w(out + c) > n - 2:
            break
        out += c
    return out + ".."


def _get(path: str, **q):
    url = f"{BASE}/{path}"
    if q:
        url += "?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


# ---------- λ -> 泊松 1x2（只比 λh>λa 会丢平局，算得虚低） ----------
def lam_1x2(lh, la, kmax=15):
    import math
    ph = [math.exp(-lh) * lh ** i / math.factorial(i) for i in range(kmax)]
    pa = [math.exp(-la) * la ** j / math.factorial(j) for j in range(kmax)]
    H = sum(ph[i] * pa[j] for i in range(kmax) for j in range(kmax) if i > j)
    D = sum(ph[i] * pa[i] for i in range(kmax))
    A = sum(ph[i] * pa[j] for i in range(kmax) for j in range(kmax) if i < j)
    return H, D, A


def top(p):
    """返回 (最热项, 概率)。p = (主, 平, 客)"""
    i = max(range(3), key=lambda k: p[k])
    return ("主胜", "平局", "客胜")[i], p[i]


def pct(v) -> str:
    """llm_win_pct 库里存的就是带 % 的字符串（"58%"），别再加一个。"""
    s = str(v if v is not None else "-")
    return s if s.endswith("%") else s + "%"


def main():
    sfc = _get("sfc/matches", refresh="true")
    matches = sfc["matches"]

    # 接口的 date 是 10:10~次日 10:10 的窗口，9-29 00:00 那批落在 9-28 里
    pred = {}
    for d in ("2026-09-28", "2026-09-29", "2026-09-30"):
        for r in _get("predictions", date=d, page=1, page_size=100)["data"]:
            pred[r["basic"]["fixture_id"]] = r

    print(f"■ 胜负彩 第 {sfc.get('issue')} 期   {sfc.get('total_count')} 场   "
          f"停售 {sfc.get('sale_end')}   开奖 {sfc.get('draw_time')}")
    print(f"  体彩已开赔率 {sum(1 for m in matches if m['had'])}/{len(matches)}   "
          f"已出预测 {sum(1 for m in matches if m['fixture_id'] and m['fixture_id'] in pred)}/{len(matches)}")
    print()

    H1 = ("no", 3, "r"), ("比赛", 30, "l"), ("日期", 11, "l"), ("市场基准", 10, "l"), \
         ("赔率 主/平/客", 17, "l"), ("去水概率 主/平/客", 21, "l"), \
         ("市场", 9, "l"), ("prob头", 10, "l"), ("lam头", 12, "l"), ("LLM头", 13, "l"), ("fixture_id", 10, "l")

    def head(cells):
        return " ".join(pad(c[0], c[1], c[2]) for c in cells)

    def row(cells):
        return " ".join(pad(c[0], c[1], c[2]) for c in cells)

    print(head(H1))
    print("-" * 138)

    tally = {"market": 0, "prob": 0, "lam": 0, "llm": 0}
    n_open = n_pred = 0
    miss = []
    audit = []

    for m in matches:
        fid = m["fixture_id"]
        r = pred.get(fid)
        no = str(m["no"])
        mn = f"{name(m['home'], 13)} vs {name(m['away'], 13)}"

        # ---- 市场：优先体彩胜平负（能下注的价），没有就退到庄家盘 ----
        src, odds, p = None, None, None
        if m["had"]:
            src = "体彩"
            odds = f"{m['had']['h']:.2f}/{m['had']['d']:.2f}/{m['had']['a']:.2f}"
            p = (m["had"]["p_h"], m["had"]["p_d"], m["had"]["p_a"])
        elif r and r.get("market") and r["market"].get("odds", {}).get("home") is not None:
            src = r["market"].get("bookmaker") or "庄家"
            o = r["market"]["odds"]
            odds = f"{o['home']:.2f}/{o['draw']:.2f}/{o['away']:.2f}"
            dv = r["market"].get("devig")
            if dv:
                p = tuple(dv)
                src = "Pinnacle"
        if src:
            n_open += 1

        if not r:
            mid = miss.append(no) or ["--"] * 5
            print(row([(no, 3, "r"), (mn, 30, "l"), (m.get("match_date") or "", 11, "l"),
                       (src or "未开", 10, "l"), (odds or "--", 17, "l"),
                       (f"{p[0]*100:.1f}/{p[1]*100:.1f}/{p[2]*100:.1f}%" if p else "--", 21, "l"),
                       ("无预测", 9, "l"), ("-", 10, "l"), ("-", 12, "l"), ("-", 13, "l"),
                       (str(fid or "-"), 10, "l")]))
            continue
        n_pred += 1

        mk, mkp = top(p) if p else (None, None)
        if mk:
            tally["market"] += 1

        x = r["xgb"]
        pp = (x["prob"]["home"], x["prob"]["draw"], x["prob"]["away"])
        pk, pkp = top(pp)
        tally["prob"] += 1

        if x.get("lambda"):
            lh, la = x["lambda"]["home"], x["lambda"]["away"]
            lk, lkp = top(lam_1x2(lh, la))
            lam_s = f"{lk} {lkp*100:.0f}%"
        else:
            lk, lam_s = None, "-"
        tally["lam"] += 1

        ll = r["llm"].get("win")
        llp = pct(r["llm"].get("win_pct"))
        if ll:
            tally["llm"] += 1

        audit.append({
            "no": no, "name": mn,
            "mkt_d": p[1] if p else None,
            "prob_d": pp[1],
            "lam_d": lam_1x2(x["lambda"]["home"], x["lambda"]["away"])[1] if x.get("lambda") else None,
            "lam_tot": (x["lambda"]["home"] + x["lambda"]["away"]) if x.get("lambda") else None,
            "pick": {"market": mk, "prob": pk, "lam": lk, "llm": ll},
        })

        def star(k):
            return k + "*" if (mk and k and k != mk) else k

        print(row([(no, 3, "r"), (mn, 30, "l"), (m.get("match_date") or "", 11, "l"),
                   (src or "未开", 10, "l"), (odds or "--", 17, "l"),
                   (f"{p[0]*100:.1f}/{p[1]*100:.1f}/{p[2]*100:.1f}%" if p else "--", 21, "l"),
                   (star(mk) if mk else "--", 9, "l"),
                   (f"{star(pk)} {pkp*100:.0f}%", 10, "l"),
                   (star(lam_s), 12, "l"),
                   (f"{star(ll)} {llp}" if ll else "-", 13, "l"),
                   (str(fid or "-"), 10, "l")]))

    print("-" * 138)
    print(f"  开赔率 {n_open}/{len(matches)}   已预测 {n_pred}/{len(matches)}"
          + (f"   未预测: {' '.join(miss)}" if miss else ""))
    print("  * = 该头最热项与市场最热项不一致")

    # ---- 体检：平局概率 vs 总进球 ----
    print("\n■ 体检")
    md = [a["mkt_d"] for a in audit if a["mkt_d"] is not None]
    pd_ = [a["prob_d"] for a in audit]
    ld = [a["lam_d"] for a in audit if a["lam_d"] is not None]
    avg = lambda v: (sum(v) / len(v) * 100) if v else 0
    print(f"  平局概率均值   市场({len(md)}场) {avg(md):.1f}%   prob头({len(pd_)}场) {avg(pd_):.1f}%"
          f"   lam泊松({len(ld)}场) {avg(ld):.1f}%")
    print(f"  14 场期望平局数  市场 {(len(matches)/max(len(md),1))*sum(md):.1f} 场"
          f"   prob头 {sum(pd_):.1f} 场   lam泊松 {sum(ld):.1f} 场")
    nd = sum(1 for a in audit if "平局" in [a["pick"]["market"], a["pick"]["prob"], a["pick"]["lam"], a["pick"]["llm"]])
    print(f"  有任何一个头把平局选为最热的场次: {nd}/{len(audit)}")

    bad = [a for a in audit if a["lam_tot"] and a["lam_tot"] > 6.0]
    print(f"\n  lam 总进球异常(>6.0) 的场次 {len(bad)}/{len(audit)}:")
    for a in bad:
        print(f"    #{a['no']:>2} {a['name']}  lam 总进球 {a['lam_tot']:.2f}")
    if bad:
        print("    (正常 ~2.5-3.5；这是 GLOBAL 国家队场次的已知问题，不是新 bug)")

    if "-v" in sys.argv:
        print("\n" + "=" * 138)
        print("逐场明细（接口原样，不加工）")
        for m in matches:
            r = pred.get(m["fixture_id"])
            if not r:
                continue
            b, x, ll = r["basic"], r["xgb"], r["llm"]
            print(f"\n[{m['no']}] {b['home_name']} vs {b['away_name']}  《{b['league_name']}》  {b['match_date']}  #{b['fixture_id']}")
            print(f"      prob 主{x['prob']['home']:.3f} 平{x['prob']['draw']:.3f} 客{x['prob']['away']:.3f}"
                  f"   |  lam {x['lambda']['home']:.2f}/{x['lambda']['away']:.2f}"
                  f"   |  大2.5 {x['over25']['over']:.3f}" if x.get("lambda") and x.get("over25") else
                  f"      prob 主{x['prob']['home']:.3f} 平{x['prob']['draw']:.3f} 客{x['prob']['away']:.3f}")
            print(f"      比分前三 " + "/".join(f"{t['score']}·{t['prob']:.3f}" for t in (x.get("top3") or [])))
            print(f"      LLM {ll.get('win')} @{ll.get('win_pct')}  比分 {ll.get('score')}  让球 {ll.get('handicap')}")
            mk = r.get("market")
            if mk:
                print(f"      盘 {mk.get('bookmaker')} {mk.get('odds')} devig={mk.get('devig')}")
                if mk.get("jc"):
                    print(f"      体彩 jc had={mk['jc'].get('had')}")


if __name__ == "__main__":
    main()
