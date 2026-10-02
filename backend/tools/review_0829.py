# -*- coding: utf-8 -*-
"""复盘: 周四9场 + 周五14场, 拉比分->写DB->对比预测统计"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
from app.db.session import SessionLocal
from sqlalchemy import text

API_KEY = os.environ["API_FOOTBALL_KEY"]
BASE = os.environ["API_FOOTBALL_BASE_URL"]
HEADERS = {"x-apisports-key": API_KEY}

# 周四: 001比尔森vs红星 002奥莫尼亚vs圣图尔登 003萨尔茨堡vs米亚尔比 004奥胡斯vs本菲卡
# 005费伦茨vs特拉布宗 006安德莱vs阿拉木图 007塞尔塔vs奥萨苏纳 008巴萨vs毕尔巴鄂 009国际vs格雷米奥
THU = [1622633, 1623449, 1623450, 1623441, 1623445, 1623442, 1570336, 1570335, 1623070]
# 周五: 001不伦瑞克vs赫塔 002波鸿vs奥斯纳 003桑坦德vs埃尔切 004海湾vs新月 005蒙彼利埃vs布洛涅
# 006格罗宁根vs福图纳 007罗达vs布雷达 008拜仁vs斯图加特 009米兰vs威尼斯 010里尔vs巴黎
# 011水晶宫vs曼城 012雷克斯vs伯明翰 013里奥阿维vs里斯本 014阿拉维斯vs黄潜
FRI = [1576153, 1576152, 1570359, 1603000, 1552455, 1552144, 1551772,
       1575140, 1550097, 1552746, 1557381, 1563118, 1575481, 1570353]

db = SessionLocal()
try:
    for fid in THU + FRI:
        data = None
        for attempt in range(8):
            try:
                r = requests.get(f"{BASE}/fixtures", headers=HEADERS,
                                 params={"id": fid}, timeout=15)
                data = r.json()
                break
            except Exception as e:
                time.sleep(3 * (attempt + 1))
        if not data or not data.get("response"):
            print(f"[!] {fid} 拉取失败")
            continue
        it = data["response"][0]
        goals = it["score"]["fulltime"]
        hg, ag = goals["home"], goals["away"]
        if hg is None:
            print(f"[!] {fid} 未开赛 {it['teams']['home']['name']} vs {it['teams']['away']['name']}")
            continue
        db.execute(text("""
            UPDATE predictions SET actual_home_goals=:hg, actual_away_goals=:ag WHERE fixture_id=:fid
        """), {"hg": hg, "ag": ag, "fid": fid})
        db.commit()
        print(f"OK {fid} {it['teams']['home']['name']} {hg}-{ag} {it['teams']['away']['name']}")
        time.sleep(1.2)
finally:
    db.close()
print("SYNC_RESULTS_DONE")