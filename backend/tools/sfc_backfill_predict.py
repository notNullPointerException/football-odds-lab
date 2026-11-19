"""
胜负彩缺失预测补跑脚本

功能：
  找出 sfc_issue_matches 中 prob_home IS NULL 的场次，
  如果对应 fixture 还没有预测记录，就调用 predict_fixture 补跑，
  然后把结果回写到 sfc_issue_matches。

用法（在 backend 目录下）：
  python tools/sfc_backfill_predict.py              # 补最近 30 期
  python tools/sfc_backfill_predict.py --issues 50  # 补最近 50 期
  python tools/sfc_backfill_predict.py --dry-run    # 只看不跑
"""

import os
import sys
import argparse
from datetime import datetime

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import text
from loguru import logger

from app.db.session import SessionLocal
from prediction.predict import predict_fixture


def get_missing_fixtures(db, limit_issues=30):
    """找出最近若干期中，有 fixture_id 但 prob_home 为空的场次"""
    rows = db.execute(text("""
        SELECT m.issue, m.no, m.fixture_id, m.home, m.away
        FROM sfc_issue_matches m
        WHERE m.issue IN (
            SELECT issue FROM (
                SELECT DISTINCT issue
                FROM sfc_issue_matches
                WHERE fixture_id IS NOT NULL
                ORDER BY issue DESC
                LIMIT :lim
            ) t
        )
          AND m.prob_home IS NULL
          AND m.fixture_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM predictions p WHERE p.fixture_id = m.fixture_id
          )
        ORDER BY m.issue DESC, m.no
    """), {"lim": limit_issues}).fetchall()
    return rows


def backfill(limit_issues=30, dry_run=False):
    db = SessionLocal()
    try:
        missing = get_missing_fixtures(db, limit_issues)
        logger.info(f"找到 {len(missing)} 场需要补预测（最近 {limit_issues} 期）")

        if not missing:
            logger.info("没有需要补的场次")
            return

        success = 0
        failed = 0
        for i, row in enumerate(missing, 1):
            issue, no, fid, home, away = row
            logger.info(f"[{i}/{len(missing)}] {issue} 第{no}场 {home} vs {away} (fixture={fid})")

            if dry_run:
                continue

            try:
                result = predict_fixture(fid, db=db)
                if result:
                    success += 1
                    logger.info(f"  预测成功")
                else:
                    failed += 1
                    logger.warning(f"  预测返回 None")
            except Exception as e:
                failed += 1
                logger.warning(f"  预测失败: {e}")

        logger.info(f"完成：成功 {success}，失败 {failed}")

        if not dry_run and success > 0:
            logger.info("预测已写入 predictions 表。")
            logger.info("如需同步到 sfc_issue_matches，请再跑一次胜负彩存档任务，或手动触发 archive。")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--issues", type=int, default=30, help="补最近多少期")
    parser.add_argument("--dry-run", action="store_true", help="只列出，不实际预测")
    args = parser.parse_args()

    backfill(limit_issues=args.issues, dry_run=args.dry_run)
