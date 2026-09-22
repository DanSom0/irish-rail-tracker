"""Tests for application HTTP routes and dashboard calculations."""

from datetime import UTC, datetime, timedelta

import pytest
from flask import template_rendered
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from app import routes
from app.extensions import db
from app.models import Observation


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        instant = datetime(2026, 9, 22, 12, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(routes, "datetime", Clock)
    return Clock


@pytest.fixture
def dashboard_data(app, clock):
    """Seed recorded observations and capture the actual rendered context."""
    rendered = {}

    def capture(sender, template, context):
        rendered.update(context)

    template_rendered.connect(capture, app)

    def seed(*rows):
        with app.app_context():
            for index, row in enumerate(rows):
                db.session.add(Observation(**{
                    "station": "CNLLY",
                    "train_code": f"A{index}",
                    "train_date": clock.instant.astimezone(routes.DUBLIN).date().isoformat(),
                    "origin": "Connolly",
                    "destination": "Bray",
                    "scheduled_time": "09:10",
                    "delay_minutes": 0,
                    "fetched_at": clock.instant,
                    **row,
                }))
            db.session.commit()

    yield seed, rendered
    template_rendered.disconnect(capture, app)


def test_health_reports_database_connectivity(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"database": "connected", "status": "ok"}


def test_health_reports_database_disconnection(client, monkeypatch):
    def raise_database_error(*_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(db.session, "execute", raise_database_error)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.get_json() == {"database": "disconnected", "status": "unhealthy"}


@pytest.mark.parametrize("delay,label", [(0, "On time"), (1, "On time"),
                                        (2, "Minor"), (5, "Minor"), (6, "Major")])
def test_dashboard_renders_observations_and_aggregates(client, dashboard_data, delay, label):
    seed, context = dashboard_data
    seed({"train_code": "A123", "delay_minutes": delay})
    response = client.get("/")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "A123" in page and "Connolly" in page and "Bray" in page
    assert f"{delay}.0 min" in page and f"{delay} min · {label}" in page
    assert '<meta http-equiv="refresh" content="60">' in page
    assert "Europe/Dublin" in page and "13:00:00 IST (UTC+0100)" in page
    assert context["age_minutes"] == 0


def test_dashboard_empty_state(client, dashboard_data):
    _, context = dashboard_data
    response = client.get("/")
    assert response.status_code == 200
    assert "No data yet" in response.get_data(as_text=True)
    assert context["summary"].trains == 0
    assert context["summary"].on_time is None
    assert context["summary"].average_delay is None
    assert context["highest_delay"] is None
    assert context["last_updated"] is None
    assert context["age_minutes"] is None


@pytest.mark.parametrize("station,expected", [("TARA", ["TARA"]), ("INVALID", ["CNLLY", "TARA"])])
def test_station_filter(client, dashboard_data, station, expected):
    seed, context = dashboard_data
    seed({"delay_minutes": 2}, {"station": "TARA", "delay_minutes": 6, "origin": "Malahide"})
    response = client.get("/", query_string={"station": station})
    assert response.status_code == 200
    assert [row.station for row in context["current_delays"]] == expected
    assert [row.station for row in context["by_station"]] == expected
    assert context["summary"].trains == len(expected)
    assert len(context["by_route"]) == len(expected)
    assert context["summary"].average_delay == (6 if station == "TARA" else 4)
    assert context["station"] == (station if station == "TARA" else "")
    assert {row["station"] for row in context["station_status"]} >= {"CNLLY", "TARA"}
    if station == "TARA":
        assert '<option value="TARA" selected>' in response.get_data(as_text=True)


def test_filter_lists_configured_and_observed_stations(app, client, dashboard_data, monkeypatch):
    seed, context = dashboard_data
    codes = tuple(f"S{i:02}" for i in range(22))
    monkeypatch.setitem(app.config, "STATION_CODES", codes)
    seed({"station": "EXTRA"})
    client.get("/?station=S21")
    assert context["stations"] == sorted([*codes, "EXTRA"])
    assert context["station"] == "S21"
    assert context["summary"].trains == 0
    assert context["last_updated"] is None
    assert len(context["station_status"]) == 23


def test_summary_uses_todays_readings_and_counts_unique_trains(client, dashboard_data, clock):
    seed, context = dashboard_data
    seed(
        {"train_code": "SAME", "delay_minutes": 0},
        {"train_code": "SAME", "station": "TARA", "delay_minutes": 1},
        {"delay_minutes": 5},
        {"delay_minutes": 6},
        {"train_date": "2026-09-21", "delay_minutes": 90,
         "fetched_at": clock.instant - timedelta(days=1)},
    )
    client.get("/")
    assert context["summary"].trains == 3
    assert context["summary"].on_time == 50
    assert context["summary"].average_delay == 3
    assert context["highest_delay"].station == "CNLLY"
    assert float(context["highest_delay"].average_delay) == pytest.approx(11 / 3)
    assert float(context["by_route"][0].average_delay) == 3


def test_current_window_and_freshness(client, dashboard_data, clock):
    seed, context = dashboard_data
    seed(
        {"station": "TARA", "delay_minutes": 99,
         "fetched_at": clock.instant - timedelta(minutes=31)},
        {"delay_minutes": 1, "fetched_at": clock.instant - timedelta(minutes=7)},
    )
    page = client.get("/").get_data(as_text=True)
    assert [row.station for row in context["current_delays"]] == ["CNLLY"]
    assert context["highest_delay"].station == "CNLLY"
    assert "Data as of 7 minutes ago" in page
    client.get("/?station=TARA")
    assert context["age_minutes"] == 31
    assert context["highest_delay"] is None


@pytest.mark.parametrize("instant,expected", [
    ("2026-03-29T00:59:00+00:00", "29 Mar 2026, 00:59:00 GMT (UTC+0000)"),
    ("2026-03-29T01:00:00+00:00", "29 Mar 2026, 02:00:00 IST (UTC+0100)"),
    ("2026-10-25T00:59:00+00:00", "25 Oct 2026, 01:59:00 IST (UTC+0100)"),
    ("2026-10-25T01:00:00+00:00", "25 Oct 2026, 01:00:00 GMT (UTC+0000)"),
])
def test_utc_timestamps_render_across_dst(client, dashboard_data, clock, instant, expected):
    seed, _ = dashboard_data
    clock.instant = datetime.fromisoformat(instant)
    seed({})
    page = client.get("/").get_data(as_text=True)
    assert page.count(expected) == 2  # Freshness and station status.


@pytest.mark.parametrize("instant,service_date", [
    ("2026-09-21T23:30:00+00:00", "2026-09-22"),
    ("2026-03-29T23:30:00+00:00", "2026-03-30"),
    ("2026-10-25T23:30:00+00:00", "2026-10-25"),
])
def test_today_uses_dublin_service_date(client, dashboard_data, clock, instant, service_date):
    seed, context = dashboard_data
    clock.instant = datetime.fromisoformat(instant)
    seed({"train_date": service_date}, {"train_date": "2026-01-01", "delay_minutes": 99})
    client.get("/")
    assert context["summary"].trains == 1
    assert context["summary"].average_delay == 0


@pytest.mark.parametrize("instant,flagged", [
    ("2026-09-22T04:59:59+00:00", False),
    ("2026-09-22T05:00:00+00:00", True),
    ("2026-09-22T22:30:00+00:00", True),
    ("2026-09-22T22:30:01+00:00", False),
    ("2026-09-22T23:30:00+00:00", False),
    ("2026-12-22T05:59:59+00:00", False),
    ("2026-12-22T06:00:00+00:00", True),
])
def test_status_flags_only_stale_stations_during_service_hours(
    client, dashboard_data, clock, instant, flagged,
):
    seed, context = dashboard_data
    clock.instant = datetime.fromisoformat(instant)
    seed(
        {"station": "CNLLY", "fetched_at": clock.instant - timedelta(minutes=30, seconds=1)},
        {"station": "TARA", "fetched_at": clock.instant - timedelta(minutes=30)},
    )
    page = client.get("/").get_data(as_text=True)
    statuses = {row["station"]: row for row in context["station_status"]}
    assert statuses["CNLLY"]["stale"] is flagged
    assert statuses["HSTON"]["stale"] is flagged  # Never observed.
    assert statuses["TARA"]["stale"] is False  # Exactly 30 minutes is still fresh.
    if not flagged:
        assert "Overnight · alerts paused" in page
        assert "No recent data</span>" not in page


def test_dashboard_queries_are_constant_with_many_stations(app, client, dashboard_data):
    seed, context = dashboard_data
    seed(*({"station": f"S{i:02}", "delay_minutes": i % 10} for i in range(120)))
    queries = []

    def record(*args):
        queries.append(args[2])

    with app.app_context():
        engine = db.engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            assert client.get("/").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", record)
    assert len(queries) == 6
    assert len(context["current_delays"]) == 100
    assert len(context["by_station"]) == 120
