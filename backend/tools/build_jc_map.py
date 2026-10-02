# -*- coding: utf-8 -*-
"""重建 tools/jc_map.json —— 体彩编号(周六001...) -> ai-football fixture_id。

只走 HTTP（/api/jc/matches + /api/fixtures），不 import 预测代码、不直连 DB。

匹配算法（与 9-18 那版一致）：
  1. 硬过滤：开球时间差 <= TIME_TOL 秒
  2. 联赛名相等（带 alias 表），若有同联赛候选则只用它们
  3. 中文重合度打分：home/away 各算一次，取最好候选
  4. 平手时要求第一名对第二名有足够领先，否则标 AMBIG 让人看

用法：
  .venv/bin/python tools/build_jc_map.py                # 只看结果，不落盘
  .venv/bin/python tools/build_jc_map.py --write        # 写 tools/jc_map.json
"""
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parent / "jc_map.json"

TIME_TOL = 600          # 开球时间容差（秒）
MIN_SCORE = 0.50        # 队名重合度下限
MARGIN = 0.25           # 第一名需领先第二名的分差

# 体彩简称 -> DB league_name（按需增补）
LEAGUE_ALIAS = {
    "美职": "美职联",
}

# 人工兜底：算法判不出、但人一眼能定的场次。key = "<销售日> <体彩编号>"。
# 命中就【优先采用】并打印 ⚠人工指定 —— 绝不静默生效，也让下次有人看得见
# 这里为什么是手写的。与上面 LEAGUE_ALIAS 同一路数：承认算法有够不着的地方，
# 但把人的判断显式记下来，而不是让它散在脑子/聊天记录里。
#
# 2026-09-24 起这个表挪进了 tools/jc_overrides.json，由 app/services/jc_match_service.py
# 和本工具【共用同一个文件】—— 现场解析和离线重建如果各存一份，迟早对不上。
OVERRIDES_FILE = Path(__file__).resolve().parent / "jc_overrides.json"


def _load_manual() -> dict:
    try:
        raw = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in raw.items() if v}
    except Exception as e:
        print(f"⚠ 读不到 {OVERRIDES_FILE.name}（{e}），本轮没有人工兜底")
        return {}


MANUAL = _load_manual()

CJK = re.compile(r"[一-鿿]")


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_jc(day):
    """取某销售日的全部竞彩场次。"""
    d = http_json(f"{BASE}/api/jc/matches?refresh=1")
    for x in d["days"]:
        if x["business_date"] == day:
            return x["matches"]
    raise SystemExit(f"体彩没有销售日 {day}（现有：{[x['business_date'] for x in d['days']]}）")


def fetch_fixtures(date):
    """全量分页拉某天 fixture（page_size 上限 100，不翻页会静默截断）。"""
    out, page = [], 1
    while True:
        d = http_json(f"{BASE}/api/fixtures?date={date}&page={page}&page_size=100")
        items = d.get("data") or []
        out.extend(items)
        if len(out) >= d.get("total", 0) or not items:
            return out
        page += 1


def cjk_set(s):
    return set(CJK.findall(s or ""))


def side_score(jc_name, db_name):
    """单侧中文重合度：交集 / 较长者长度。None = 这侧无从比较。

    DB 里不少队是拉丁名（Celta Vigo），跟体彩的"塞尔塔"一个汉字都不重合 ——
    那不是"不匹配"，是"没法比"，硬算 0 分会把正确候选压到阈值以下
    （周六023 就是这么被卡住的）。返回 None 让调用方把该侧排除出均值。
    """
    sa, sb = cjk_set(jc_name), cjk_set(db_name)
    if not sb:
        return None
    if not sa:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _n(s):
    """None（无从比对）在排序/比差时当 -1 处理。"""
    return -1.0 if s is None else s


