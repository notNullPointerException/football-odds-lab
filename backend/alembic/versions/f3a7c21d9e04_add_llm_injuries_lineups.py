"""add_llm_injuries_lineups

Revision ID: f3a7c21d9e04
Revises: b6d2a8c9f001
Create Date: 2026-09-29 10:12:00.000000

伤停/首发是每次预测都拉了、拼进 LLM prompt 的实时情报，但以前只活在 prompt 里，
预测完就丢。加两列冻住「模型当时到底看见了什么」，方便赛后复盘。

用 Text 不用 varchar：国家队伤停名单动辄十几条，llm_brief 那种 String(200) 会截断。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a7c21d9e04'
down_revision: Union[str, None] = 'b6d2a8c9f001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('predictions', sa.Column('llm_injuries', sa.Text(), nullable=True))
    op.add_column('predictions', sa.Column('llm_lineups', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('predictions', 'llm_lineups')
    op.drop_column('predictions', 'llm_injuries')
