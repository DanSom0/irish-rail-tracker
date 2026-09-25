"""Current train positions from Irish Rail, cached once per web process.

getCurrentTrainsXML has no origin, destination or position timestamp fields, and
running trains are reported at station coordinates rather than between them.
"""

import logging
from concurrent.futures import Future
from datetime import UTC, datetime
from math import isfinite
from threading import Lock
from time import monotonic
from xml.etree import ElementTree

import requests

from app.stations import STATION_COORDINATES
from app.xml_helpers import child_text, find_rows, has_child, local_name

logger = logging.getLogger(__name__)
FEED = "getCurrentTrainsXML"
RUNNING = "R"
TRAIN_CACHE_SECONDS = 60
# Added on every side of the monitored stations' bounding box: roughly 5.5 km
# north-south and 3.3 km east-west at Dublin's latitude.
TRAIN_BOUNDS_MARGIN_DEGREES = 0.05

Bounds = tuple[float, float, float, float]


class UpstreamFeedError(Exception):
    """Irish Rail answered, but not with a train positions document."""


def _public_message(raw: str, train_code: str) -> str:
    """Turn the feed's literal ``\\n`` separators into lines, dropping the repeated train code."""
    lines = [line.strip() for line in raw.replace("\\n", "\n").splitlines() if line.strip()]
    if lines and lines[0] == train_code:
        lines = lines[1:]
    return "\n".join(lines)


def parse_train_positions(xml_body: bytes) -> tuple[list[dict], dict[str, int]]:
    """Parse running trains; malformed individual records are logged and skipped."""
    root = ElementTree.fromstring(xml_body)
    if local_name(root.tag) != "ArrayOfObjTrainPositions":
        raise UpstreamFeedError(f"unexpected root element {local_name(root.tag)!r}")
    rows = find_rows(root, "objTrainPositions")
    trains = []
    running = 0
    for row in rows:
        if child_text(row, "TrainStatus") != RUNNING:
            continue
        running += 1
        code = child_text(row, "TrainCode")
        raw_latitude, raw_longitude = child_text(row, "TrainLatitude"), child_text(row, "TrainLongitude")
        try:
            latitude, longitude = float(raw_latitude), float(raw_longitude)
        except ValueError:
            latitude = longitude = float("nan")
        if not code or not (isfinite(latitude) and isfinite(longitude)) or not (
            -90 <= latitude <= 90 and -180 <= longitude <= 180
        ):
            logger.warning(
                "skipping malformed train position",
                extra={"feed": FEED, "train_code": code, "latitude": raw_latitude, "longitude": raw_longitude},
            )
            continue
        if latitude == 0 and longitude == 0:
            # The feed reports 0,0 for some running trains that have no known position.
            logger.info("skipping train without reported position", extra={"feed": FEED, "train_code": code})
            continue
        train = {
            "train_code": code,
            "latitude": latitude,
            "longitude": longitude,
            "direction": child_text(row, "Direction") or None,
            "public_message": _public_message(child_text(row, "PublicMessage"), code),
        }
        # Only present when the feed has the field, so the UI can tell "empty" from "not provided".
        for field, key in (("Origin", "origin"), ("Destination", "destination")):
            if has_child(row, field):
                train[key] = child_text(row, field) or None
        trains.append(train)
    return trains, {"entries_parsed": len(rows), "trains_running": running}


def monitored_bounds(monitored: list[str] | tuple[str, ...]) -> Bounds | None:
    """South, north, west and east edges around the monitored stations, with the margin."""
    coordinates = [STATION_COORDINATES[code] for code in monitored if code in STATION_COORDINATES]
    if not coordinates:
        return None
    margin = TRAIN_BOUNDS_MARGIN_DEGREES
    return (
        min(point[0] for point in coordinates) - margin,
        max(point[0] for point in coordinates) + margin,
        min(point[1] for point in coordinates) - margin,
        max(point[1] for point in coordinates) + margin,
    )


def trains_in_bounds(trains: list[dict], bounds: Bounds) -> list[dict]:
    # A rectangle also catches nearby unrelated lines (for example Maynooth or Kildare
    # services); it is a geographic filter, not a claim that a train serves a monitored station.
    south, north, west, east = bounds
    return [train for train in trains
            if south <= train["latitude"] <= north and west <= train["longitude"] <= east]


class TrainPositionsCache:
    """Caches successes and failures; concurrent callers share one upstream request."""

    def __init__(self, clock=monotonic):
        self._clock = clock
        self._lock = Lock()
        self._inflight: Future | None = None
        self._cached: dict | None = None
        self._last_good: dict | None = None
        self._expires = 0.0

    def get(self, url: str, timeout: float, bounds: Bounds) -> dict:
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
            result = self._fetch(url, timeout, bounds)
        except Exception as error:
            # Programming errors are not cached: they reach the caller and Flask's error log.
            with self._lock:
                self._inflight = None
                future.set_exception(error)
            raise

        with self._lock:
            self._cached = result
            self._expires = self._clock() + TRAIN_CACHE_SECONDS
            if result["status"] == "ok":
                self._last_good = result
            self._inflight = None
            future.set_result(result)
        return result

    def _fetch(self, url: str, timeout: float, bounds: Bounds) -> dict:
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException:
            logger.warning("train positions API request failed", exc_info=True, extra={"feed": FEED})
            return self._failure()
        if not response.content.strip():
            logger.warning("empty API response", extra={"feed": FEED})
            return self._failure()
        try:
            trains, counts = parse_train_positions(response.content)
        except (ElementTree.ParseError, UpstreamFeedError):
            logger.warning("invalid train positions XML", exc_info=True, extra={"feed": FEED})
            return self._failure()
        kept = trains_in_bounds(trains, bounds)
        logger.info("train positions parsed", extra={"feed": FEED, **counts, "trains_in_bounds": len(kept)})
        return {"status": "ok", "trains": kept, "fetched_at": datetime.now(UTC).isoformat(timespec="seconds")}

    def _failure(self) -> dict:
        if self._last_good is not None:
            return {**self._last_good, "status": "stale"}
        return {"status": "error", "reason": "Train positions are unavailable.", "trains": [], "fetched_at": None}


cache = TrainPositionsCache()


def current_trains(url: str, timeout: float, monitored: list[str] | tuple[str, ...]) -> dict:
    bounds = monitored_bounds(monitored)
    if bounds is None:
        logger.warning("no monitored station has coordinates", extra={"feed": FEED, "stations": list(monitored)})
        return {
            "status": "error",
            "reason": "No monitored station has map coordinates.",
            "trains": [],
            "fetched_at": None,
        }
    return cache.get(url, timeout, bounds)
