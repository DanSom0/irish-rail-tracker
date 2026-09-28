"""Irish Rail realtime API client and database writer for station polls."""

import logging
from datetime import UTC, datetime
from xml.etree import ElementTree

import requests
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import Observation, StationPoll, WorkerHeartbeat
from app.xml_helpers import child_text, find_rows, local_name

logger = logging.getLogger(__name__)


def _normalise_train_date(train_date: str) -> str:
    """Convert the API's ``16 Sep 2026`` format to a stable deduplication key."""
    return datetime.strptime(train_date, "%d %b %Y").replace(tzinfo=UTC).date().isoformat()


class StationPollError(Exception):
    """A failed station poll. ``reason`` is short and safe to store: no URLs or stack traces."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_station_data(xml_body: bytes, station: str) -> list[dict[str, object]]:
    """Parse API XML into database-ready observations; a valid document may have no rows.

    Raises StationPollError for a body that is not a station board, so it is never
    mistaken for an empty board.
    """
    try:
        root = ElementTree.fromstring(xml_body)
    except ElementTree.ParseError:
        raise StationPollError("invalid XML") from None
    if local_name(root.tag) != "ArrayOfObjStationData":
        raise StationPollError("unexpected XML document")
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
    """Fetch and parse one station's board; raise StationPollError if the poll failed."""
    try:
        response = requests.get(api_url, params={"StationCode": station}, timeout=timeout)
        response.raise_for_status()
    except requests.Timeout:
        raise StationPollError("request timed out") from None
    except requests.ConnectionError:
        raise StationPollError("connection error") from None
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "error"
        raise StationPollError(f"HTTP {status}") from None
    except requests.RequestException:
        raise StationPollError("request failed") from None
    if not response.content.strip():
        raise StationPollError("empty response body")
    return parse_station_data(response.content, station)


def _upsert_observations(observations: list[dict[str, object]], fetched_at: datetime) -> None:
    """Save observations with the poll's shared timestamp, replacing a stored delay for the train."""
    if not observations:
        return
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


def record_successful_poll(
    station: str, observations: list[dict[str, object]], attempted_at: datetime,
) -> datetime:
    """Save a poll's observations and its record in one transaction; return succeeded_at.

    Every observation gets ``fetched_at == succeeded_at``, which is what makes it current.
    An empty board advances ``succeeded_at`` too, so the station's board clears while
    earlier observations stay stored.
    """
    succeeded_at = datetime.now(UTC)
    values = {
        "attempted_at": attempted_at, "succeeded_at": succeeded_at,
        "outcome": "ok" if observations else "empty",
        "train_count": len(observations), "error_reason": None,
    }
    try:
        _upsert_observations(observations, succeeded_at)
        statement = insert(StationPoll).values(station_code=station, **values)
        db.session.execute(statement.on_conflict_do_update(
            index_elements=[StationPoll.station_code], set_=values,
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return succeeded_at


def record_failed_poll(station: str, reason: str, attempted_at: datetime) -> None:
    """Record a failure, keeping succeeded_at so the last good board shows as stale."""
    values = {
        "attempted_at": attempted_at, "outcome": "error",
        "train_count": None, "error_reason": reason[:200],
    }
    statement = insert(StationPoll).values(station_code=station, **values)
    db.session.execute(statement.on_conflict_do_update(
        index_elements=[StationPoll.station_code], set_=values,
    ))
    db.session.commit()


def record_heartbeat() -> None:
    """Mark a completed cycle. It proves the worker runs, not that data is arriving."""
    completed_at = datetime.now(UTC)
    statement = insert(WorkerHeartbeat).values(id=1, last_cycle_completed_at=completed_at)
    db.session.execute(statement.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.id], set_={"last_cycle_completed_at": completed_at},
    ))
    db.session.commit()


def poll_station(station: str, api_url: str, timeout: float) -> tuple[str, int]:
    """Poll one station and record the outcome; return it and the observations saved."""
    attempted_at = datetime.now(UTC)
    try:
        observations = fetch_station(station, api_url, timeout)
        record_successful_poll(station, observations, attempted_at)
    except StationPollError as error:
        reason = error.reason
        logger.warning(
            "station poll failed",
            extra={"station": station, "outcome": "error", "error_reason": reason},
        )
    except Exception as error:
        db.session.rollback()
        reason = "database error" if isinstance(error, SQLAlchemyError) else "unexpected error"
        logger.exception(
            "station poll failed unexpectedly",
            extra={"station": station, "outcome": "error", "error_reason": reason},
        )
    else:
        outcome = "ok" if observations else "empty"
        logger.info(
            "station poll succeeded",
            extra={"station": station, "outcome": outcome, "train_count": len(observations)},
        )
        return outcome, len(observations)
    try:
        record_failed_poll(station, reason, attempted_at)
    except Exception:
        db.session.rollback()
        logger.exception("station poll outcome not recorded", extra={"station": station})
    return "error", 0


def run_fetch_cycle(app) -> int:
    """Poll all configured stations; errors stay isolated to each station.

    The heartbeat is written when the cycle completes, even if some polls failed.
    """
    saved = 0
    outcomes = {"ok": 0, "empty": 0, "error": 0}
    for station in app.config["STATION_CODES"]:
        outcome, count = poll_station(
            station, app.config["IRISH_RAIL_API_URL"], app.config["REQUEST_TIMEOUT_SECONDS"],
        )
        outcomes[outcome] += 1
        saved += count
    try:
        record_heartbeat()
    except Exception:
        db.session.rollback()
        logger.exception("worker heartbeat not recorded")
    logger.info(
        "fetch cycle completed",
        extra={"observations_saved": saved, **{f"stations_{key}": value for key, value in outcomes.items()}},
    )
    return saved
