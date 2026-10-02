import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from app.core.config import settings

FIDS = [1557368, 1563099]

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
        print(f"== {fid}: 网络失败")
        continue
    if not items:
        print(f"== {fid}: 无伤停数据")
        continue
    print(f"== {fid}")
    seen = set()
    for it in items:
        p = it.get("player") or {}
        t = it.get("team") or {}
        key = (t.get('name'), p.get('name'), p.get('reason'))
        if key in seen: continue
        seen.add(key)
        print(f"  [{t.get('name')}] {p.get('name')} - {p.get('type')} - {p.get('reason')}")
