# -*- coding: utf-8 -*-
"""预测周日竞彩 009-024 (16 场)"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db.session import SessionLocal
from prediction.predict import predict_fixture

FIDS=[(1575159,"009 莱红牛vs汉堡"),(1570376,"010 莱万特vs巴萨"),
      (1494758,"011 汉坎vs莫尔德"),(1552759,"012 勒芒vs朗斯"),
      (1557404,"013 曼联vs曼城"),(1575166,"014 埃沃斯堡vs拜仁"),
      (1550123,"015 那不勒斯vs博洛尼亚"),(1570375,"016 赫塔费vs拉科"),
      (1575493,"017 本菲卡vs吉维森特"),(1552169,"018 埃因霍温vs鹿斯巴达"),
      (1550124,"019 萨索洛vs尤文"),(1552757,"020 布雷斯特vs巴黎"),
      (1570380,"021 皇家社会vs马竞"),(1575496,"022 法马利康vs里斯本"),
      (1492375,"023 弗拉门戈vs科林蒂安"),(1490479,"024 芝加哥vs新英格兰")]
db=SessionLocal(); ok=0; fail=[]
try:
    for fid,nm in FIDS:
        t0=time.time()
        print(f"[跑] {nm} [{fid}] ...",flush=True)
        try:
            predict_fixture(fid, db=db); ok+=1
            print(f"    OK {time.time()-t0:.1f}s",flush=True)
        except Exception as e:
            fail.append(nm); print(f"    FAIL: {e}",flush=True)
finally: db.close()
print(f"DONE ok={ok} fail={fail}",flush=True)
