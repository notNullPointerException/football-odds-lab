# -*- coding: utf-8 -*-
"""把 POST /api/predict/{fixture_id} 的返回排版成模版。

POST 返回是扁平结构（LLM 块字段名是 brief_analysis），与 GET /api/predictions
的分块结构（basic/xgb/llm/result，字段名 brief）不同，所以单独一个渲染器。

用法:
    .venv/bin/python tools/render_post.py <dumped.jsonl>
其中 dumped.jsonl 每行一个 POST 返回的 JSON。
"""
import sys, json

NAMES = {  # fixture_id -> (主队, 客队, 联赛)
    1575494: ("布拉加", "埃斯托里尔", "葡超"),
    1557402: ("利兹联", "纽卡斯尔", "英超"),
    1570382: ("比利亚雷亚尔", "Real Betis", "西甲"),
    1550120: ("国米", "乌迪内斯", "意甲"),
    1552474: ("圣旺红星", "梅斯", "法乙"),
    1629915: ("Al-Ahli Jeddah", "棉农", "亚冠精英联赛"),
    1494274: ("佐加顿斯", "GAIS", "瑞典超"),
    1494756: ("博德闪耀", "桑讷菲尤尔", "挪超"),
    1550118: ("Como", "帕尔马", "意甲"),
    1550125: ("都灵", "罗马", "意甲"),
    1638097: ("Inter Turku", "VPS瓦萨", "芬超"),
}

rows = []
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line.startswith('{"status"'):
        continue
    o = json.loads(line)
    if o.get("status") != "ok":
        print(f"[!] {o.get('fixture_id')} 返回非 ok: {o}")
        continue
    rows.append(o)

print(f"■ POST /api/predict/{{fixture_id}}  共 {len(rows)} 场\n")
for o in rows:
    r = o["result"]
    m = r["llm"]
    fid = o["fixture_id"]
    label = NAMES.get(fid)
    head = f"{label[0]} vs {label[1]}  《{label[2]}》" if label else ""
    print("─" * 78)
    print(f"fixture_id={fid}  {head}")
    print(f"  XGB  {r['model_group']}  主{r['win_home']:.6f} 平{r['win_draw']:.6f} 客{r['win_away']:.6f}")
    print(f"       λ {r['lambda_home']:.6f} / {r['lambda_away']:.6f}     大2.5 {r['over25_prob']:.6f}")
    print(f"       比分前三 " + " / ".join(f"{t['score']}·{t['prob']}" for t in r["top3"]))
    print(f"       让球标签 {r['handicap']}")
    print(f"  LLM  {m['win']} {m['win_pct']}   比分 {m['score']}")
    print(f"       让 {m['handicap_team']} {m['handicap_num']}  {m['handicap_pct']}")
    print(f"       大小 {m['ou_type']} {m['ou_line']}  {m['ou_pct']}")
    print(f"       依据 {m['brief_analysis']}")
    print(f"       数据 {m['core_data']}")
    print(f"       深报 {m['deep_report']}")
print("─" * 78)
