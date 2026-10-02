"""预测相关 API 端点"""

import asyncio
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Query, Depends
from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.context_service import EMPTY_CTX, context_for_fixtures
# 从 prediction.errors 拿：这个模块只有 stdlib，import 它不会把 pandas/xgboost 拖起来
# （predict.predict 才是重的，那个仍然留在 _run() 里惰性导入）。
from prediction.errors import PredictError

router = APIRouter()


def _date_to_utc_range(date_str: str) -> tuple[str, str]:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    # 分界时间为当天 10:10（含）
    start = d + timedelta(hours=10, minutes=10)
    end   = d + timedelta(hours=34, minutes=10)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _jc_block(d: dict) -> dict | None:
    """体彩竞彩盘（实际下注的价，水位 ~11.4%）。devig/payback 由 jc.py 算好存在 jc_meta 里，这里只做搬运。"""
    meta = d.get("jc_meta") or {}
    if not meta:
        return None

    def pool(p):
        if not p:
            return None
        out = {"odds": {"home": p.get("h"), "draw": p.get("d"), "away": p.get("a")},
               "payback": p.get("payback"), "devig": p.get("devig")}
        if "line" in p:
            out["line"] = p.get("line")
            out["side"] = p.get("side")
        return out

    return {"match_num": meta.get("match_num"),
            "had": pool(meta.get("had")), "hhad": pool(meta.get("hhad"))}


@router.post("/predict/{fixture_id}")
async def predict_match(fixture_id: int):
    """手动触发单场预测。

    失败分两种，页面上长得不一样，别混：
      502 + 具体原因 = 该场预测【跑了但没跑成】（LLM 超时 / 联赛没模型 / 特征不足）→ 可重跑
      400 "无需预测"  = 【压根不该跑】（查无此场 / 命中黑名单）→ 重跑也没用
    """
    def _run():
        from prediction.predict import predict_fixture
        return predict_fixture(fixture_id)

    try:
        result = await asyncio.to_thread(_run)
    except PredictError as e:
        logger.warning(f"预测失败 fixture={fixture_id} stage={e.stage}: {e.reason}")
        raise HTTPException(status_code=502, detail=e.reason)
    if result is None:
        raise HTTPException(status_code=400, detail="无需预测（比赛不存在或已拉黑）")
    return {"status": "ok", "fixture_id": fixture_id, "result": result}


