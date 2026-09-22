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


@pytest.mark.parametrize("delay,label", [(-2, "2 min early"), (0, "On time"), (1, "1 min late"),
                                        (2, "2 min late"), (5, "5 min late"), (6, "6 min late")])
def test_dashboard_renders_observations_and_aggregates(client, dashboard_data, delay, label):
    seed, context = dashboard_data
    seed({"train_code": "A123", "delay_minutes": delay})
    response = client.get("/")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "A123" in page and "Connolly" in page and "Bray" in page
    assert f"{delay}.0 min" in page and label in page
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
    assert sorted(row.station for row in context["current_delays"]) == expected
    assert context["summary"].trains == len(expected)
    assert context["summary"].average_delay == (6 if station == "TARA" else 4)
    assert context["station"] == (station if station == "TARA" else "")
    assert {row["station"] for row in context["station_status"]} >= {"CNLLY", "TARA"}
    if station == "TARA":
        assert '<option value="TARA" selected>' in response.get_data(as_text=True)
    client.get("/routes", query_string={"station": station})
    assert context["pagination"]["total"] == len(expected)


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
    assert float(context["summary"].on_time) == pytest.approx(100 / 3)
    assert context["summary"].average_delay == 4
    assert context["highest_delay"].station == "CNLLY"
    assert float(context["highest_delay"].average_delay) == pytest.approx(11 / 3)
    client.get("/routes")
    assert float(context["pagination"]["items"][0].average_delay) == 4


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
    assert "Updated 7 minutes ago" in page
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
    page = client.get("/status").get_data(as_text=True)
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
    page = client.get("/status").get_data(as_text=True)
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
    assert len(queries) == 5
    assert len(context["current_delays"]) == 5
    assert context["network"]["readings"] == 120
    assert all(row.delay_minutes == 9 for row in context["current_delays"])


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("path,title,active", [
    ("/", "Overview", "Overview"),
    ("/stations", "Stations", "Stations"),
    ("/stations/CNLLY", "Dublin Connolly", "Stations"),
    ("/routes", "Route performance", "Route performance"),
    ("/status", "Data status", "Data status"),
])
def test_each_page_renders_with_title_navigation_and_refresh(
    client, dashboard_data, populated, path, title, active,
):
    seed, _ = dashboard_data
    if populated:
        seed({})
    response = client.get(path)
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert f"<title>{title} · Irish Rail Delay Tracker</title>" in page
    assert f'aria-current="page">{active}</a>' in page
    assert '<meta http-equiv="refresh" content="60">' in page
    assert '<main id="main"' in page
    assert '<script' not in page


@pytest.mark.parametrize("path", ["/stations/UNKNOWN", "/missing", "/stations/CNLLY?page=999"])
def test_unknown_pages_are_styled_404s(client, dashboard_data, path):
    response = client.get(path)
    page = response.get_data(as_text=True)
    assert response.status_code == 404
    assert "Page not found · Irish Rail Delay Tracker" in page
    assert 'style.css' in page and 'Browse stations' in page
    assert 'href="/"' in page


def test_station_detail_is_scoped_and_directory_links_to_it(client, dashboard_data):
    seed, context = dashboard_data
    seed({"train_code": "CONNOLLY", "delay_minutes": 7},
         {"station": "TARA", "train_code": "TARATRAIN", "delay_minutes": 1})
    page = client.get("/stations/CNLLY?view=delays").get_data(as_text=True)
    assert "CONNOLLY" in page and "TARATRAIN" not in page
    assert context["summary"].trains == 1
    assert context["summary"].average_delay == 7
    assert context["pagination"]["total"] == 1
    directory = client.get("/stations").get_data(as_text=True)
    assert 'href="/stations/CNLLY"' in directory and 'href="/stations/TARA"' in directory
    assert client.get("/stations?station=TARA").location == "/stations/TARA"
    assert client.get("/stations?station=INVALID").status_code == 200


def test_current_network_is_weighted_and_excludes_old_readings(client, dashboard_data, clock):
    seed, context = dashboard_data
    seed({"delay_minutes": 0}, {"delay_minutes": 1},
         {"station": "TARA", "delay_minutes": 8},
         {"station": "HSTON", "delay_minutes": 99,
          "fetched_at": clock.instant - timedelta(minutes=31)})
    page = client.get("/").get_data(as_text=True)
    network = context["network"]
    assert network["average_delay"] == 3
    assert network["on_time"] == pytest.approx(200 / 3)
    assert network["major"] == 1 and network["readings"] == 3
    assert network["reporting"] == 2
    assert "stations without recent data" in page
    assert context["summary"].average_delay == 27  # Today is distinct from right now.


@pytest.mark.parametrize("path", ["/stations/CNLLY?view=delays", "/routes?station=CNLLY"])
def test_pagination_is_bounded_deterministic_and_preserves_filters(client, dashboard_data, path):
    seed, context = dashboard_data
    seed(*({"origin": f"Origin {i:02}", "delay_minutes": i} for i in range(17)),
         {"station": "TARA", "origin": "Excluded", "delay_minutes": 99})
    page = client.get(path).get_data(as_text=True)
    first = context["pagination"]["items"]
    assert len(first) == 15 and context["pagination"]["total"] == 17
    assert "Excluded" not in page
    assert 'rel="next"' in page
    if path.startswith("/routes"):
        assert 'station=CNLLY' in page
    else:
        assert 'view=delays' in page
    separator = "&" if "?" in path else "?"
    client.get(path + separator + "page=2")
    second = context["pagination"]["items"]
    assert len(second) == 2
    assert not {row.origin for row in first} & {row.origin for row in second}
    assert client.get(path + separator + "page=999999999999999999").status_code == 404
    client.get(path + separator + "page=oops")
    assert context["pagination"]["page"] == 1


