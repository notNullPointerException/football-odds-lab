"""
推理入口:给定 fixture_id,执行完整预测流程并写入 predictions 表.

流程:
  1. 从 DB 读取比赛信息
  2. 提取特征 → XGBoost 推理(λ, Top3, 参考概率)
  3. 赔率API(实时)
  4. LLM(全量数据 + XGBoost 参考) 最终决策
  5. 写入 predictions 表
"""

import os
import sys
import json
import pickle
import re
import threading
from datetime import datetime

from prediction.errors import PredictError   # 失败要带原因，别让调用方只拿到 None

# 用户指定永久拉黑的对阵(blacklist.json),命中直接跳过不预测
_BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), "..", "tools", "blacklist.json")
_BLACKLIST = []
try:
    with open(_BLACKLIST_FILE, "r", encoding="utf-8") as _f:
        _BLACKLIST = json.load(_f).get("fixtures", [])
except Exception:
    pass

import numpy as np
from scipy.stats import poisson
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy import text
from app.db.session import SessionLocal
from app.core.config import settings
from prediction.features import extract_features_for_fixture
from prediction.training.model import load_models, _fill_na
from prediction.training.data import assign_group

import pandas as pd

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
FEATURE_COLS_PATH = os.path.join(MODELS_DIR, 'feature_cols.pkl')


def _load_feature_cols() -> list[str]:
    with open(FEATURE_COLS_PATH, 'rb') as f:
        return pickle.load(f)


def _load_best_models(league_id: int) -> tuple[dict, str]:
    league_key = f'L_{league_id}'
    m = load_models(league_key)
    if m:
        return m, league_key
    group = assign_group(league_id)
    m = load_models(group)
    if m:
        return m, group
    m = load_models('GLOBAL')
    if m:
        return m, 'GLOBAL'
    return None, None


def _poisson_top3(lambda_home: float, lambda_away: float,
                  max_goals: int = 8) -> list[dict]:
    lh = max(0.1, min(lambda_home, 8.0))
    la = max(0.1, min(lambda_away, 8.0))
    scores = []
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = poisson.pmf(h, lh) * poisson.pmf(a, la)
            scores.append({'score': f'{h}-{a}', 'prob': round(float(p), 4)})
    scores.sort(key=lambda x: x['prob'], reverse=True)
    return scores[:3]


def _call_llm(prompt: str) -> dict | None:
    try:
        import httpx
        resp = httpx.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.deepseek_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 1500,
                # 关闭思考模式: pro 深度推理会吃光 token 预算导致 content 为空
                "thinking": {"type": "disabled"},
            },
            timeout=90.0,
        )
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content']
        parsed = _parse_llm_json(content)
        if parsed and _has_required_llm_fields(parsed):
            return parsed
        logger.warning(f"LLM 返回缺少必要预测字段: {content[:800]}")
    except Exception as e:
        logger.warning(f"LLM 失败: {e}")
    return None


def _parse_llm_json(content: str) -> dict | None:
    """从模型回复中提取第一个合法 JSON object。"""
    decoder = json.JSONDecoder()
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _has_required_llm_fields(data: dict) -> bool:
    required = (
        "win", "win_pct", "score",
        "handicap_num", "handicap_team", "handicap_pct",
        "ou_line", "ou_type", "ou_pct",
    )
    return all(str(data.get(k) or "").strip() for k in required)


