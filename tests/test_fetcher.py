"""Tests for Irish Rail XML parsing and observation persistence."""

from pathlib import Path

from app.extensions import db
from app.fetcher import parse_station_data, upsert_observations
from app.models import Observation

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_station_data_handles_api_edge_cases():
    """Namespaced XML preserves clean train data and terminating schedules."""
    observations = parse_station_data(
        (FIXTURES / "station_data_namespaced.xml").read_bytes(), "CNLLY"
    )

    assert observations == [
        {
            "station": "CNLLY",
            "train_code": "A123",
            "train_date": "2026-09-16",
            "origin": "Connolly",
            "destination": "Bray",
            "scheduled_time": "09:10",
            "delay_minutes": 7,
        },
        {
            "station": "CNLLY",
            "train_code": "T456",
            "train_date": "2026-09-16",
            "origin": "Heuston",
            "destination": "Heuston",
            "scheduled_time": "10:30",
            "delay_minutes": 0,
        },
    ]


def test_upsert_observations_inserts_and_deduplicates(app):
    """Polling the same train again updates its delay instead of adding a row."""
    observation = parse_station_data(
        (FIXTURES / "station_data_namespaced.xml").read_bytes(), "CNLLY"
    )[0]

    with app.app_context():
        assert upsert_observations([observation]) == 1
        observation["delay_minutes"] = 12
        assert upsert_observations([observation]) == 1
        saved = db.session.scalars(db.select(Observation)).all()

    assert len(saved) == 1
    assert saved[0].delay_minutes == 12
    assert saved[0].train_code == "A123"
