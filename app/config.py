"""Environment-backed application configuration."""

import os

from app.stations import station_codes

DEFAULT_STATION_CODES = station_codes([
    "DBATE", "MHIDE", "GRGRD", "HWTHJ", "HOWTH", "CLSLA", "MYNTH", "DCDRA", "CTARF", "CNLLY",
    "TARA", "HSTON", "PERSE", "GCDK", "LDWNE", "HZLCH", "BROCK", "DLERY", "BRAY", "GSTNS",
])


class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://irishrail:irishrail@db:5432/irishrail"
    )
    IRISH_RAIL_API_URL = os.getenv(
        "IRISH_RAIL_API_URL",
        "http://api.irishrail.ie/realtime/realtime.asmx/getStationDataByCodeXML",
    )
    IRISH_RAIL_TRAINS_API_URL = os.getenv(
        "IRISH_RAIL_TRAINS_API_URL",
        "https://api.irishrail.ie/realtime/realtime.asmx/getCurrentTrainsXML",
    )
    STATION_CODES = station_codes(os.getenv("STATION_CODES", "").split(",")) or DEFAULT_STATION_CODES
    FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL_MINUTES", "5"))
    REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