def _fetch_odds(fixture_id: int, kickoff_ts: int | None = None) -> dict | None:
    """拉取该场的庄家赔率（1X2 优先，没有则退亚盘）。

    🔴 2026-09-28 修：`/odds` 的 `date` 参数是【开球的 UTC 日期】，不是「快照日期」。
    原实现从今天往回扫 7 天（today-0 … today-6），于是
    **任何开球 UTC 日期在明天及以后的场次，永远扫不到自己的那一天** → 直接返回 None。
    实测 26134 期 14 场里 9 场因此丢盘（7 场是「比赛在未来」，另 2 场见下），
    而它们的盘在 api-football 上明明都在（各 9 家，含 Pinnacle）。
    现在改成**按开球的 UTC 日期查**，再拿 ±1 天兜底（防跨日/时区边界），最多 3 次请求。
    注意：同一个 fixture 只有正中那一天有数据，所以原来那个「7 天赔率变化」的设计
    本身就是建立在对这个参数的误解上的，拿不到任何跨天变化。

    第二个坑：原来用裸 httpx（timeout=10、无重试、异常一律 return None），
    一次超时/429 就静默丢掉这场的全部赔率。改用 apifootball.api_get（带退避重试）。
    """
    try:
        from datetime import timedelta, timezone

        from app.core.apifootball import api_get, check_api_error

        if kickoff_ts:
            utc_day = datetime.fromtimestamp(int(kickoff_ts), tz=timezone.utc).date()
        else:
            # 拿不到开球时刻就退回「今天」—— 至少不比原来更差
            utc_day = datetime.now(timezone.utc).date()

        # 收集所有庄家数据: {bookmaker_name: {date: {home_odd, draw_odd, away_odd}}}
        bookmaker_odds = {}
        # 没有胜平负盘时的备选：亚盘（2 路、无平局）
        ah_odds = {}

        for date_str in [(utc_day + timedelta(days=o)).strftime("%Y-%m-%d")
                         for o in (0, -1, 1)]:
            try:
                resp = api_get("/odds", {"fixture": fixture_id, "date": date_str},
                               context=f"赔率 fixture={fixture_id} date={date_str}")
            except Exception as e:
                logger.warning(f"赔率查询失败 fixture={fixture_id} date={date_str}: "
                               f"{type(e).__name__}: {e}")
                continue
            # 🔴 必须先查 body 里的 errors：api-football 的额度耗尽/套餐拒绝是
            #    【HTTP 200 + errors 非空 + response: []】—— api_get 只看状态码，
            #    这里不查的话，「三天都空 → return None」和「这场真的没盘」长得一模一样，
            #    日志里一个字都没有。这正是 9-18/9-24 那两次「假装成功」的同一族坑。
            if check_api_error(resp, f"赔率 fixture={fixture_id} date={date_str}"):
                continue
            # errors 为空 + response: [] 才是【真·没有赔率】，照常跳过
            data = resp.get("response") or []
            if not data:
                continue

            for bm in data[0].get("bookmakers", []):
                bm_name = bm.get("name", "未知")
                if bm_name not in bookmaker_odds:
                    bookmaker_odds[bm_name] = {}
                for bet in bm.get("bets", []):
                    if bet.get("name") != "Match Winner":
                        continue
                    vals = {v["value"]: float(v["odd"]) for v in bet.get("values", [])}
                    ho, dr, aw = vals.get("Home"), vals.get("Draw"), vals.get("Away")
                    if not all([ho, dr, aw]):
                        continue
                    t = 1/ho + 1/dr + 1/aw
                    bookmaker_odds[bm_name][date_str] = {
                        "home_odd": round(1/ho / t, 3),
                        "draw_odd": round(1/dr / t, 3),
                        "away_odd": round(1/aw / t, 3),
                        "home_raw": ho, "draw_raw": dr, "away_raw": aw,
                    }
                    break
                # 亚盘：一次返回整条盘口线，取最接近平手的那条（主盘）
                for bet in bm.get("bets", []):
                    if bet.get("name") != "Asian Handicap":
                        continue
                    by_line = {}
                    for v in bet.get("values", []):
                        side, _, ln = str(v["value"]).rpartition(" ")
                        if side not in ("Home", "Away"):
                            continue
                        try:
                            by_line.setdefault(float(ln), {})[side] = float(v["odd"])
                        except ValueError:
                            continue
                    pairs = [(ln, d) for ln, d in by_line.items()
                             if d.get("Home") and d.get("Away")]
                    if pairs:
                        ln, d = min(pairs, key=lambda x: (abs(x[0]),
                                                          1/x[1]["Home"] + 1/x[1]["Away"]))
                        ah_odds.setdefault(bm_name, {})[date_str] = {
                            "home_raw": d["Home"], "away_raw": d["Away"], "line": ln,
                        }
                    break

            # 这个日期就是这个 fixture 的赔率，±1 天不用再问了（省额度）
            if bookmaker_odds or ah_odds:
                break

        if not bookmaker_odds and not ah_odds:
            # 三个日期都没盘。留一行痕：否则「为什么这场没盘」永远查不出来
            # （被拒的情况上面已经 ERROR 过了，这里是不涉及错误码的真·无数据）。
            logger.warning(f"赔率三个日期都无数据 fixture={fixture_id} "
                           f"kickoff_utc_day={utc_day}")
            return None

        # 入库用：挑一个庄家的最新一天原始赔率（Pinnacle 水位最低、最接近真实概率）
        def _newest(src):
            for b in ("Pinnacle", "Bet365", "Betfair", "1xBet"):
                if src.get(b):
                    day = max(src[b])
                    return b, day, src[b][day]
            for b, d in sorted(src.items(), key=lambda x: -len(x[1])):
                if d:
                    day = max(d)
                    return b, day, d[day]
            return None, None, None

        pick_bm, snap, o = _newest(bookmaker_odds)
        if pick_bm:
            pay = 1.0 / sum(1.0 / o[k] for k in ("home_raw", "draw_raw", "away_raw"))
            pick = {"bet": "1x2", "bookmaker": pick_bm, "snapshot": snap, "line": None,
                    "home": o["home_raw"], "draw": o["draw_raw"], "away": o["away_raw"],
                    "payback": round(pay, 4),
                    "devig": [round(pay / o["home_raw"], 4),
                              round(pay / o["draw_raw"], 4),
                              round(pay / o["away_raw"], 4)]}
        else:
            pick_bm, snap, o = _newest(ah_odds)
            pay = 1.0 / (1.0 / o["home_raw"] + 1.0 / o["away_raw"])
            pick = {"bet": "ah", "bookmaker": pick_bm, "snapshot": snap, "line": o["line"],
                    "home": o["home_raw"], "draw": None, "away": o["away_raw"],
                    "payback": round(pay, 4),
                    "devig": [round(pay / o["home_raw"], 4), None,
                              round(pay / o["away_raw"], 4)]}

        # 取数据最多的前5个庄家
        sorted_bms = sorted(bookmaker_odds.items(), key=lambda x: -len(x[1]))[:5]
        # 格式化输出 —— 遍历【实际取到的日期】，别再按「今天往回 7 天」拼：
        # 现在按开球的 UTC 日期查，能取到的那天通常【不是今天】，硬拼日期会全是「无数据」。
        lines = []
        for bm_name, dates in sorted_bms:
            lines.append(f"  {bm_name}:")
            for key in sorted(dates):
                odds = dates[key]
                lines.append(f"    {key[5:]}: 主{odds['home_odd']:.0%}({odds['home_raw']}) "
                             f"平{odds['draw_odd']:.0%}({odds['draw_raw']}) "
                             f"客{odds['away_odd']:.0%}({odds['away_raw']})")

        return {"text": ("\n" + "\n".join(lines)) if lines else "", "pick": pick}
    except Exception as e:
        # 🔴 用 warning 不用 debug：这里曾经把一个 NameError 整整吞掉，
        #    症状是「赔率全都为 None」而日志里一个字都没有，排查了很久。
        #    赔率拿不到本身不是崩溃，但【为什么拿不到】必须留痕。
        logger.warning(f"赔率获取失败 fixture={fixture_id}: {type(e).__name__}: {e}")
    return None


