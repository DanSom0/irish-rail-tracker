"""Database models for rail delay observations, station polls and the worker heartbeat."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class Observation(db.Model):
    """Latest known delay for one train at one station on a train date."""

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("station", "train_code", "train_date", name="uq_observation_train"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    station: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    train_code: Mapped[str] = mapped_column(String(32), nullable=False)
    train_date: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(128), nullable=False)
    destination: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_time: Mapped[str] = mapped_column(String(16), nullable=False)
    delay_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class StationPoll(db.Model):
    """The latest poll of one station: an empty board or a failure is recorded too.

    A station's current trains are exactly the observations whose ``fetched_at`` equals
    ``succeeded_at``. A failure keeps ``succeeded_at``, so that set stays visible as stale.
    """

    __tablename__ = "station_polls"
    __table_args__ = (
        CheckConstraint(
            "(outcome = 'ok' AND train_count > 0 AND succeeded_at IS NOT NULL AND error_reason IS NULL)"
            " OR (outcome = 'empty' AND train_count = 0 AND succeeded_at IS NOT NULL"
            " AND error_reason IS NULL)"
            " OR (outcome = 'error' AND train_count IS NULL AND error_reason IS NOT NULL)",
            name="ck_station_polls_outcome",
        ),
    )

    station_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    train_count: Mapped[int | None] = mapped_column(Integer)
    error_reason: Mapped[str | None] = mapped_column(String(200))


class WorkerHeartbeat(db.Model):
    """Single row: when the worker last completed a fetch cycle, whatever the station outcomes."""

    __tablename__ = "worker_heartbeat"
    __table_args__ = (CheckConstraint("id = 1", name="ck_worker_heartbeat_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    last_cycle_completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
