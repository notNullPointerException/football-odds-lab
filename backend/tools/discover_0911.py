# -*- coding: utf-8 -*-
"""9-11 周四竞彩 001-007 候选发现: 按 league + date 打印全部球队名 (UTC: 001-006=09-10, 007=09-10/09-11)"""
import sys, os, time, json, subprocess, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

API_KEY = os.environ["API_FOOTBALL_KEY"]
BASE = os.environ["API_FOOTBALL_BASE_URL"]
HEADERS = {"x-apisports-key": API_KEY}

LEAGUES = {2: "欧冠", 94: "葡超", 13: "解放者杯"}

def api_get(path, params):
    last = None
    for attempt in range(5):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=15)
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1)
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-m", "20", "-H",
                          f"x-apisports-key: {API_KEY}", url],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout)
    raise last

for DATE in ("2026-09-10", "2026-09-11"):
    print(f"########## DATE {DATE}", flush=True)
    for lid, lname in LEAGUES.items():
        try:
            data = api_get("/fixtures", {"league": lid, "season": 2026, "date": DATE})
        except Exception as e:
            print(f"[{lname}] API失败 {e}", flush=True)
            continue
        res = data.get("response") or []
        print(f"===== {lid} {lname}: {len(res)} 场 =====", flush=True)
        for it in sorted(res, key=lambda x: x["fixture"]["date"]):
            f = it["fixture"]
            print(f"  {f['id']} {f['date']} | {it['teams']['home']['name']} vs {it['teams']['away']['name']}", flush=True)
        time.sleep(0.3)
