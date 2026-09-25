"""Current train positions from Irish Rail, cached once per web process."""

import logging
from concurrent.futures import Future
from datetime import UTC, datetime
from math import isfinite
from threading import Lock
from time import monotonic
from xml.etree import ElementTree

import requests

from app.fetcher import _text
from app.stations import STATION_COORDINATES

logger = logging.getLogger(__name__)
TRAIN_CACHE_SECONDS = 60
TRAIN_BOUNDS_MARGIN_DEGREES = 0.05


def parse_train_positions(xml_body: bytes) -> list[dict]:
    """Parse reported running positions; bad individual records do not hide good ones."""
    root = ElementTree.fromstring(xml_body)
    trains = []
    for row in root.iter():
        if row.tag.rsplit("}", 1)[-1] != "objTrainPositions" or _text(row, "TrainStatus") != "R":
            continue
        code = _text(row, "TrainCode")
        try:
            latitude = float(_text(row, "TrainLatitude"))
            longitude = float(_text(row, "TrainLongitude"))
        except ValueError:
            logger.warning("skipping malformed train position", extra={"train_code": code})
            continue
        if not code or not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or not (
            isfinite(latitude) and isfinite(longitude)
        ):
            logger.warning("skipping malformed train position", extra={"train_code": code})
            continue
        trains.append({
            "train_code": code,
            "latitude": latitude,
            "longitude": longitude,
            "direction": _text(row, "Direction"),
            "public_message": _text(row, "PublicMessage"),
            "origin": _text(row, "Origin") or None,
            "destination": _text(row, "Destination") or None,
        })
    return trains


def trains_in_bounds(trains: list[dict], monitored: list[str] | tuple[str, ...]) -> list[dict]:
    coordinates = [STATION_COORDINATES[code] for code in monitored if code in STATION_COORDINATES]
    if not coordinates:
        return []
    south, north = min(point[0] for point in coordinates), max(point[0] for point in coordinates)
    west, east = min(point[1] for point in coordinates), max(point[1] for point in coordinates)
    margin = TRAIN_BOUNDS_MARGIN_DEGREES
    # A rectangular bound can include nearby unrelated lines; it is a geographic hint, not a route claim.
    return [train for train in trains if south - margin <= train["latitude"] <= north + margin
            and west - margin <= train["longitude"] <= east + margin]


class TrainPositionsCache:
    def __init__(self, clock=monotonic):
        self._clock = clock
        self._lock = Lock()
        self._inflight: Future | None = None
        self._cached: dict | None = None
        self._last_good: dict | None = None
        self._expires = 0.0

    def get(self, url: str, timeout: float) -> dict:
        with self._lock:
            if self._cached is not None and self._clock() < self._expires:
                return self._cached
            owner = self._inflight is None
            if owner:
                self._inflight = Future()
            future = self._inflight
        if not owner:
            return future.result()

        try:
            try:
                response = requests.get(url, timeout=timeout)
                response.raise_for_status()
                trains = parse_train_positions(response.content)
            except (requests.RequestException, ElementTree.ParseError):
                logger.warning("train positions fetch failed", exc_info=True)
                result = ({**self._last_good, "status": "stale"} if self._last_good else
                          {"status": "error", "trains": [], "fetched_at": None})
            else:
                result = {"status": "ok", "trains": trains, "fetched_at": datetime.now(UTC).isoformat()}
        except Exception as error:
            with self._lock:
                future.set_exception(error)
                self._inflight = None
            raise

        with self._lock:
            self._cached = result
            self._expires = self._clock() + TRAIN_CACHE_SECONDS
            if result["status"] == "ok":
                self._last_good = result
            future.set_result(result)
            self._inflight = None
        return result


cache = TrainPositionsCache()
