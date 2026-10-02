# -*- coding: utf-8 -*-
"""按日期发现候选场次 —— 绕开 API-Football 免费档的 league+season 封锁。

【为什么不能用 league+season】
免费档调 /fixtures?league=13&season=2026 会被永久拒绝：
    {'plan': 'Free plans do not have access to this season, try from 2022 to 2024.'}
这是**账号套餐限制，不是每日额度**，等 UTC 00:00 重置也不会好。
加 date / from / to 都救不了，只要带 season 就挂。

【可行路径】
/fixtures?date=YYYY-MM-DD  → 返回当天全球所有比赛（约 430 场），本地按 league id 过滤。
1 次请求覆盖全部联赛，比原来「每联赛 1 次」更省额度
（免费档 100 次/天 + 10 次/分钟，超了是 429）。

用法:
    .venv/bin/python tools/discover_fixtures.py 2026-09-18
    .venv/bin/python tools/discover_fixtures.py 2026-09-17 2026-09-18
    .venv/bin/python tools/discover_fixtures.py 2026-09-18 --leagues 3,140,13
"""
import sys, os, time, json, subprocess, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

API_KEY = os.environ["API_FOOTBALL_KEY"]
BASE = os.environ["API_FOOTBALL_BASE_URL"]
HEADERS = {"x-apisports-key": API_KEY}

# API-Football 的联赛 id -> 中文名（竞彩常见）
LEAGUE_NAMES = {
    3: "欧罗巴/欧联", 2: "欧冠", 13: "解放者杯", 11: "南美杯",
    140: "西甲", 39: "英超", 135: "意甲", 78: "德甲", 61: "法甲",
    48: "英联赛杯", 45: "英足总杯", 71: "巴甲", 128: "阿甲",
    130: "美职联", 253: "美公开赛", 88: "荷甲", 94: "葡超",
    203: "土超", 235: "俄超", 307: "沙特联", 292: "韩K联", 98: "日职",
}


def api_get(path, params):
    """带 curl 兜底的 GET（requests 偶发被掐时用 curl）。"""
    last = None
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=20)
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1)
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-s", "-m", "25", "-H",
                          f"x-apisports-key: {API_KEY}", url],
                         capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return json.loads(out.stdout)
    raise last


def parse_args(argv):
    """返回 (日期列表, 联赛过滤集合或 None)。支持 --leagues 3,140,13 与 --leagues=3,140,13。"""
    dates, only = [], None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--leagues":
            i += 1
            only = {int(x) for x in argv[i].split(",")}
        elif a.startswith("--leagues="):
            only = {int(x) for x in a.split("=", 1)[1].split(",")}
        elif not a.startswith("--"):
            dates.append(a)
        i += 1
    return dates, only


def main():
    args, only = parse_args(sys.argv[1:])
    if not args:
        print(__doc__)
        return 1

    for date in args:
        data = api_get("/fixtures", {"date": date})
        errs = data.get("errors") or {}
        if errs:
            print(f"[!] {date} API 报错 {errs}", flush=True)
            if "requests" in errs:
                print("    -> 每日额度用尽，UTC 00:00 (BJ 08:00) 重置", flush=True)
            continue
        res = data.get("response") or []
        print(f"########## {date}  全天 {len(res)} 场", flush=True)

        by_league = {}
        for it in res:
            lid = it["league"]["id"]
            if only and lid not in only:
                continue
            by_league.setdefault(lid, []).append(it)

        for lid in sorted(by_league, key=lambda k: LEAGUE_NAMES.get(k, f"zz{k}")):
            items = sorted(by_league[lid], key=lambda x: x["fixture"]["date"])
            name = LEAGUE_NAMES.get(lid, "?")
            print(f"===== {lid} {name}: {len(items)} 场 =====", flush=True)
            for it in items:
                f = it["fixture"]
                # UTC -> 北京时间，方便和竞彩编号对
                print(f"  {f['id']} {f['date']} {f['status']['short']:<3} | "
                      f"{it['teams']['home']['name']} vs {it['teams']['away']['name']}", flush=True)
        time.sleep(6)   # 免费档 10 次/分钟，保守留余量
    return 0


if __name__ == "__main__":
    sys.exit(main())