@router.get("/predictions")
async def get_predictions(
    date: str | None = Query(None),
    category: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return await asyncio.to_thread(
        _get_predictions_sync, db, date, category, page, page_size
    )


def _get_predictions_sync(db, date, category, page, page_size):
    try:
        conditions = []
        params: dict = {}
        if date:
            utc_start, utc_end = _date_to_utc_range(date)
            conditions.append("p.match_date >= :utc_start AND p.match_date < :utc_end")
            params["utc_start"] = utc_start
            params["utc_end"] = utc_end
        if category:
            conditions.append("f.category = :category")
            params["category"] = category

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        total = db.execute(
            text(f"SELECT COUNT(*) FROM predictions p LEFT JOIN fixtures f ON p.fixture_id = f.id {where}"),
            params
        ).scalar()

        # JOIN 源表（leagues / teams）以使用 _zh 中文字段兜底
        rows = db.execute(text(f"""
            SELECT p.fixture_id, p.match_date, p.model_group,
                   p.win_home, p.win_draw, p.win_away, p.over25_prob,
                   p.top3_scores, p.lambda_home, p.lambda_away, p.handicap,
                   p.odds_home, p.odds_draw, p.odds_away, p.odds_meta,
                   p.jc_home, p.jc_draw, p.jc_away,
                   p.jc_hhad_home, p.jc_hhad_draw, p.jc_hhad_away, p.jc_line, p.jc_meta,
                   p.llm_win, p.llm_score, p.llm_win_pct,
                   p.llm_brief, p.llm_core_data, p.llm_deep_report,
                   p.llm_handicap, p.llm_over_under,
                   p.llm_handicap_num, p.llm_handicap_team, p.llm_handicap_pct,
                   p.llm_ou_line, p.llm_ou_type, p.llm_ou_pct,
                   p.llm_injuries, p.llm_lineups,
                   p.home_logo, p.away_logo,
                   COALESCE(ht.name_zh, p.home_name) AS home_name,
                   COALESCE(at.name_zh, p.away_name) AS away_name,
                   COALESCE(lg.name_zh, p.league_name) AS league_name,
                   f.status_short, f.category,
                   f.goals_home AS actual_h, f.goals_away AS actual_a,
                   p.win_correct, p.over25_correct, p.handicap_correct, p.score_in_top3
            FROM predictions p
            LEFT JOIN fixtures f ON p.fixture_id = f.id
            LEFT JOIN teams ht ON f.home_id = ht.id
            LEFT JOIN teams at ON f.away_id = at.id
            LEFT JOIN leagues lg ON f.league_id = lg.id
            {where}
            ORDER BY p.match_date DESC
            LIMIT :limit OFFSET :offset
        """), {**params, "limit": page_size, "offset": (page - 1) * page_size}).fetchall()

        # ctx 赛前背景（展示层新增）：每页批量补 休息天数/xG5/H2H；失败兜底不挡卡片
        try:
            ctx_map = context_for_fixtures(db, [r.fixture_id for r in rows if r.fixture_id])
        except Exception:
            ctx_map = {}

        import json as _json
        data = []
        for r in rows:
            d = dict(r._mapping)
            for field in ('top3_scores', 'odds_meta', 'jc_meta'):
                if isinstance(d.get(field), str):
                    try:
                        d[field] = _json.loads(d[field])
                    except Exception:
                        pass

            record = {
                "basic": {
                    "fixture_id": d.get("fixture_id"),
                    "home_name": d.get("home_name"),
                    "away_name": d.get("away_name"),
                    "home_logo": d.get("home_logo"),
                    "away_logo": d.get("away_logo"),
                    "league_name": d.get("league_name"),
                    "match_date": d["match_date"].isoformat() if d.get("match_date") else None,
                    "status_short": d.get("status_short"),
                    "category": d.get("category"),
                },
                "xgb": {
                    "model_group": d.get("model_group"),
                    "prob": {
                        "home": d.get("win_home"),
                        "draw": d.get("win_draw"),
                        "away": d.get("win_away"),
                    },
                    "over25": {
                        "over": d.get("over25_prob"),
                        "under": 1 - (d.get("over25_prob") or 0),
                    } if d.get("over25_prob") is not None else None,
                    "lambda": {
                        "home": d.get("lambda_home"),
                        "away": d.get("lambda_away"),
                    } if d.get("lambda_home") is not None else None,
                    "top3": d.get("top3_scores"),
                    "handicap": d.get("handicap"),
                },
                # market：预测当时的两种盘，口径不同别混用 ——
                #   顶层 odds/devig/payback = 庄家盘（Pinnacle 优先，水位 ~4-5%），sharp 参考
                #   jc.*                    = 体彩竞彩盘（水位 ~11.4%），实际下注的价
                "market": {
                    "bet": (d.get("odds_meta") or {}).get("bet"),
                    "bookmaker": (d.get("odds_meta") or {}).get("bookmaker"),
                    "snapshot": (d.get("odds_meta") or {}).get("snapshot"),
                    "line": (d.get("odds_meta") or {}).get("line"),
                    "odds": {
                        "home": d.get("odds_home"),
                        "draw": d.get("odds_draw"),
                        "away": d.get("odds_away"),
                    },
                    "devig": (d.get("odds_meta") or {}).get("devig"),
                    "payback": (d.get("odds_meta") or {}).get("payback"),
                    "jc": _jc_block(d),
                } if (d.get("odds_meta") or d.get("odds_home") is not None
                       or d.get("jc_meta")) else None,
                "llm": {
                    "win": d.get("llm_win"),
                    "win_pct": d.get("llm_win_pct"),
                    "score": d.get("llm_score"),
                    "handicap": d.get("llm_handicap"),
                    "handicap_num": d.get("llm_handicap_num"),
                    "handicap_team": d.get("llm_handicap_team"),
                    "handicap_pct": d.get("llm_handicap_pct"),
                    "over_under": d.get("llm_over_under"),
                    "ou_line": d.get("llm_ou_line"),
                    "ou_type": d.get("llm_ou_type"),
                    "ou_pct": d.get("llm_ou_pct"),
                    "brief": d.get("llm_brief"),
                    "core_data": d.get("llm_core_data"),
                    "deep_report": d.get("llm_deep_report"),
                    # 喂进 prompt 的伤停/首发原文（纯文本，不走上面那三行的 JSON 反序列化）
                    "injuries": d.get("llm_injuries"),
                    "lineups": d.get("llm_lineups"),
                },
                "result": {
                    "score": f"{d['actual_h']}-{d['actual_a']}" if d.get("actual_h") is not None else None,
                    "win_correct": d.get("win_correct"),
                    "over25_correct": d.get("over25_correct"),
                    "handicap_correct": d.get("handicap_correct"),
                    "top3_correct": d.get("score_in_top3"),
                } if d.get("actual_h") is not None else None,
            }
            record["ctx"] = ctx_map.get(d.get("fixture_id"), EMPTY_CTX)
            data.append(record)

        return {"data": data, "total": total, "page": page, "page_size": page_size}
    finally:
        pass
