# -*- coding: utf-8 -*-
"""用体彩官方接口的中文队名，回填 teams.name_zh。

数据源：体彩 getMatchCalculatorV1 的 homeTeamAllName / awayTeamAllName（全称，
如「米德尔斯堡」「皇家马德里」），不是 abb 简称（「米堡」「皇马」）—— 表里
用全称更好读。

匹配路径：体彩编号 --jc_map.json--> fixture_id --> fixtures.home_id/away_id --> teams

铁律：**只填空缺（name_zh IS NULL 或空串），绝不覆盖已有值**。回填后
app/api/predictions.py 的 COALESCE(ht.name_zh, p.home_name) 会自动用上，
不用改任何代码。

用法:
    .venv/bin/python tools/fill_team_zh.py            # 预演，只打印不改库
    .venv/bin/python tools/fill_team_zh.py --apply    # 真正写库
"""
import sys, json, pathlib, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))   # backend/ 加进 path 才能 import app

APPLY = "--apply" in sys.argv

SP = ("https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
      "?poolCode=hhad,had&channel=c")
JC2FID = json.loads((pathlib.Path(__file__).parent / "jc_map.json")
                    .read_text(encoding="utf-8"))["map"]

req = urllib.request.Request(SP, headers={"User-Agent": "Mozilla/5.0",
                                          "Referer": "https://www.sporttery.cn/"})
with urllib.request.urlopen(req, timeout=30) as r:
    jd = json.loads(r.read().decode())
jc = {m["matchNumStr"]: m
      for day in jd["value"]["matchInfoList"] for m in day["subMatchList"]}

from app.db.session import SessionLocal          # noqa: E402
from sqlalchemy import text                      # noqa: E402

db = SessionLocal()
rows = []
for num, fid in JC2FID.items():
    m = jc.get(num)
    if not m:
        continue
    f = db.execute(text("""
        SELECT f.id, f.home_id, f.away_id, f.home_name, f.away_name,
               f.league_id, f.league_name
        FROM fixtures f WHERE f.id=:i"""), {"i": fid}).fetchone()
    if not f:
        continue
    pairs = [("主", "teams", f.home_id, f.home_name, m.get("homeTeamAllName")),
             ("客", "teams", f.away_id, f.away_name, m.get("awayTeamAllName")),
             ("联", "leagues", f.league_id, f.league_name, m.get("leagueAbbName"))]
    for side, tbl, tid, en, cn in pairs:
        if not tid or not cn:
            continue
        cur = db.execute(text(f"SELECT name, name_zh FROM {tbl} WHERE id=:i"), {"i": tid}).fetchone()
        if not cur:
            continue
        if (cur.name_zh or "").strip():
            rows.append((num, side, tbl, tid, cur.name, cur.name_zh, cn, "已有，跳过"))
        else:
            rows.append((num, side, tbl, tid, cur.name, cur.name_zh, cn, "待回填"))

todo = [r for r in rows if r[7] == "待回填"]
n_t = sum(1 for r in todo if r[2] == "teams")
print(f"■ 体彩接口 {len(jc)} 场 / jc_map 命中 {len({r[0] for r in rows})} 场")
print(f"  待回填 {len(todo)} 条（队 {n_t} / 联赛 {len(todo) - n_t}），已有中文名跳过 {len(rows) - len(todo)} 条\n")
print(f"  {'编号':<8}{'边':<4}{'表':<9}{'id':<7}{'库里英文名':<24}{'→ 中文名':<16}状态")
print("  " + "-" * 82)
for num, side, tbl, tid, en, zh, cn, st in sorted(
        rows, key=lambda r: (r[0][:2], int(r[0][2:]), r[1])):
    print(f"  {num:<8}{side:<4}{tbl:<9}{tid:<7}{str(en)[:22]:<24}{str(cn)[:12]:<16}{st}")

if not APPLY:
    print(f"\n  预演模式，未写库。要写：加 --apply")
    raise SystemExit

for num, side, tbl, tid, en, zh, cn, st in todo:
    db.execute(text(f"UPDATE {tbl} SET name_zh=:z WHERE id=:i AND (name_zh IS NULL OR name_zh='')"),
               {"z": cn, "i": tid})
db.commit()
print(f"\n  ✓ 已写入 {len(todo)} 条中文名（队 {n_t} / 联赛 {len(todo) - n_t}）")
