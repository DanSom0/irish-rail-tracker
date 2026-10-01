"""Baseline: the observations schema that create_all built before migrations.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-28

Production already has these tables and is stamped with this revision instead of
running it (scripts/stamp-baseline.sh). Only a new, empty database runs this upgrade.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0001_baseline'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the schema in an empty database; refuse to build it over existing tables."""
    existing = sorted(set(sa.inspect(op.get_bind()).get_table_names()) - {'alembic_version'})
    if existing:
        raise RuntimeError(
            f"Database has tables ({', '.join(existing)}) but no Alembic revision. It was "
            "created before migrations: stamp it with scripts/stamp-baseline.sh "
            "(docs/operations.md) instead of running the baseline."
        )
    op.create_table('observations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('station', sa.String(length=32), nullable=False),
    sa.Column('train_code', sa.String(length=32), nullable=False),
    sa.Column('train_date', sa.String(length=32), nullable=False),
    sa.Column('origin', sa.String(length=128), nullable=False),
    sa.Column('destination', sa.String(length=128), nullable=False),
    sa.Column('scheduled_time', sa.String(length=16), nullable=False),
    sa.Column('delay_minutes', sa.Integer(), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('station', 'train_code', 'train_date', name='uq_observation_train')
    )
    op.create_index(op.f('ix_observations_fetched_at'), 'observations', ['fetched_at'], unique=False)
    op.create_index(op.f('ix_observations_station'), 'observations', ['station'], unique=False)


def downgrade() -> None:
    """Unsupported: it would drop every observation. Restore a backup instead."""
    raise RuntimeError(
        "Downgrading the baseline is not supported: it would drop all observations. "
        "Restore a backup instead (docs/operations.md)."
    )
