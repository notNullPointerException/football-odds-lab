"""补拉指定联赛的球员数据。

用法：
    python backfill_players.py               # 跑默认清单
    python backfill_players.py 11 17 39 61   # 只补这几个联赛
"""
import sys
import time

from loguru import logger

from app.core.apifootball import api_get
from app.db.session import SessionLocal
from app.services.player_service import _sync_league_players
from app.services.season_query import accessible_season_for_league

# 2026-08-17 首次同步时 DNS 失败漏掉、历史未覆盖的联赛（默认清单）
DEFAULT_MISSING = [2, 3, 17, 18, 39, 40, 41, 45, 61, 78, 79, 94, 106, 135,
                   140, 141, 144, 188, 197, 203, 204, 235, 274, 283, 296,
                   307, 323, 333, 848]


def probe_season(lid: int, year: int) -> bool:
    """探测该赛季是否有球员数据"""
    try:
        data = api_get(
            "/players",
            {"league": lid, "season": year, "page": 1},
            context=f"探测联赛 {lid} season={year}",
            attempts=2,
        )
        return data.get("results", 0) > 0
    except Exception as e:
        logger.warning(f"探测联赛 {lid} season={year} 失败: {e}")
        return False


def main():
    leagues = [int(a) for a in sys.argv[1:]] or DEFAULT_MISSING
    db = SessionLocal()
    ok = fail = 0
    try:
        for lid in leagues:
            season = accessible_season_for_league(db, lid)
            if not season:
                logger.warning(f"联赛 {lid} 无可用赛季，跳过")
                fail += 1
                continue
            year = season.year
            if not probe_season(lid, year):
                fb = year - 1
                logger.info(f"联赛 {lid} season={year} 无数据，fallback 到 {fb}")
                if not probe_season(lid, fb):
                    logger.warning(f"联赛 {lid} season={fb} 也无数据，跳过")
                    fail += 1
                    continue
                year = fb
            try:
                logger.info(f"开始补拉联赛 {lid} season={year}")
                if not _sync_league_players(db, lid, year):
                    logger.error(f"联赛 {lid} 补拉失败（API 拒绝/重试耗尽）")
                    fail += 1
                    continue
                ok += 1
                logger.info(f"联赛 {lid} 补拉完成 ({ok}/{len(leagues)})")
            except Exception as e:
                logger.error(f"联赛 {lid} 补拉异常: {e}")
                fail += 1
            time.sleep(0.8)
    finally:
        db.close()
    logger.info(f"补拉结束: 成功 {ok}, 失败 {fail}, 共 {len(leagues)}")


if __name__ == "__main__":
    main()