def parse_ts(s):
    s = (s or "").replace("T", " ")[:19]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def main():
    day = sys.argv[sys.argv.index("--date") + 1] if "--date" in sys.argv else "2026-09-19"
    jc = fetch_jc(day)
    print(f"体彩销售日 {day}：{len(jc)} 场\n")

    fixtures = []
    for d in {m["kickoff"][:10] for m in jc if m.get("kickoff")} | {day, }:
        fixtures.extend(fetch_fixtures(d))
    # 去重
    fixtures = list({f["id"]: f for f in fixtures}.values())
    print(f"DB fixture 池：{len(fixtures)} 条\n")

    mapping, problems = {}, []

    for m in jc:
        num = m["match_num"]
        lg = m["league"]
        lg_db = LEAGUE_ALIAS.get(lg, lg)
        kt = parse_ts(m["kickoff"])
        home, away = (m["home"] or "").strip(), (m["away"] or "").strip()

        if kt is None:
            problems.append((num, "体彩无开球时间", ""))
            continue

        # 0) 人工指定优先。放在最前面 —— 人工判断不仅用于「算法判不出」，
        #    也用于「算法判错了」，所以它必须能覆盖后面所有分支。
        manual_fid = MANUAL.get(f"{day} {num}")
        if manual_fid is not None:
            fx = next((f for f in fixtures if f["id"] == manual_fid), None)
            if fx is None:
                # 手写的 id 不在池子里，多半是笔误或那天没同步到 —— 报出来，别用
                problems.append((num, f"人工指定的 {manual_fid} 不在 fixture 池里", None))
                continue
            mapping[num] = manual_fid
            print(f"{num} {home} vs {away}  ->  {manual_fid}  "
                  f"[{fx['league_name']}] {fx['home_name']} vs {fx['away_name']}  ⚠人工指定")
            continue

        # 1) 时间窗
        cands = []
        for f in fixtures:
            ft = parse_ts(f["date"])
            if ft is None:
                continue
            dt = abs((ft - kt).total_seconds())
            if dt <= TIME_TOL:
                cands.append((f, dt))

        if not cands:
            problems.append((num, f"时间窗内无候选 ({m['kickoff']})", ""))
            continue

        # 2) 联赛过滤（有同联赛的才收缩，避免 alias 缺失时全军覆没）
        same_lg = [c for c in cands if c[0].get("league_name") == lg_db]
        pool = same_lg if same_lg else cands

        scored = []
        for f, dt in pool:
            parts = [p for p in (side_score(home, f.get("home_name")),
                                 side_score(away, f.get("away_name"))) if p is not None]
            # 两侧 DB 名都是拉丁名 -> 无从比对，靠"时间窗内唯一"兜底
            s = sum(parts) / len(parts) if parts else None
            scored.append((s, dt, f))
        scored.sort(key=lambda x: (-(x[0] if x[0] is not None else -1), x[1]))

        best = scored[0]
        second = scored[1] if len(scored) > 1 else None
        tag = ""

        if best[0] is None:
            if len(pool) == 1:
                # 时间+联赛已经锁死到唯一一场，标出来即可
                mapping[num] = best[2]["id"]
                fx = best[2]
                print(f"{num} {home} vs {away}  ->  {fx['id']}  "
                      f"[{fx['league_name']}] {fx['home_name']} vs {fx['away_name']}  "
                      f"⚠两侧拉丁名无从比对,时间窗内唯一")
                continue
            problems.append((num, f"两面拉丁名且时间窗有 {len(pool)} 个候选,无法定", best[2]["id"]))
            continue

        if best[0] < MIN_SCORE:
            problems.append((num, f"最佳重合度仅 {best[0]:.2f} < {MIN_SCORE}（窗内 {len(pool)} 候选）",
                             best[2]["id"]))
            continue
        if second and (_n(best[0]) - _n(second[0])) < MARGIN and best[1] == second[1]:
            problems.append(
                (num, f"平手未拉开：{best[0]:.2f} vs {second[0]:.2f}，候选 "
                     f"{best[2]['home_name']} / {second[2]['home_name']}", best[2]["id"]))
            continue

        if not same_lg:
            tag = " ⚠联赛名不匹配(按时间+队名定)"
        if len(pool) == 1:
            tag += " ※唯一候选"

        mapping[num] = best[2]["id"]
        fx = best[2]
        print(f"{num} {home} vs {away}  ->  {fx['id']}  "
              f"[{fx['league_name']}] {fx['home_name']} vs {fx['away_name']}  "
              f"重合{best[0]:.2f} 差{best[1]:.0f}s{tag}")

    print()
    if problems:
        print(f"❌ 未定/存疑 {len(problems)} 场：")
        for num, why, fid in problems:
            print(f"   {num}: {why}" + (f"  (最佳候选 id={fid})" if fid else ""))
    print(f"\n✅ 映射 {len(mapping)}/{len(jc)}")

    if "--write" not in sys.argv:
        return

    # 默认要求零存疑才落盘。--allow-partial 允许写入已定的部分 —— 未定的场次
    # 在 jc_map 里就是【没有这个 key】，jc.py 的 .get() 自然返回 None，页面不显示
    # id，与「这场没关联上」的语义一致，不会误关联到别的场次。
    if problems and "--allow-partial" not in sys.argv:
        print("有存疑场次，未落盘（要写已定的部分请加 --allow-partial）")
        return

    OUT.write_text(json.dumps({
        "date": day,
        "batch": f"{day} 竞彩",
        "map": mapping,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {OUT}")
    if problems:
        print(f"⚠ 部分落盘：{len(problems)} 场未定，未写入 map —— "
              + "、".join(num for num, _, _ in problems))


if __name__ == "__main__":
    main()