# 体彩赔率缓存：批量预测时 predict_fixture 是逐场调用的，没有缓存就会每场打一次体彩接口
_JC_CACHE = {"at": 0.0, "by_fid": {}}
_JC_TTL = 120
# 🔴 胜负彩页那个「跑 14 场预测」按钮是【并行】发的，14 个线程会同时看到缓存过期、
#    同时去打 14 次体彩接口（缓存等于没有，还平白挨限流）。锁 + 二次检查：
#    只有第一个进去的真的刷，其余排队进来时时间已经被刷新过，直接读缓存。
_JC_LOCK = threading.Lock()


def _fetch_jc_odds(fixture_id: int) -> dict | None:
    """这场比赛「此刻」的体彩竞彩盘（胜平负 + 让球）。

    只有【正在售卖的竞彩场次】才有：非竞彩场次、已停售、停售后重跑，一律 None。
    数据源与页面同源（app.api.jc 的同一份 payload），不额外走 HTTP。
    """
    import time
    if time.time() - _JC_CACHE["at"] > _JC_TTL:
        with _JC_LOCK:
            # 锁里必须【再判一次】：等锁的这段时间里，先进来的那个可能已经刷完了。
            # 只在锁外判的话，排队的线程会依次进来各刷一遍 —— 锁就白加了。
            if time.time() - _JC_CACHE["at"] > _JC_TTL:
                try:
                    from app.api.jc import _build_payload
                    payload = _build_payload()
                except Exception as e:
                    logger.debug(f"体彩赔率获取失败: {e}")
                    return None
                by_fid = {}
                for day in payload.get("days") or []:
                    for m in day.get("matches") or []:
                        if m.get("fixture_id"):
                            by_fid[m["fixture_id"]] = m
                _JC_CACHE.update(at=time.time(), by_fid=by_fid)
    return _JC_CACHE["by_fid"].get(fixture_id)


def _jc_pool(p: dict | None, with_line: bool = False) -> dict | None:
    if not p or p.get("h") is None:
        return None
    out = {"h": p.get("h"), "d": p.get("d"), "a": p.get("a"),
           "payback": p.get("payback"),
           "devig": [p.get("p_h"), p.get("p_d"), p.get("p_a")]}
    if with_line:
        out["line"] = p.get("line")
        out["side"] = p.get("side")
    return out


def _jc_pick(m: dict) -> dict | None:
    """体彩 match -> 入库结构。胜平负与让球两个池子都留（让球是实际下注的盘）。"""
    had = _jc_pool(m.get("had"))
    hhad = _jc_pool(m.get("hhad"), with_line=True)
    if not had and not hhad:
        return None
    return {"match_num": m.get("match_num"), "had": had, "hhad": hhad}


def _fetch_standings_text(db, team_id, league_id, season) -> str:
    row = db.execute(text("""
        SELECT `rank`, points, goals_diff, all_played, all_win, all_draw, all_lose
        FROM standings WHERE team_id=:tid AND league_id=:lid AND season=:s LIMIT 1
    """), {"tid": team_id, "lid": league_id, "s": season}).fetchone()
    if not row:
        return "无积分榜数据"
    d = dict(row._mapping)
    return f"排名{d['rank']} 积{d['points']}分 {d['all_win']}胜{d['all_draw']}平{d['all_lose']}负 净胜球{d['goals_diff']}"


