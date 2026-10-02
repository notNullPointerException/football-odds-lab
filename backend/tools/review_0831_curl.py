# -*- coding: utf-8 -*-
"""复盘周日批次 全curl版: 快速拉结果 + 对错对比"""
import sys, os, json, subprocess, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
from app.db.session import SessionLocal
from sqlalchemy import text

API_KEY = os.environ["API_FOOTBALL_KEY"]
BASE = os.environ["API_FOOTBALL_BASE_URL"]

FIXTURE_IDS = [1552151, 1494747, 1570360, 1557384, 1575141,
               1550105, 1570356, 1552152, 1550102, 1550099,
               1552742, 1492354, 1570355, 1575479, 1490435]

def api_get_curl(path, params):
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-m", "15", "-H",
                          f"x-apisports-key: {API_KEY}", url],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout)
    return None

db = SessionLocal()
results = {}
try:
    for fid in FIXTURE_IDS:
        data = api_get_curl("/fixtures", {"id": fid})
        if not data or not data.get("response"):
            print(f"[!] {fid} 拉取失败", flush=True)
            continue
        it = data["response"][0]
        goals = it["score"]["fulltime"]
        hg, ag = goals["home"], goals["away"]
        if hg is None:
            print(f"[未开赛] {fid} {it['teams']['home']['name']} vs {it['teams']['away']['name']}", flush=True)
            continue
        db.execute(text("""
            UPDATE predictions SET actual_home_goals=:hg, actual_away_goals=:ag WHERE fixture_id=:fid
        """), {"hg": hg, "ag": ag, "fid": fid})
        db.commit()
        results[fid] = (it["teams"]["home"]["name"], it["teams"]["away"]["name"], hg, ag)
        print(f"OK {fid} {it['teams']['home']['name']} {hg}-{ag} {it['teams']['away']['name']}", flush=True)
finally:
    db.close()

def result_tag(hg, ag):
    return '主' if hg > ag else ('客' if hg < ag else '平')

def llm_tag(v):
    if v is None:
        return None
    s = str(v)
    if '主' in s or '胜' in s or 'home' in s.lower():
        return '主'
    if '平' in s or 'draw' in s.lower():
        return '平'
    if '客' in s or '负' in s or 'away' in s.lower():
        return '客'
    return s[:12]

db = SessionLocal()
rows = []
try:
    for fid in FIXTURE_IDS:
        r = db.execute(text("""
            SELECT f.home_name, f.away_name,
                   p.win_home, p.win_draw, p.win_away,
                   p.llm_win, p.llm_win_pct, p.llm_handicap, p.llm_handicap_pct,
                   p.llm_score,
                   p.actual_home_goals, p.actual_away_goals
            FROM predictions p JOIN fixtures f ON f.id = p.fixture_id
            WHERE p.fixture_id = :fid
        """), {"fid": fid}).mappings().first()
        if r:
            rows.append(r)
finally:
    db.close()

xgb_ok = llm_ok = score_ok = played = 0
print("\n=== 周日批次复盘 ===")
for r in rows:
    hg, ag = r["actual_home_goals"], r["actual_away_goals"]
    if hg is None:
        print(f"[未开赛] {r['home_name']} vs {r['away_name']}")
        continue
    played += 1
    actual = result_tag(hg, ag)
    xgb = max(('主', r['win_home']), ('平', r['win_draw']), ('客', r['win_away']), key=lambda x: x[1])[0]
    llm = llm_tag(r['llm_win'])
    xb = '✅' if xgb == actual else '❌'
    lb = '✅' if llm == actual else '❌'
    if xgb == actual: xgb_ok += 1
    if llm == actual: llm_ok += 1
    sc = f"{r['llm_score']}"
    s_ok = ''
    try:
        ph, pa = sc.split('-')
        if int(ph) == hg and int(pa) == ag:
            score_ok += 1
            s_ok = '🎯'
    except Exception:
        pass
    print(f"{r['home_name']} {hg}-{ag} {r['away_name']} | 实际:{actual} | XGB:{xgb}{xb}({r['win_home']:.0f}/{r['win_draw']:.0f}/{r['win_away']:.0f}) | LLM:{llm}{lb}({r['llm_win_pct']}%) | 比分:{sc}{s_ok}")

if played:
    print(f"\nXGB命中 {xgb_ok}/{played} = {xgb_ok/played*100:.0f}% | LLM命中 {llm_ok}/{played} = {llm_ok/played*100:.0f}% | 精确比分 {score_ok}/{played}")