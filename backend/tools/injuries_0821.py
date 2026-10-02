"""周五批次伤停查询: /injuries?fixture= 每场一次, 带重试"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from app.core.config import settings

FIDS = [1602982, 1495763, 1494255, 1602985, 1552443, 1551757, 1552733, 1557367, 1570345]

def fetch(fid):
    for attempt in range(5):
        try:
            r = httpx.get(f"{settings.api_football_base_url}/injuries",
                          headers={"x-apisports-key": settings.api_football_key},
                          params={"fixture": fid}, timeout=30)
            return r.json().get("response", [])
        except Exception as e:
            print(f"  retry {fid} #{attempt+1}: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
    return None

for fid in FIDS:
    items = fetch(fid)
    if items is None:
        print(f"== {fid}: 网络失败,跳过")
        continue
    if not items:
        print(f"== {fid}: 无伤停数据 (API 无覆盖或无人伤停)")
        continue
    print(f"== {fid}")
    for it in items:
        p = it.get("player") or {}
        t = it.get("team") or {}
        print(f"  [{t.get('name')}] {p.get('name')} - {p.get('type')} - {p.get('reason')}")
