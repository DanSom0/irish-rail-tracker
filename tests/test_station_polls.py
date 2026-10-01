"""Station poll outcomes, current boards, the worker heartbeat and ingestion readiness."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app import fetcher, routes
from app.config import max_update_age
from app.extensions import db
from app.models import Observation, StationPoll, WorkerHeartbeat

FIXTURES = Path(__file__).parent / "fixtures"
BOARD = (FIXTURES / "station_data_namespaced.xml").read_bytes()  # Trains A123 and T456.
EMPTY_BOARD = b'<ArrayOfObjStationData xmlns="http://api.irishrail.ie/realtime/" />'
URL = "https://example.test/getStationDataByCodeXML"


def _board(*codes):
    rows = "".join(
        f"<objStationData><Traincode>{code} </Traincode><Traindate>28 Sep 2026</Traindate>"
        "<Origin>Howth</Origin><Destination>Bray</Destination><Scharrival>09:00</Scharrival>"
        "<Schdepart>09:02</Schdepart><Late>3</Late></objStationData>" for code in codes
    )
    return f'<ArrayOfObjStationData xmlns="http://api.irishrail.ie/realtime/">{rows}</ArrayOfObjStationData>'.encode()


@pytest.fixture
def api(monkeypatch):
    """Serve each station's next response: XML bytes, or an exception to raise."""
    responses = {}

    def get(_url, params, timeout):
        result = responses[params["StationCode"]]
        if isinstance(result, Exception):
            raise result
        return Mock(content=result)

    monkeypatch.setattr(fetcher.requests, "get", get)
    return responses


@pytest.fixture
def monitored(app, monkeypatch):
    monkeypatch.setitem(app.config, "STATION_CODES", ("CNLLY", "TARA"))


def _poll(app, station="CNLLY"):
    with app.app_context():
        return fetcher.poll_station(station, URL, 1)


def _cycle(app):
    with app.app_context():
        return fetcher.run_fetch_cycle(SimpleNamespace(config=app.config))


def _record(app, station="CNLLY"):
    with app.app_context():
        return db.session.get(StationPoll, station)


def _stored_trains(app):
    with app.app_context():
        return sorted(db.session.scalars(db.select(Observation.train_code)))


def _network(client, code="CNLLY"):
    return next(item for item in client.get("/api/network").get_json()["stations"] if item["code"] == code)


def _current(client, code="CNLLY"):
    return sorted(train["train_code"] for train in _network(client, code)["current_trains"])


def test_poll_saves_every_observation_with_the_polls_succeeded_at(app, api, monitored):
    api["CNLLY"] = BOARD
    assert _poll(app) == ("ok", 2)
    record = _record(app)
    with app.app_context():
        fetched = set(db.session.scalars(db.select(Observation.fetched_at)))
    assert fetched == {record.succeeded_at}
    assert (record.outcome, record.train_count, record.error_reason) == ("ok", 2, None)
    assert record.attempted_at <= record.succeeded_at


def test_repeat_poll_updates_a_trains_delay_instead_of_adding_a_row(app, api, monitored):
    api["CNLLY"] = BOARD
    _poll(app)
    api["CNLLY"] = BOARD.replace(b"<Late>7</Late>", b"<Late>12</Late>")
    _poll(app)
    with app.app_context():
        saved = db.session.scalars(db.select(Observation).order_by(Observation.train_code)).all()
    assert [(row.train_code, row.delay_minutes) for row in saved] == [("A123", 12), ("T456", 0)]


def test_successful_empty_poll_clears_the_board_and_keeps_history(app, client, api, monitored):
    api["CNLLY"] = BOARD
    _poll(app)
    first = _record(app).succeeded_at
    api["CNLLY"] = EMPTY_BOARD
    assert _poll(app) == ("empty", 0)

    record = _record(app)
    assert (record.outcome, record.train_count, record.error_reason) == ("empty", 0, None)
    assert record.succeeded_at > first
    assert _current(client) == []
    assert _network(client)["latest_observation_at"] == record.succeeded_at.isoformat()
    assert _stored_trains(app) == ["A123", "T456"]


