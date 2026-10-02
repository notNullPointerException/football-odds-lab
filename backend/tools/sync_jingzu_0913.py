# -*- coding: utf-8 -*-
"""周日竞彩 007-024 (9-13 晚 ~ 9-14 凌晨): 入库 category=jingzu
007 Lecce-Monza 已开球(仅存档) 008 Lille-Troyes 已开球(仅存档)
009 Leipzig-Hamburg  010 Levante-Barcelona 011 HamKam-Molde    012 Le Mans-Lens
013 ManUtd-ManCity   014 Elversberg-Bayern 015 Napoli-Bologna  016 Getafe-Depor
017 Benfica-GilVic   018 PSV-Sparta       019 Sassuolo-Juventus 020 Brest-PSG
021 R.Sociedad-Atleti 022 Famalicao-Sporting 023 Flamengo-Corinthians 024 Chicago-NE
"""
import sys, os, time, json, subprocess, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
from app.db.session import SessionLocal
from app.services.fixture_service import _upsert_fixture
from sqlalchemy import text

API_KEY = os.environ["API_FOOTBALL_KEY"]; BASE = os.environ["API_FOOTBALL_BASE_URL"]
HEADERS = {"x-apisports-key": API_KEY}
FIDS = {7:1550122,8:1552760,9:1575159,10:1570376,11:1494758,12:1552759,13:1557404,14:1575166,
        15:1550123,16:1570375,17:1575493,18:1552169,19:1550124,20:1552757,21:1570380,
        22:1575496,23:1492375,24:1490479}

def api_get(path, params):
    last=None
    for a in range(5):
        try:
            r=requests.get(f"{BASE}{path}",headers=HEADERS,params=params,timeout=15); return r.json()
        except Exception as e: last=e; time.sleep(1)
    url=f"{BASE}{path}?"+urllib.parse.urlencode(params)
    out=subprocess.run(["curl","-s","-m","20","-H",f"x-apisports-key: {API_KEY}",url],capture_output=True,text=True)
    if out.returncode==0 and out.stdout.strip(): return json.loads(out.stdout)
    raise last

db=SessionLocal(); ok=[]; miss=[]
try:
    for idx,fid in sorted(FIDS.items()):
        d=api_get("/fixtures",{"id":fid})
        if not d or not d.get("response"): print(f"MISSING {idx:03d} {fid}",flush=True); miss.append(idx); continue
        it=d["response"][0]
        _upsert_fixture(db,it)
        db.execute(text("UPDATE fixtures SET category='jingzu' WHERE id=:f"),{"f":fid}); db.commit()
        print(f"OK {idx:03d} [{fid}] {it['teams']['home']['name']} vs {it['teams']['away']['name']} ({it['league']['name']})",flush=True)
        ok.append(idx); time.sleep(0.3)
    print(f"SYNCED {len(ok)} MISSING {miss}",flush=True)
finally: db.close()
