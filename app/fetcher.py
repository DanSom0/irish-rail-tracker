"""Irish Rail realtime API client and database writer."""

import logging
from datetime import UTC, datetime
from xml.etree import ElementTree

import requests
from sqlalchemy.dialects.postgresql import insert

from app.extensions import db
from app.models import Observation
from app.xml_helpers import child_text, find_rows

logger = logging.getLogger(__name__)


def _normalise_train_date(train_date: str) -> str:
    """Convert the API's ``16 Sep 2026`` format to a stable deduplication key."""
    return datetime.strptime(train_date, "%d %b %Y").replace(tzinfo=UTC).date().isoformat()


def parse_station_data(xml_body: bytes, station: str) -> list[dict[str, object]]:
    """Parse API XML into database-ready observations without propagating bad XML."""
    try:
        root = ElementTree.fromstring(xml_body)
    except ElementTree.ParseError:
        logger.warning("invalid station XML", exc_info=True, extra={"station": station})
        return []
    rows = find_rows(root, "objStationData")
    logger.info("station XML parsed", extra={"station": station, "entries_parsed": len(rows)})
    observations: list[dict[str, object]] = []
    for row in rows:
        train_code = child_text(row, "Traincode")
        raw_train_date = child_text(row, "Traindate")
        scheduled_departure = child_text(row, "Schdepart")
        scheduled_arrival = child_text(row, "Scharrival")
        scheduled_time = (
            scheduled_arrival if scheduled_departure == "00:00" else scheduled_departure
        )
        if not all((train_code, raw_train_date, scheduled_time)):
            logger.warning("skipping incomplete train record", extra={"station": station})
            continue
        try:
            train_date = _normalise_train_date(raw_train_date)
        except ValueError:
            logger.warning(
                "skipping invalid train date",
                extra={"station": station, "train_code": train_code, "train_date": raw_train_date},
            )
            continue
        try:
            delay_minutes = int(child_text(row, "Late") or "0")
        except ValueError:
            logger.warning("skipping invalid delay", extra={"station": station, "train_code": train_code})
            continue
        observations.append(
            {
                "station": station,
                "train_code": train_code,
                "train_date": train_date,
                "origin": child_text(row, "Origin") or "Unknown",
                "destination": child_text(row, "Destination") or "Unknown",
                "scheduled_time": scheduled_time,
                "delay_minutes": delay_minutes,
            }
        )
    return observations


def fetch_station(station: str, api_url: str, timeout: float) -> list[dict[str, object]]:
    """Fetch and parse realtime data for one station without propagating request errors."""
    try:
        response = requests.get(api_url, params={"StationCode": station}, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        logger.warning("station API request failed", exc_info=True, extra={"station": station})
        return []
    if not response.content.strip():
        logger.warning("empty API response", extra={"station": station})
        return []
    observations = parse_station_data(response.content, station)
    logger.info(
        "station fetch succeeded",
        extra={"station": station, "observations_received": len(observations)},
    )
    return observations


def upsert_observations(observations: list[dict[str, object]]) -> int:
    """Save observations, replacing a previously stored delay for the train."""
    if not observations:
        return 0
    fetched_at = datetime.now(UTC)
    statement = insert(Observation).values(
        [{**observation, "fetched_at": fetched_at} for observation in observations]
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_observation_train",
        set_={
            "origin": statement.excluded.origin,
            "destination": statement.excluded.destination,
            "scheduled_time": statement.excluded.scheduled_time,
            "delay_minutes": statement.excluded.delay_minutes,
            "fetched_at": statement.excluded.fetched_at,
        },
    )
    db.session.execute(statement)
    db.session.commit()
    return len(observations)


def run_fetch_cycle(app) -> int:
    """Fetch all configured stations; errors remain isolated to each station."""
    saved = 0
    for station in app.config["STATION_CODES"]:
        try:
            saved += upsert_observations(
                fetch_station(
                    station,
                    app.config["IRISH_RAIL_API_URL"],
                    app.config["REQUEST_TIMEOUT_SECONDS"],
                )
            )
        except (requests.RequestException, ElementTree.ParseError) as error:
            db.session.rollback()
            logger.exception("station fetch failed", extra={"station": station, "error": str(error)})
        except Exception:
            db.session.rollback()
            logger.exception("unexpected station fetch failure", extra={"station": station})
    logger.info("fetch cycle completed", extra={"observations_saved": saved})
    return saved
