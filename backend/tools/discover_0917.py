# -*- coding: utf-8 -*-
"""9-17 周四竞彩 003-011 候选发现（⚠️ 已废弃的写法，勿照抄）

🔴 本脚本用的 league+season 查法在免费档被**永久封锁**（不是额度问题）：
   {'plan': 'Free plans do not have access to this season, try from 2022 to 2024.'}
   加 date/from/to 都救不了，只要带 season 就挂。
   改用 **tools/discover_fixtures.py**（走 ?date= 全天拉取再本地过滤，1 次请求覆盖所有联赛）。

以下保留作历史记录。
(BJ 09-18 00:45~08:30 => UTC 09-17 16:45 ~ 09-18 00:30, 查 UTC 09-17/09-18)
"""
import sys, os, time, json, subprocess, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

API_KEY = os.environ["API_FOOTBALL_KEY"]
BASE = os.environ["API_FOOTBALL_BASE_URL"]
HEADERS = {"x-apisports-key": API_KEY}

LEAGUES = {
    3: "欧罗巴", 140: "西甲", 13: "解放者杯",
}


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


for DATE in ("2026-09-17", "2026-09-18"):
    print(f"########## UTC DATE {DATE}", flush=True)
    for lid, lname in LEAGUES.items():
        try:
            data = api_get("/fixtures", {"league": lid, "season": 2026, "date": DATE})
        except Exception as e:
            print(f"[{lname}] API失败 {e}", flush=True)
            continue
        if data.get("errors"):
            print(f"[{lname}] ERRORS {data['errors']}", flush=True)
        res = data.get("response") or []
        print(f"===== {lid} {lname}: {len(res)} 场 =====", flush=True)
        for it in sorted(res, key=lambda x: x["fixture"]["date"]):
            f = it["fixture"]
            print(f"  {f['id']} {f['date']} | {it['teams']['home']['name']} vs {it['teams']['away']['name']}", flush=True)
        time.sleep(0.3)
