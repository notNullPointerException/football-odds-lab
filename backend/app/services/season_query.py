"""赛季选择工具：API-Football Pro 计划支持全部赛季

各同步服务按 Season.is_current 拉取当前赛季(2026)，
Pro 计划(2026-08-17 订阅)已放开全部赛季访问。
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.league import League, Season

# 可访问的赛季范围（Pro 计划全量放开）
ACCESSIBLE_YEAR_MIN = 2022
ACCESSIBLE_YEAR_MAX = 2026


def accessible_seasons(db: Session):
    """返回每个已启用联赛最近可访问的赛季(2022-2026)，替代 is_current"""
    max_year = (
        db.query(Season.league_id, func.max(Season.year).label("max_year"))
        .join(League)
        .filter(
            League.enabled == True,
            Season.year >= ACCESSIBLE_YEAR_MIN,
            Season.year <= ACCESSIBLE_YEAR_MAX,
        )
        .group_by(Season.league_id)
        .subquery()
    )
    return (
        db.query(Season)
        .join(
            max_year,
            (Season.league_id == max_year.c.league_id)
            & (Season.year == max_year.c.max_year),
        )
        .all()
    )


def accessible_season_for_league(db: Session, league_id: int):
    """返回指定联赛最近可访问的赛季"""
    return (
        db.query(Season)
        .filter(
            Season.league_id == league_id,
            Season.year >= ACCESSIBLE_YEAR_MIN,
            Season.year <= ACCESSIBLE_YEAR_MAX,
        )
        .order_by(Season.year.desc())
        .first()
    )