def test_route_sample_counts_and_day_filter(client, dashboard_data):
    seed, context = dashboard_data
    seed({"delay_minutes": 1}, {"station": "TARA", "delay_minutes": 5},
         {"train_date": "2026-01-01", "delay_minutes": 99})
    page = client.get("/routes").get_data(as_text=True)
    row = context["pagination"]["items"][0]
    assert row.trains == 2 and row.average_delay == 3
    assert "2 trains" in page


def test_status_lists_stale_stations_first(client, dashboard_data, clock):
    seed, context = dashboard_data
    seed({"station": "CNLLY"},
         {"station": "TARA", "fetched_at": clock.instant - timedelta(hours=1)})
    client.get("/status")
    flags = [row["stale"] for row in context["station_status"]]
    assert flags == sorted(flags, reverse=True)


def test_latest_train_reading_drives_rows_and_all_averages(client, dashboard_data, clock):
    seed, context = dashboard_data
    seed(
        {"train_code": "SAME", "delay_minutes": 40,
         "fetched_at": clock.instant - timedelta(minutes=35)},
        {"train_code": "SAME", "station": "TARA", "delay_minutes": 10,
         "fetched_at": clock.instant - timedelta(minutes=5)},
        {"train_code": "SAME", "station": "PERSE", "delay_minutes": 0},
        {"train_code": "OTHER", "delay_minutes": 6},
    )
    page = client.get("/").get_data(as_text=True)
    rows = context["current_delays"]
    assert [(row.train_code, row.station, row.delay_minutes) for row in rows] == [
        ("OTHER", "CNLLY", 6), ("SAME", "PERSE", 0),
    ]
    assert [(reading.station, reading.delay_minutes)
            for reading in context["reading_details"][rows[1].id]] == [("TARA", 10), ("CNLLY", 40)]
    assert 'Latest reading at <a href="/stations/PERSE">Dublin Pearse</a>' in page
    assert 'Reading details' in page and '40 min late' in page
    assert context["network"]["readings"] == 3  # Station readings remain distinct from trains.
    assert context["network"]["trains"] == 2
    assert context["network"]["average_delay"] == 3
    assert context["network"]["on_time"] == 50
    assert context["summary"].trains == 2
    assert context["summary"].average_delay == 3
    client.get("/routes")
    row = context["pagination"]["items"][0]
    assert row.trains == 2 and row.average_delay == 3
    client.get("/?station=TARA")
    assert context["summary"].trains == 1
    assert context["summary"].average_delay == 10
    assert context["current_delays"][0].station == "TARA"


def test_train_dates_are_separate_and_equal_timestamps_are_deterministic(
    client, dashboard_data,
):
    seed, context = dashboard_data
    seed(
        {"train_code": "SAME", "delay_minutes": 8},
        {"train_code": "SAME", "station": "TARA", "delay_minutes": 1},
        {"train_code": "SAME", "train_date": "2026-09-21", "delay_minutes": 5},
    )
    client.get("/")
    assert [(row.train_date, row.station) for row in context["current_delays"]] == [
        ("2026-09-21", "CNLLY"), ("2026-09-22", "TARA"),
    ]
    assert context["summary"].trains == 1
    assert context["summary"].average_delay == 1
    assert context["network"]["trains"] == 2
    client.get("/")
    assert context["current_delays"][1].station == "TARA"


@pytest.mark.parametrize("instant,rows,expected", [
    ("2026-09-22T12:00:00+00:00", [
        {"scheduled_time": "12:55", "delay_minutes": 10},
        {"scheduled_time": "13:02"},
        {"scheduled_time": "12:59"},
        {"scheduled_time": "broken"},
        {"scheduled_time": "13:10", "train_date": "2026-09-21"},
    ], ["A1", "A0"]),
    ("2026-09-22T23:05:00+00:00", [
        {"scheduled_time": "23:59", "delay_minutes": 10, "train_date": "2026-09-22"},
        {"scheduled_time": "00:06", "train_date": "2026-09-23"},
        {"scheduled_time": "23:50", "train_date": "2026-09-22"},
    ], ["A1", "A0"]),
    ("2026-12-22T12:00:00+00:00", [
        {"scheduled_time": "12:01"}, {"scheduled_time": "11:59"},
    ], ["A0"]),
])
def test_next_services_use_dublin_schedules_and_reported_delay(
    client, dashboard_data, clock, instant, rows, expected,
):
    seed, context = dashboard_data
    clock.instant = datetime.fromisoformat(instant)
    seed(*rows)
    page = client.get("/stations/CNLLY").get_data(as_text=True)
    assert [row.train_code for row in context["pagination"]["items"]] == expected
    assert 'href="/stations/CNLLY"' not in page  # No repeated self-link in service rows.
    assert '<th scope="col">Time</th>' in page
    assert 'Includes terminating arrivals' in page
    client.get("/stations/CNLLY?view=delays")
    assert context["pagination"]["total"] == len(rows)


def test_home_station_picker_names_and_footer(client, dashboard_data):
    seed, _ = dashboard_data
    seed({})
    page = client.get("/").get_data(as_text=True)
    assert page.index('Where are you travelling from?') < page.index('Last 30 minutes')
    for code, name in routes.STATION_NAMES.items():
        assert f'<option value="{code}">{name}</option>' in page
    assert 'Independent project, not affiliated with Iarnród Éireann' in page
    assert 'Daniel English' in page and 'public realtime API' in page
    assert 'selected stations in the Dublin area' in page
    assert 'not a confirmed arrival delay' in page
    assert page.index('GitHub ↗') > page.index('<footer')
    assert 'Route performance</a>' in page and 'Data status</a>' in page