def test_partial_board_drops_a_train_missing_from_the_latest_successful_poll(
    app, client, api, monitored,
):
    api["CNLLY"] = _board("A", "B")
    _poll(app)
    assert _current(client) == ["A", "B"]
    api["CNLLY"] = _board("B")
    _poll(app)
    assert _current(client) == ["B"]
    assert _stored_trains(app) == ["A", "B"]


@pytest.mark.parametrize("failure,reason", [
    (requests.Timeout(f"Read timed out: {URL}?StationCode=CNLLY"), "request timed out"),
    (requests.ConnectionError(f"{URL}?StationCode=CNLLY"), "connection error"),
    (b"<not-valid-xml", "invalid XML"),
])
def test_failed_poll_keeps_the_last_successful_board_marked_stale(
    app, client, api, monitored, failure, reason,
):
    api["CNLLY"] = BOARD
    _poll(app)
    before = _record(app)
    api["CNLLY"] = failure
    assert _poll(app) == ("error", 0)

    record = _record(app)
    assert (record.outcome, record.train_count, record.error_reason) == ("error", None, reason)
    assert record.succeeded_at == before.succeeded_at
    assert record.attempted_at > before.attempted_at
    assert _current(client) == ["A123", "T456"]
    poll = _network(client)["poll"]
    assert (poll["stale"], poll["outcome"], poll["error_reason"]) == (True, "error", reason)
    shown = before.succeeded_at.astimezone(routes.DUBLIN).strftime("%H:%M")
    assert poll["text"] == f"Update failed; showing data from {shown}"


def test_failure_before_any_success_shows_no_board(app, client, api, monitored):
    api["CNLLY"] = requests.Timeout("timed out")
    _poll(app)
    record = _record(app)
    assert (record.outcome, record.succeeded_at, record.train_count) == ("error", None, None)
    poll = _network(client)["poll"]
    assert (poll["stale"], poll["text"]) == (False, "Update failed; no data yet")


def test_observations_and_the_poll_record_are_saved_atomically(app, client, api, monitored, monkeypatch):
    api["CNLLY"] = _board("A")
    _poll(app)
    before = _record(app)
    api["CNLLY"] = _board("B")
    execute = db.session.execute
    calls = []

    def fail_on_poll_record(statement, *args, **kwargs):
        calls.append(statement)
        if len(calls) == 2:  # After the observations are written, before the poll record.
            raise OperationalError("UPSERT station_polls", {}, Exception("connection lost"))
        return execute(statement, *args, **kwargs)

    with app.app_context():
        monkeypatch.setattr(db.session, "execute", fail_on_poll_record)
        with pytest.raises(SQLAlchemyError):
            fetcher.record_successful_poll("CNLLY", fetcher.fetch_station("CNLLY", URL, 1), before.attempted_at)
        monkeypatch.setattr(db.session, "execute", execute)

    assert _stored_trains(app) == ["A"]
    record = _record(app)
    assert (record.outcome, record.succeeded_at) == ("ok", before.succeeded_at)
    assert _current(client) == ["A"]


def test_a_database_error_while_saving_records_a_failed_poll(app, api, monitored, monkeypatch):
    api["CNLLY"] = BOARD
    _poll(app)
    before = _record(app)

    def fail(*_args):
        raise OperationalError("INSERT INTO observations", {}, Exception("disk full"))

    monkeypatch.setattr(fetcher, "_upsert_observations", fail)
    assert _poll(app) == ("error", 0)
    record = _record(app)
    assert (record.outcome, record.error_reason) == ("error", "database error")
    assert record.succeeded_at == before.succeeded_at


def test_cycle_logs_accurate_outcomes_with_sanitised_reasons(app, api, monitored, caplog):
    api["CNLLY"] = BOARD
    api["TARA"] = requests.Timeout(f"HTTPSConnectionPool: Read timed out. (url: {URL}?StationCode=TARA)")
    with caplog.at_level(logging.INFO, logger="app.fetcher"):
        assert _cycle(app) == 2

    by_station = {(record.message, getattr(record, "station", None)): record for record in caplog.records}
    succeeded = by_station[("station poll succeeded", "CNLLY")]
    assert (succeeded.outcome, succeeded.train_count) == ("ok", 2)
    failed = by_station[("station poll failed", "TARA")]
    assert (failed.outcome, failed.error_reason, failed.exc_info) == ("error", "request timed out", None)
    assert ("station poll succeeded", "TARA") not in by_station
    assert "StationCode" not in caplog.text and "Traceback" not in caplog.text
    completed = by_station[("fetch cycle completed", None)]
    assert (completed.stations_ok, completed.stations_empty, completed.stations_error) == (1, 0, 1)