def _fetch_team_recent_via_api(team_id: int) -> str:
    """通过API拉取球队近10场真实战绩（含逐场对手明细）"""
    try:
        import httpx
        # trust_env=False: 绕开系统代理直连，理由见 app/core/apifootball.py docstring
        r = httpx.get(
            f"{settings.api_football_base_url}/fixtures",
            headers={"x-apisports-key": settings.api_football_key},
            params={"team": team_id, "last": 10},
            timeout=10.0,
            trust_env=False,
        )
        r.raise_for_status()
        data = r.json().get("response", [])
        if not data:
            return "无"
        wins = draws = losses = gf = ga = 0
        details = []
        results_form = []
        for item in data:
            teams = item["teams"]
            goals = item["goals"]
            league = item.get("league", {})
            is_home = teams["home"]["id"] == team_id
            gh = goals["home"] if goals["home"] is not None else 0
            ga_ = goals["away"] if goals["away"] is not None else 0
            opponent = teams["away"]["name"] if is_home else teams["home"]["name"]
            league_name = league.get("name", "")
            if is_home:
                gf += gh; ga += ga_
                if gh > ga_: w = 'W'; wins += 1
                elif gh == ga_: w = 'D'; draws += 1
                else: w = 'L'; losses += 1
                score = f"{gh}-{ga_}"
            else:
                gf += ga_; ga += gh
                if ga_ > gh: w = 'W'; wins += 1
                elif ga_ == gh: w = 'D'; draws += 1
                else: w = 'L'; losses += 1
                score = f"{ga_}-{gh}"
            results_form.append(w)
            details.append(f"  {opponent}({league_name}) {score} {w}")

        results_form.reverse()
        details.reverse()
        n = len(data)
        detail_text = '\n'.join(details)
        form_str = ' '.join(results_form[-10:])
        return f"{wins}胜{draws}平{losses}负 进{gf}球失{ga}球 场均进{gf/n:.1f}失{ga/n:.1f}\n近10场明细:\n{detail_text}"
    except Exception as e:
        logger.debug(f"API拉取球队数据失败 team={team_id}: {e}")
        return "无"


def _fetch_lineups_text(fixture_id: int, home_id: int, away_id: int) -> str:
    """拉取确认首发；未公布时返回阵容不确定提示。"""
    try:
        import httpx
        # trust_env=False: 绕开系统代理直连，理由见 app/core/apifootball.py docstring
        r = httpx.get(
            f"{settings.api_football_base_url}/fixtures/lineups",
            headers={"x-apisports-key": settings.api_football_key},
            params={"fixture": fixture_id},
            timeout=10.0,
            trust_env=False,
        )
        r.raise_for_status()
        data = r.json().get("response", [])
        if not data:
            return "未获取到确认首发。国家队/世界杯阵容轮换、临场战术和球员状态不确定，必须降低置信度。"

        lines = []
        for team_entry in data:
            team = team_entry.get("team") or {}
            tid = team.get("id")
            side = "主队" if tid == home_id else "客队" if tid == away_id else "球队"
            formation = team_entry.get("formation") or "未知阵型"
            starters = []
            for item in team_entry.get("startXI") or []:
                player = item.get("player") or {}
                name = player.get("name")
                pos = player.get("pos")
                if name:
                    starters.append(f"{name}{f'({pos})' if pos else ''}")
            if starters:
                lines.append(f"{side} {team.get('name', '')} {formation}: " + ", ".join(starters))

        if not lines:
            return "已查询阵容接口，但未得到有效首发名单。按阵容未确认处理，降低置信度。"
        return "确认首发:\n" + "\n".join(lines)
    except Exception as e:
        logger.debug(f"首发阵容获取失败 fixture={fixture_id}: {e}")
        return "首发阵容获取失败。按阵容未确认处理，特别是世界杯/国家队赛事必须降低置信度。"


def _competition_context(fixture: dict) -> str:
    league_name = str(fixture.get("league_name") or "")
    round_name = str(fixture.get("round") or "")
    text = f"{league_name} {round_name}".lower()

    if "world cup" in text or "世界杯" in text:
        kind = "世界杯/国家队正赛"
    elif any(k in text for k in ("qualifier", "qualification", "预选")):
        kind = "国家队预选赛"
    elif any(k in text for k in ("friendlies", "friendly", "友谊")):
        kind = "友谊赛"
    elif any(k in text for k in ("u23", "u21", "u20", "u19")):
        kind = "青年队赛事"
    elif any(k in text for k in ("women", "女足")):
        kind = "女足赛事"
    elif any(k in text for k in ("cup", "杯", "fifa", "euro", "copa", "olympic", "奥运")):
        kind = "杯赛/锦标赛"
    else:
        kind = ""

    if kind:
        return (
            f"本场属于{kind}。"
            "这类比赛样本少、轮换多、战意和赛制影响大，赔率市场不可作为主要判断依据。"
        )
    return (
        "本场更接近常规联赛环境。赔率仍只能作为市场情绪和风险提示，"
        "最终判断应优先依据球队状态、主客场、积分背景和对手含金量。"
    )


