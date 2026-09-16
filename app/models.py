"""Database models for rail delay observations."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
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