def test_heartbeat_updates_when_some_polls_fail(app, api, monitored):
    api["CNLLY"] = requests.ConnectionError("refused")
    api["TARA"] = b"<not-valid-xml"
    started = datetime.now(UTC)
    _cycle(app)
    with app.app_context():
        heartbeats = db.session.scalars(db.select(WorkerHeartbeat)).all()
    assert len(heartbeats) == 1 and heartbeats[0].last_cycle_completed_at >= started
    assert {_record(app, code).outcome for code in ("CNLLY", "TARA")} == {"error"}

    _cycle(app)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(WorkerHeartbeat)) == 1


def _set_heartbeat(app, age):
    with app.app_context():
        db.session.merge(WorkerHeartbeat(id=1, last_cycle_completed_at=datetime.now(UTC) - age))
        db.session.commit()


def test_ingestion_is_not_ready_without_a_heartbeat(client, monitored):
    response = client.get("/health/ingestion")
    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "not ready"
    assert body["heartbeat"] == {"last_cycle_completed_at": None, "age_seconds": None, "max_age_seconds": 900}
    assert body["stations"] == {"ok": 0, "empty": 0, "error": 0, "awaiting_first_poll": 2}


@pytest.mark.parametrize("age,status", [
    (timedelta(minutes=14, seconds=50), 200),
    (timedelta(minutes=15), 503),
    (timedelta(hours=3), 503),
])
def test_ingestion_readiness_follows_heartbeat_age(app, client, monitored, age, status):
    _set_heartbeat(app, age)
    response = client.get("/health/ingestion")
    assert response.status_code == status
    assert response.get_json()["status"] == ("ok" if status == 200 else "not ready")
    assert response.get_json()["heartbeat"]["age_seconds"] >= int(age.total_seconds())


def test_ingestion_reports_station_errors_without_failing_and_empty_is_healthy(
    app, client, api, monkeypatch,
):
    monkeypatch.setitem(app.config, "STATION_CODES", ("CNLLY", "TARA", "PERSE", "HSTON"))
    api.update(CNLLY=BOARD, TARA=EMPTY_BOARD, PERSE=requests.Timeout("timed out"),
               HSTON=requests.Timeout("timed out"))
    _cycle(app)
    with app.app_context():  # HSTON has never been polled.
        db.session.delete(db.session.get(StationPoll, "HSTON"))
        db.session.commit()

    response = client.get("/health/ingestion")
    assert response.status_code == 200
    assert response.get_json()["stations"] == {"ok": 1, "empty": 1, "error": 1, "awaiting_first_poll": 1}
    assert client.get("/health").get_json() == {"database": "connected", "status": "ok", "release": None}