_INJ_TYPE_ZH = {"Injured": "伤停", "Suspended": "停赛",
                "Missing Fixture": "缺阵", "Questionable": "存疑"}
_INJ_STATUS_ZH = {"Out": "缺席", "Doubtful": "存疑", "Questionable": "存疑"}


def _fetch_injuries_text(fixture_id: int, home_id: int, away_id: int) -> str:
    """拉取该场比赛双方伤停/停赛名单，按主客队分组"""
    try:
        import httpx
        # trust_env=False: 绕开系统代理直连，理由见 app/core/apifootball.py docstring
        r = httpx.get(
            f"{settings.api_football_base_url}/injuries",
            headers={"x-apisports-key": settings.api_football_key},
            params={"fixture": fixture_id},
            timeout=10.0,
            trust_env=False,
        )
        r.raise_for_status()
        data = r.json().get("response", [])
        if not data:
            return "该场比赛暂无伤停数据"

        # API 偶尔返回重复条目，按 (team_id, player_id) 去重
        seen: set[tuple] = set()
        by_team: dict[int, list[str]] = {}
        for item in data:
            player = item.get("player") or {}
            team = item.get("team") or {}
            name = player.get("name")
            if not name:
                continue
            key = (team.get("id"), player.get("id"))
            if key in seen:
                continue
            seen.add(key)
            typ = player.get("type") or "伤停"
            reason = player.get("reason") or ""
            status = player.get("status") or ""
            parts = [name, _INJ_TYPE_ZH.get(typ, typ)]
            if reason:
                parts.append(reason)
            if status:
                parts.append(_INJ_STATUS_ZH.get(status, status))
            by_team.setdefault(team.get("id"), []).append(" - ".join(parts))

        if not by_team:
            return "该场比赛暂无伤停数据"

        lines = []
        for tid, names in by_team.items():
            side = "主队" if tid == home_id else "客队" if tid == away_id else "球队"
            team_name = next((t.get("name") for t in data if (t.get("team") or {}).get("id") == tid), "")
            lines.append(f"{side}{team_name}: " + " | ".join(names))
        return "伤停名单:\n" + "\n".join(lines)
    except Exception as e:
        logger.debug(f"伤停获取失败 fixture={fixture_id}: {e}")
        return "伤停数据获取失败。若有核心球员缺阵将影响判断，注意降低置信度。"


def _build_llm_prompt(fixture: dict, xgb_result: dict, odds: dict | None,
                      home_stats: str, away_stats: str,
                      home_standings: str, away_standings: str,
                      lineups_text: str, injuries_text: str) -> str:
    pw = xgb_result
    top3_str = '  '.join(f"{t['score']}({t['prob']:.0%})" for t in pw['top3'])

    odds_text = ""
    if odds and odds.get("text"):
        odds_text = f"{odds['text']}"

    model_probs = {'主胜': pw['win_home'], '平局': pw['win_draw'], '客胜': pw['win_away']}
    model_top = max(model_probs, key=model_probs.get)
    model_top_pct = model_probs[model_top]
    competition_context = _competition_context(fixture)

    return f"""你是一位专业足球分析师.请对以下比赛进行独立分析并给出最终预测.

【比赛信息】
{fixture['home_name']} vs {fixture['away_name']}
联赛:{fixture['league_name']} 赛季:{fixture['season']}
赛事属性:{competition_context}
阵容信息:{lineups_text}
伤停信息:{injuries_text}

【{fixture['home_name']} 完整数据】
积分榜: {home_standings}
近10场战绩: {home_stats}

【{fixture['away_name']} 完整数据】
积分榜: {away_standings}
近10场战绩: {away_stats}

{"" if not odds_text else "【市场赔率】" + odds_text}

【机器模型参考】仅作校准，不是结论
胜平负: 主{pw['win_home']:.0%} 平{pw['win_draw']:.0%} 客{pw['win_away']:.0%}
让球参考:{pw['handicap']}  大小球参考:{pw['over25_prob']:.0%}大球
Top3比分参考: {top3_str}
模型最高项:{model_top}({model_top_pct:.0%})

【重要规则】
1. 必须先基于球队状态、对手含金量、主客场表现、积分背景、赛事属性和赛程动机独立判断，再参考机器模型做校准。
2. 不要因为模型最高项是{model_top}就默认选择它；如果球队数据、赛程背景或赔率信号不支持，应选择其他结果。
3. 重点检查平局和冷门可能性，不要为了迎合模型概率而排除低概率但合理的结果。
4. 模型概率只表示历史数据下的统计参考，不能替代你的最终判断。
5. 赔率只代表市场价格和热度，不代表真实胜率；世界杯、杯赛、国家队、友谊赛、青年队、女足或样本不足时，赔率权重必须降低。
6. 世界杯/国家队比赛若没有确认首发，不要假设固定阵容；必须考虑轮换、伤停、战术调整、体能和临场动机，并降低置信度。
7. 置信度要反映真实不确定性；数据冲突、杯赛、友谊赛、世界杯、阵容未确认或样本不足时应降低置信度。

请严格输出JSON格式:
{{"win":"主胜|平局|客胜","win_pct":"本场预测信心百分比,如85%","score":"三个最可能比分用逗号分隔如2-1,1-1,3-0","handicap_num":"让球数,负数=主队让,正数=客队让,如-1","handicap_team":"主队或客队","handicap_pct":"让球方赢盘概率百分比,如65%","ou_line":"大小球线如2.5","ou_type":"大或小","ou_pct":"大小球概率百分比如60%","brief_analysis":"一句话结论(20字内)","core_data":"主客队数据对比(100字内)","deep_report":"攻防对比/数据支撑(200字内)"}}"""


