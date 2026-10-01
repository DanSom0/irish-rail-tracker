"""Environment-backed application configuration."""

import os
from datetime import timedelta

from app.stations import station_codes

DEFAULT_STATION_CODES = station_codes([
    "DBATE", "MHIDE", "GRGRD", "HWTHJ", "HOWTH", "CLSLA", "MYNTH", "DCDRA", "CTARF", "CNLLY",
    "TARA", "HSTON", "PERSE", "GCDK", "LDWNE", "HZLCH", "BROCK", "DLERY", "BRAY", "GSTNS",
])


def max_update_age(fetch_interval_minutes: int) -> timedelta:
    """How old the worker's last cycle, or a station's last successful poll, may be before it
    counts as not current: three missed polls, and never less than 15 minutes.

    The one threshold for /health/ingestion readiness, stale boards and coverage.
    """
    return max(timedelta(minutes=15), 3 * timedelta(minutes=fetch_interval_minutes))


class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://irishrail:irishrail@db:5432/irishrail"
    )
    IRISH_RAIL_API_URL = os.getenv(
        "IRISH_RAIL_API_URL",
        "https://api.irishrail.ie/realtime/realtime.asmx/getStationDataByCodeXML",
    )
    IRISH_RAIL_TRAINS_API_URL = os.getenv(
        "IRISH_RAIL_TRAINS_API_URL",
        "https://api.irishrail.ie/realtime/realtime.asmx/getCurrentTrainsXML",
    )
    STATION_CODES = station_codes(os.getenv("STATION_CODES", "").split(",")) or DEFAULT_STATION_CODES
    FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL_MINUTES", "5"))
    REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
    # Train positions are fetched during a web request, so a hung feed must not hold a Gunicorn worker for long.
    TRAINS_REQUEST_TIMEOUT_SECONDS = float(os.getenv("TRAINS_REQUEST_TIMEOUT_SECONDS", "3"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # The commit SHA of the running release, set by production Compose from IMAGE_TAG.
    RELEASE_SHA = os.getenv("RELEASE_SHA") or None