def test_ingestion_is_unhealthy_when_the_database_is_unreachable(client, monitored, monkeypatch):
    def raise_database_error(*_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(db.session, "scalar", raise_database_error)
    response = client.get("/health/ingestion")
    assert response.status_code == 503
    assert response.get_json() == {"database": "disconnected", "status": "unhealthy"}


def test_station_without_a_poll_record_is_awaiting_first_poll(app, client, monitored):
    with app.app_context():  # Stored before this release: never treated as an empty success.
        db.session.add(Observation(station="TARA", train_code="E1", train_date="2026-09-28", origin="Howth",
                                   destination="Bray", scheduled_time="09:00", delay_minutes=2,
                                   fetched_at=datetime.now(UTC) - timedelta(minutes=1)))
        db.session.commit()
    station = _network(client, "TARA")
    assert station["poll"]["outcome"] == "awaiting_first_poll"
    assert (station["current_trains"], station["latest_observation_at"]) == ([], None)
    page = client.get("/status").get_data(as_text=True)
    assert "Awaiting first poll" in page
    assert "0/2 stations reporting" in client.get("/").get_data(as_text=True)


def test_status_page_map_api_and_boards_agree_on_freshness(app, client, api, monkeypatch):
    monkeypatch.setitem(app.config, "STATION_CODES", ("CNLLY", "TARA", "PERSE", "HSTON"))
    api.update(CNLLY=BOARD, TARA=_board("E1"), PERSE=_board("P1"), HSTON=EMPTY_BOARD)
    _cycle(app)
    api.update(TARA=EMPTY_BOARD, PERSE=requests.Timeout("timed out"))
    for code in ("TARA", "PERSE"):
        _poll(app, code)
    perse_time = _record(app, "PERSE").succeeded_at.astimezone(routes.DUBLIN).strftime("%H:%M")
    with app.app_context():  # HSTON has never been polled.
        db.session.delete(db.session.get(StationPoll, "HSTON"))
        db.session.commit()

    expected = {
        "CNLLY": ("OK, 2 trains", ["A123", "T456"]),
        "TARA": ("No services returned", []),
        "PERSE": (f"Update failed; showing data from {perse_time}", ["P1"]),
        "HSTON": ("Awaiting first poll", []),
    }
    status_page = client.get("/status").get_data(as_text=True)
    for code, (text, trains) in expected.items():
        assert _network(client, code)["poll"]["text"] == text
        assert text in status_page
        assert _current(client, code) == trains
        board = client.get(f"/stations/{code}", query_string={"view": "delays"}).get_data(as_text=True)
        assert all(train in board for train in trains)
    assert "P1" in client.get("/stations/PERSE", query_string={"view": "delays"}).get_data(as_text=True)
    assert f"Update failed; showing data from {perse_time}" in client.get("/stations/PERSE").get_data(as_text=True)
    assert "E1" not in client.get("/stations/TARA", query_string={"view": "delays"}).get_data(as_text=True)


def test_a_successful_board_goes_stale_when_the_worker_stops_polling(app, client, api, monitored):
    api["CNLLY"] = BOARD
    _poll(app)
    with app.app_context():
        record = db.session.get(StationPoll, "CNLLY")
        old = datetime.now(UTC) - timedelta(minutes=20)
        db.session.execute(db.update(Observation).values(fetched_at=old))
        record.attempted_at = record.succeeded_at = old
        db.session.commit()
    poll = _network(client)["poll"]
    assert poll["stale"] is True
    assert poll["text"] == f"OK, 2 trains; not updated since {old.astimezone(routes.DUBLIN):%H:%M}"
    assert _current(client) == ["A123", "T456"]


@pytest.mark.parametrize("interval,limit", [(1, 15), (5, 15), (10, 30), (20, 60)])
def test_max_update_age_is_three_intervals_but_at_least_fifteen_minutes(interval, limit):
    assert max_update_age(interval) == timedelta(minutes=limit)


@pytest.mark.parametrize("age,status", [(timedelta(minutes=20), 200), (timedelta(minutes=29, seconds=50), 200),
                                        (timedelta(minutes=30), 503)])
def test_a_longer_fetch_interval_extends_readiness(app, client, monitored, monkeypatch, age, status):
    monkeypatch.setitem(app.config, "FETCH_INTERVAL_MINUTES", 10)
    _set_heartbeat(app, age)
    response = client.get("/health/ingestion")
    assert response.status_code == status
    assert response.get_json()["heartbeat"]["max_age_seconds"] == 1800


def test_a_longer_fetch_interval_extends_board_freshness_and_coverage(app, client, api, monitored, monkeypatch):
    monkeypatch.setitem(app.config, "FETCH_INTERVAL_MINUTES", 10)
    api["CNLLY"] = BOARD
    _poll(app)

    def age_board(minutes):
        with app.app_context():
            old = datetime.now(UTC) - timedelta(minutes=minutes)
            db.session.execute(db.update(Observation).values(fetched_at=old))
            record = db.session.get(StationPoll, "CNLLY")
            record.attempted_at = record.succeeded_at = old
            db.session.commit()

    age_board(20)  # Stale at the default 15 minutes, but within three 10-minute polls.
    assert _network(client)["poll"]["stale"] is False
    status = client.get("/status").get_data(as_text=True)
    assert "succeeded in the last 30 minutes" in status
    assert "1/2 stations reporting" in client.get("/").get_data(as_text=True)
    age_board(30)
    assert _network(client)["poll"]["stale"] is True
    assert "0/2 stations reporting" in client.get("/").get_data(as_text=True)