def predict_fixture(fixture_id: int, db=None) -> dict | None:
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        # 1. 读取比赛
        row = db.execute(text("""
            SELECT f.id, f.home_id, f.away_id, f.league_id, f.season,
                   f.date, f.timestamp, f.goals_home, f.goals_away,
                   f.home_name, f.away_name, f.home_logo, f.away_logo,
                   f.league_name
            FROM fixtures f WHERE f.id = :fid
        """), {"fid": fixture_id}).fetchone()
        if not row:
            return None
        fixture = dict(row._mapping)

        # 0. 黑名单拦截(用户指定拉黑对阵)
        for _b in _BLACKLIST:
            if fixture['home_name'] == _b['home'] and fixture['away_name'] == _b['away']:
                logger.info(f"黑名单对阵跳过: {fixture['home_name']} vs {fixture['away_name']} ({_b.get('note', '')})")
                return None

        # 2. 特征 + XGBoost
        # 下面这两处以前是 return None，跟「黑名单跳过」混在同一个出口，调用方分不清
        # 是「不用预测」还是「预测不了」。改成抛 PredictError，原因能一路带到页面上。
        feat = extract_features_for_fixture(db, fixture)
        if feat is None:
            raise PredictError("特征不足，无法提取（该场历史数据不完整）", "feature")

        feature_cols = _load_feature_cols()
        models, model_group = _load_best_models(fixture['league_id'])
        if models is None:
            raise PredictError(f"联赛 {fixture['league_name']} 没有可用模型", "model")

        X = pd.DataFrame([feat])[feature_cols]
        X_filled = _fill_na(X)
        win_probs = models['win'].predict_proba(X_filled)[0]
        over25_prob = float(models['over25'].predict_proba(X_filled)[0][1])
        lambda_home = float(models['lambda_home'].predict(X_filled)[0])
        lambda_away = float(models['lambda_away'].predict(X_filled)[0])
        top3 = _poisson_top3(lambda_home, lambda_away)

        # 让球: 最低档0.5球, 无"平手"
        diff = lambda_home - lambda_away
        ad = abs(diff)
        if ad < 0.75:
            val = "0.5"
        elif ad < 1.25:
            val = "1"
        elif ad < 1.75:
            val = "1.5"
        else:
            val = "2"
        hc = f"{'主让' if diff > 0 else '客让'}{val}球"

        xgb_result = {
            'win_home': float(win_probs[2]),
            'win_draw': float(win_probs[1]),
            'win_away': float(win_probs[0]),
            'over25_prob': over25_prob,
            'top3': top3,
            'lambda_home': lambda_home,
            'lambda_away': lambda_away,
            'handicap': hc,
        }

        # 3. 赔率：庄家盘存 odds_*，体彩盘存 jc_*（两者口径不同，别混用）
        #    timestamp 是开球的真正 epoch（f.date 是本地 UTC+8），_fetch_odds 要靠它
        #    算出开球的 UTC 日期 —— 少了它就会去问「今天」从而整场丢盘。
        odds = _fetch_odds(fixture_id, fixture.get('timestamp'))
        _jc = _fetch_jc_odds(fixture_id)
        jc_pick = _jc_pick(_jc) if _jc else None

        # 4. 全量数据
        home_stats = _fetch_team_recent_via_api(fixture['home_id'])
        away_stats = _fetch_team_recent_via_api(fixture['away_id'])
        home_standings = _fetch_standings_text(db, fixture['home_id'], fixture['league_id'], fixture['season'])
        away_standings = _fetch_standings_text(db, fixture['away_id'], fixture['league_id'], fixture['season'])
        lineups_text = _fetch_lineups_text(fixture_id, fixture['home_id'], fixture['away_id'])
        injuries_text = _fetch_injuries_text(fixture_id, fixture['home_id'], fixture['away_id'])

        # 5. LLM
        llm_result = None
        try:
            prompt = _build_llm_prompt(fixture, xgb_result, odds,
                                       home_stats, away_stats,
                                       home_standings, away_standings,
                                       lineups_text, injuries_text)
            llm_result = _call_llm(prompt)
        except Exception as e:
            logger.debug(f"LLM失败: {e}")
        if llm_result is None:
            # 最常见的一种失败：LLM 超时/断流/返回的 JSON 缺字段。以前这里 return None，
            # 库和 created_at 里什么都不留 —— 页面上只显示「跑失败」，看不出是网络还是没模型。
            logger.warning(f"预测失败 fixture={fixture_id}: LLM 未返回完整预测，跳过入库")
            raise PredictError("LLM 未返回完整预测（超时或返回内容不完整，可直接重跑）", "llm")

        # 6. 写库
        _save_prediction(db, fixture, xgb_result, llm_result, odds, model_group, jc_pick,
                         injuries_text=injuries_text, lineups_text=lineups_text)

        # 伤停/首发原文一并带出：它们以前只活在 prompt 里，出了这个函数就查不到了 ——
        # 「喂进去的是真名单还是兜底话术」只能靠把原文带出来才分得清。
        # 用 {**llm_result, ...} 另建字典，别往 llm_result 里塞展示用的键：它上面已经传给
        # _save_prediction 并驱动过 _has_required_llm_fields 校验了。
        result = {**xgb_result,
                  'llm': {**llm_result, 'injuries': injuries_text, 'lineups': lineups_text},
                  'model_group': model_group}
        logger.info(
            f"预测完成 fixt={fixture_id} "
            f"{fixture['home_name']} vs {fixture['away_name']} "
            f"LLM:{llm_result.get('win','-') if llm_result else '-'}"
        )
        return result

    finally:
        if own_db:
            db.close()


