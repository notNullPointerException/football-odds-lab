from sqlalchemy.orm import Session
from loguru import logger

from app.core.apifootball import api_get, check_api_error
from app.core.config import settings
from app.models.league import League, Season
from app.models.player import Player, PlayerStats
from app.services.season_query import accessible_seasons, accessible_season_for_league


def _sync_league_players(db: Session, league_id: int, season_year: int) -> bool:
    """同步单个联赛单个赛季的球员。返回 False 表示被 API 拒绝或请求失败。"""
    logger.info(f"拉取联赛 {league_id} 赛季 {season_year} 的球员...")

    page = 1
    seen_players = set()
    failed = False

    while True:
        try:
            data = api_get(
                "/players",
                {"league": league_id, "season": season_year, "page": page},
                context=f"拉取联赛 {league_id} 赛季 {season_year} 球员 (page={page})",
            )
        except Exception as e:
            logger.error(
                f"拉取联赛 {league_id} 球员失败 (page={page}): {e}"
            )
            failed = True
            break

        # API 把拒绝藏在 HTTP 200 的 errors 字段里，不查就会被当成「没数据」静默跳过
        if check_api_error(data, f"拉取联赛 {league_id} 赛季 {season_year} 球员"):
            failed = True
            break

        items = data.get("response", [])
        if not items:
            break

        for item in items:
            player_raw = item.get("player", {})
            pid = player_raw.get("id")
            if not pid:
                continue

            # 球员去重 merge
            if pid not in seen_players:
                seen_players.add(pid)
                birth = player_raw.get("birth") or {}
                db.merge(
                    Player(
                        id=pid,
                        name=player_raw.get("name", ""),
                        firstname=player_raw.get("firstname", ""),
                        lastname=player_raw.get("lastname", ""),
                        age=player_raw.get("age"),
                        nationality=player_raw.get("nationality", ""),
                        height=player_raw.get("height", ""),
                        weight=player_raw.get("weight", ""),
                        injured=player_raw.get("injured", False),
                        photo=player_raw.get("photo", ""),
                        birth_date=birth.get("date"),
                        birth_place=birth.get("place", ""),
                        birth_country=birth.get("country", ""),
                    )
                )

            # 统计数据 (每个球员可能有多个 team 的统计)
            for stats in item.get("statistics", []):
                team_info = stats.get("team") or {}
                league_info = stats.get("league") or {}
                tid = team_info.get("id") or None
                lid = league_info.get("id") or league_id
                stat_season = league_info.get("season") or season_year

                # 按 (player_id, team_id, league_id, season) 去重
                stats_obj = (
                    db.query(PlayerStats)
                    .filter(
                        PlayerStats.player_id == pid,
                        PlayerStats.team_id == tid,
                        PlayerStats.league_id == lid,
                        PlayerStats.season == stat_season,
                    )
                    .first()
                )
                if stats_obj:
                    stats_obj.games = stats.get("games")
                    stats_obj.substitutes = stats.get("substitutes")
                    stats_obj.shots = stats.get("shots")
                    stats_obj.goals = stats.get("goals")
                    stats_obj.passes = stats.get("passes")
                    stats_obj.tackles = stats.get("tackles")
                    stats_obj.duels = stats.get("duels")
                    stats_obj.dribbles = stats.get("dribbles")
                    stats_obj.fouls = stats.get("fouls")
                    stats_obj.cards = stats.get("cards")
                    stats_obj.penalty = stats.get("penalty")
                else:
                    db.add(
                        PlayerStats(
                            player_id=pid,
                            team_id=tid,
                            league_id=lid,
                            season=stat_season,
                            games=stats.get("games"),
                            substitutes=stats.get("substitutes"),
                            shots=stats.get("shots"),
                            goals=stats.get("goals"),
                            passes=stats.get("passes"),
                            tackles=stats.get("tackles"),
                            duels=stats.get("duels"),
                            dribbles=stats.get("dribbles"),
                            fouls=stats.get("fouls"),
                            cards=stats.get("cards"),
                            penalty=stats.get("penalty"),
                        )
                    )

        db.commit()

        paging = data.get("paging", {})
        total_pages = paging.get("total", 1)
        if page >= total_pages:
            break
        page += 1
        logger.debug(f"联赛 {league_id} 球员翻页 {page}/{total_pages}")

    if not failed:
        logger.info(f"联赛 {league_id} 球员同步完成")
    return not failed


def sync_players_by_league(db: Session, league_id: int) -> None:
    """按指定联赛ID同步球员"""
    if not settings.api_football_key:
        logger.warning("API_FOOTBALL_KEY 未配置，跳过球员同步")
        return

    season = accessible_season_for_league(db, league_id)
    if not season:
        logger.warning(f"联赛 {league_id} 没有当前赛季")
        return

    _sync_league_players(db, league_id, season.year)


def sync_players(db: Session) -> None:
    """遍历已启用联赛的当前赛季，拉取球员和统计数据"""
    if not settings.api_football_key:
        logger.warning("API_FOOTBALL_KEY 未配置，跳过球员同步")
        return

    # 遍历已启用联赛的最近可访问赛季(Free 计划限 2022-2024)
    seasons = accessible_seasons(db)
    if not seasons:
        logger.warning("没有找到当前赛季，跳过球员同步")
        return

    rejected = 0
    for season in seasons:
        if not _sync_league_players(db, season.league_id, season.year):
            rejected += 1

    if rejected:
        logger.warning(
            f"球员同步完成：共 {len(seasons)} 个联赛，其中 {rejected} 个被 API 拒绝/失败"
        )
    else:
        logger.info(f"球员同步完成：{len(seasons)} 个联赛全部正常")
