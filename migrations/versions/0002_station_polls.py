"""Record each station's latest poll outcome and the worker's heartbeat.

Revision ID: 0002_station_polls
Revises: 0001_baseline
Create Date: 2026-09-28

Additive only: two new tables, and observations is unchanged, so the previous release
keeps working against the migrated database (rollback never runs migrations).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002_station_polls'
down_revision: str | Sequence[str] | None = '0001_baseline'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('station_polls',
    sa.Column('station_code', sa.String(length=32), nullable=False),
    sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('succeeded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.String(length=8), nullable=False),
    sa.Column('train_count', sa.Integer(), nullable=True),
    sa.Column('error_reason', sa.String(length=200), nullable=True),
    sa.CheckConstraint(
        "(outcome = 'ok' AND train_count > 0 AND succeeded_at IS NOT NULL AND error_reason IS NULL)"
        " OR (outcome = 'empty' AND train_count = 0 AND succeeded_at IS NOT NULL"
        " AND error_reason IS NULL)"
        " OR (outcome = 'error' AND train_count IS NULL AND error_reason IS NOT NULL)",
        name='ck_station_polls_outcome'),
    sa.PrimaryKeyConstraint('station_code')
    )
    op.create_table('worker_heartbeat',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('last_cycle_completed_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('id = 1', name='ck_worker_heartbeat_single_row'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Local use only; production never downgrades. Observations are untouched."""
    op.drop_table('worker_heartbeat')
    op.drop_table('station_polls')