def _llm_handicap_str(llm: dict) -> str | None:
    """LLM 输出 handicap_num/team 字段，拼成可读让球盘文本（负数=主队让）"""
    try:
        num = float(llm.get("handicap_num") or 0)
        team = str(llm.get("handicap_team") or "").strip()
        if not team:
            return None
        if num == 0:
            return "平手盘"
        if num < 0:
            return f"{team}让{abs(num)}球"
        return f"{team}让{num}球"
    except (TypeError, ValueError):
        return None


def _llm_ou_str(llm: dict) -> str | None:
    """LLM 输出 ou_line/ou_type 字段，拼成可读大小球文本"""
    line = llm.get("ou_line")
    typ = str(llm.get("ou_type") or "").strip()
    if line is None or not typ:
        return None
    return f"{typ} {line}"


def _save_prediction(db, fixture, xgb, llm, odds, model_group, jc_pick=None,
                     injuries_text=None, lineups_text=None):
    """赔率只认「首次预测那一刻」的值：ON DUPLICATE 用 COALESCE，后续重跑不覆盖已写入的。

    injuries_text / lineups_text 是喂给 LLM 的伤停/首发原文（_fetch_*_text 的返回值，
    含「暂无伤停数据」「获取失败」这些哨兵串）。哨兵串【原样存】不要归一化成 NULL ——
    否则事后又分不清是「查了确实没有」还是「压根没查成」，正好把加这两列要解决的问题
    重造一遍。存储语义跟 llm_* 一族：越接近开赛的越准，无条件覆盖。
    """
    llm = llm or {}
    pick = (odds or {}).get("pick") or {}
    j_had = (jc_pick or {}).get("had") or {}
    j_hhad = (jc_pick or {}).get("hhad") or {}
    db.execute(text("""
        INSERT INTO predictions (
            fixture_id, home_name, away_name, home_logo, away_logo,
            league_name, match_date, model_group,
            win_home, win_draw, win_away, over25_prob,
            top3_scores, lambda_home, lambda_away, handicap,
            odds_home, odds_draw, odds_away, odds_meta,
            jc_home, jc_draw, jc_away,
            jc_hhad_home, jc_hhad_draw, jc_hhad_away, jc_line, jc_meta,
            llm_win, llm_score, llm_win_pct,
            llm_brief, llm_core_data, llm_deep_report,
            llm_handicap, llm_over_under,
            llm_handicap_num, llm_handicap_team, llm_handicap_pct,
            llm_ou_line, llm_ou_type, llm_ou_pct,
            llm_injuries, llm_lineups,
            created_at, updated_at
        ) VALUES (
            :fid, :hname, :aname, :hlogo, :alogo,
            :lname, :mdate, :mgroup,
            :wh, :wd, :wa, :o25,
            :top3, :lh, :la, :hc,
            :oh, :od, :oa, :om,
            :jc1h, :jc1d, :jc1a,
            :jc2h, :jc2d, :jc2a, :jcline, :jcmeta,
            :lw, :ls, :lwp,
            :lb, :lcd, :ldr,
            :lhc, :lou,
            :hcn, :hct, :hcp,
            :oun, :out, :oup,
            :linj, :llup,
            :now, :now
        )
        ON DUPLICATE KEY UPDATE
            win_home=:wh, win_draw=:wd, win_away=:wa, over25_prob=:o25,
            top3_scores=:top3, lambda_home=:lh, lambda_away=:la, handicap=:hc,
            odds_home=COALESCE(odds_home, :oh), odds_draw=COALESCE(odds_draw, :od),
            odds_away=COALESCE(odds_away, :oa), odds_meta=COALESCE(odds_meta, :om),
            jc_home=COALESCE(jc_home, :jc1h), jc_draw=COALESCE(jc_draw, :jc1d),
            jc_away=COALESCE(jc_away, :jc1a),
            jc_hhad_home=COALESCE(jc_hhad_home, :jc2h),
            jc_hhad_draw=COALESCE(jc_hhad_draw, :jc2d),
            jc_hhad_away=COALESCE(jc_hhad_away, :jc2a),
            jc_line=COALESCE(jc_line, :jcline), jc_meta=COALESCE(jc_meta, :jcmeta),
            llm_win=:lw, llm_score=:ls, llm_win_pct=:lwp,
            llm_brief=:lb, llm_core_data=:lcd, llm_deep_report=:ldr,
            llm_handicap=:lhc, llm_over_under=:lou,
            llm_handicap_num=:hcn, llm_handicap_team=:hct, llm_handicap_pct=:hcp,
            llm_ou_line=:oun, llm_ou_type=:out, llm_ou_pct=:oup,
            llm_injuries=:linj, llm_lineups=:llup,
            model_group=:mgroup, updated_at=:now
    """), {
        'fid': fixture['id'],
        'hname': fixture['home_name'], 'aname': fixture['away_name'],
        'hlogo': fixture.get('home_logo'), 'alogo': fixture.get('away_logo'),
        'lname': fixture['league_name'], 'mdate': fixture['date'],
        'mgroup': model_group,
        'wh': xgb['win_home'], 'wd': xgb['win_draw'], 'wa': xgb['win_away'],
        'o25': xgb['over25_prob'],
        'top3': json.dumps(xgb['top3']),
        'lh': xgb['lambda_home'], 'la': xgb['lambda_away'],
        'hc': xgb['handicap'],
        'oh': pick.get('home'), 'od': pick.get('draw'), 'oa': pick.get('away'),
        'om': json.dumps(pick, ensure_ascii=False) if pick else None,
        'jc1h': j_had.get('h'), 'jc1d': j_had.get('d'), 'jc1a': j_had.get('a'),
        'jc2h': j_hhad.get('h'), 'jc2d': j_hhad.get('d'), 'jc2a': j_hhad.get('a'),
        'jcline': j_hhad.get('line'),
        'jcmeta': json.dumps(jc_pick, ensure_ascii=False) if jc_pick else None,
        'lw': llm.get('win'), 'ls': llm.get('score'), 'lwp': llm.get('win_pct'),
        'lb': llm.get('brief_analysis'), 'lcd': llm.get('core_data'),
        'ldr': llm.get('deep_report'),
        'lhc': _llm_handicap_str(llm), 'lou': _llm_ou_str(llm),
        'hcn': llm.get('handicap_num'), 'hct': llm.get('handicap_team'), 'hcp': llm.get('handicap_pct'),
        'oun': llm.get('ou_line'), 'out': llm.get('ou_type'), 'oup': llm.get('ou_pct'),
        'linj': injuries_text, 'llup': lineups_text,
        'now': datetime.now(),
    })

    # 模型对比日志：记录本次 LLM 模型输出（flash/pro 双跑对比用，按 (fixture_id, model) 更新）
    db.execute(text("""
        INSERT INTO model_compare_logs (
            fixture_id, model_name, match_date,
            llm_win, llm_win_pct, llm_score, llm_handicap, llm_over_under, llm_deep_report,
            created_at, updated_at
        ) VALUES (
            :fid, :mn, :mdate,
            :lw, :lwp, :ls, :lhc, :lou, :ldr,
            :now, :now
        )
        ON DUPLICATE KEY UPDATE
            llm_win=:lw, llm_win_pct=:lwp, llm_score=:ls,
            llm_handicap=:lhc, llm_over_under=:lou, llm_deep_report=:ldr,
            updated_at=:now
    """), {
        'fid': fixture['id'], 'mn': settings.deepseek_model, 'mdate': fixture['date'],
        'lw': llm.get('win'), 'lwp': llm.get('win_pct'), 'ls': llm.get('score'),
        'lhc': _llm_handicap_str(llm), 'lou': _llm_ou_str(llm),
        'ldr': llm.get('deep_report'), 'now': datetime.now(),
    })
    db.commit()
