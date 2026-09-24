"""Tests for application HTTP routes and dashboard calculations."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
    assert ("A123" in page) is (delay >= 2)
    assert label in page
    assert '<meta http-equiv="refresh" content="60">' in page
    assert "Europe/Dublin" in page and "13:00:00 IST (UTC+0100)" in page
    assert context["age_minutes"] == 0


def test_dashboard_empty_state(client, dashboard_data):
    _, context = dashboard_data
    response = client.get("/")
    assert response.status_code == 200
    assert "Current punctuality is not available" in response.get_data(as_text=True)
    assert "Current delays are unavailable until a monitored station returns fresh readings" in (
        response.get_data(as_text=True)
    )
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
        {"delay_minutes": 2, "fetched_at": clock.instant - timedelta(minutes=7)},
    )
    page = client.get("/").get_data(as_text=True)
    assert [row.station for row in context["current_delays"]] == ["CNLLY"]
    assert context["highest_delay"].station == "CNLLY"
    assert "Updated 7 minutes ago" in page
    client.get("/?station=TARA")
    assert context["age_minutes"] == 31
    assert context["highest_delay"] is None


def test_current_views_only_show_latest_station_cycle_but_daily_stats_keep_all_trains(
    client, dashboard_data, clock,
):
    seed, context = dashboard_data
    seed(
        {"train_code": "DEPARTED", "scheduled_time": "14:00", "delay_minutes": 9,
         "fetched_at": clock.instant - timedelta(minutes=30)},
        {"train_code": "PREVIOUS", "scheduled_time": "14:10", "delay_minutes": 7,
         "fetched_at": clock.instant - timedelta(minutes=7)},
        {"train_code": "ONBOARD", "scheduled_time": "14:20", "delay_minutes": 2},
    )
    home = client.get("/").get_data(as_text=True)
    assert [row.train_code for row in context["current_delays"]] == ["ONBOARD"]
    assert "DEPARTED" not in home and "PREVIOUS" not in home
    assert context["summary"].trains == 3
    assert context["summary"].major == 2
    assert context["summary"].average_delay == 6
    assert context["network"]["trains"] == 1
    assert context["network"]["average_delay"] == 2
    assert context["network"]["major"] == 0
    for view in ("next", "delays"):
        page = client.get(f"/stations/CNLLY?view={view}").get_data(as_text=True)
        assert [row.train_code for row in context["pagination"]["items"]] == ["ONBOARD"]
        assert "DEPARTED" not in page and "PREVIOUS" not in page
        assert context["summary"].trains == 3
    client.get("/stations")
    assert next(row for row in context["station_status"] if row["station"] == "CNLLY")[
        "average_delay"] == 2


def test_latest_station_board_expires_after_ten_minutes(client, dashboard_data, clock):
    seed, context = dashboard_data
    seed(
        {"train_code": "STALE", "delay_minutes": 9,
         "fetched_at": clock.instant - timedelta(minutes=10, seconds=1)},
        {"train_code": "CURRENT", "station": "TARA", "delay_minutes": 8,
         "fetched_at": clock.instant - timedelta(minutes=10)},
    )
    home = client.get("/").get_data(as_text=True)
    assert [row.train_code for row in context["current_delays"]] == ["CURRENT"]
    assert "STALE" not in home
    assert context["coverage"]["reporting"] == 2  # Status retains its 30-minute window.
    assert context["summary"].trains == 2
    assert context["reporting_shortcuts"] == ["TARA"]
    client.get("/stations/CNLLY?view=delays")
    assert context["pagination"]["total"] == 0


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
        assert "No recent readings" in page


def test_dashboard_queries_are_constant_with_many_stations(app, client, dashboard_data, monkeypatch):
    seed, context = dashboard_data
    monkeypatch.setitem(app.config, "STATION_CODES", tuple(f"S{i:02}" for i in range(120)))
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
    ("/status", "Data status", "● Limited coverage"),
    ("/about/data", "How the data works", None),
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
    if active:
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
    assert "2/20 stations reporting" in page
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


def test_status_lists_stations_alphabetically(client, dashboard_data, clock):
    seed, context = dashboard_data
    seed({"station": "CNLLY"},
         {"station": "TARA", "fetched_at": clock.instant - timedelta(hours=1)})
    client.get("/status")
    names = [routes.station_name(row["station"]) for row in context["station_status"]]
    assert names == sorted(names, key=str.casefold)


def test_latest_train_reading_drives_rows_and_all_averages(client, dashboard_data, clock):
    seed, context = dashboard_data
    seed(
        {"train_code": "SAME", "delay_minutes": 40,
         "fetched_at": clock.instant - timedelta(minutes=35)},
        {"train_code": "SAME", "station": "TARA", "delay_minutes": 10,
         "fetched_at": clock.instant - timedelta(minutes=5)},
        {"train_code": "SAME", "station": "PERSE", "delay_minutes": 2},
        {"train_code": "OTHER", "delay_minutes": 6},
    )
    page = client.get("/").get_data(as_text=True)
    rows = context["current_delays"]
    assert [(row.train_code, row.station, row.delay_minutes) for row in rows] == [
        ("OTHER", "CNLLY", 6), ("SAME", "PERSE", 2),
    ]
    assert [(reading.station, reading.delay_minutes)
            for reading in context["reading_details"][rows[1].id]] == [("TARA", 10), ("CNLLY", 40)]
    assert 'Reported at <a href="/stations/PERSE">Dublin Pearse</a>' in page
    assert '<summary>Service details</summary>' in page and '40 min late' in page
    reporting = page.index('Reported at <a href="/stations/PERSE">')
    assert reporting < page.index(
        '<details class="reading-details"><summary>Service details</summary>', reporting,
    ) < page.index('Train SAME')
    assert context["network"]["readings"] == 3  # Station readings remain distinct from trains.
    assert context["network"]["trains"] == 2
    assert context["network"]["average_delay"] == 4
    assert context["network"]["on_time"] == 0
    assert context["summary"].trains == 2
    assert context["summary"].average_delay == 4
    client.get("/routes")
    row = context["pagination"]["items"][0]
    assert row.trains == 2 and row.average_delay == 4
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
        {"train_code": "SAME", "station": "TARA", "delay_minutes": 2},
        {"train_code": "SAME", "train_date": "2026-09-21", "delay_minutes": 5},
    )
    client.get("/")
    assert [(row.train_date, row.station) for row in context["current_delays"]] == [
        ("2026-09-21", "CNLLY"), ("2026-09-22", "TARA"),
    ]
    assert context["summary"].trains == 1
    assert context["summary"].average_delay == 2
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
    assert '<th scope="col" class="numeric">Scheduled</th>' in page
    assert 'inferred from matching origin and destination names' in page
    client.get("/stations/CNLLY?view=delays")
    assert context["pagination"]["total"] == len(rows)


def test_home_station_picker_names_and_footer(client, dashboard_data):
    seed, _ = dashboard_data
    seed({})
    page = client.get("/").get_data(as_text=True)
    assert page.index('class="station-start"') < page.index('<h1 id="home-heading">')
    assert page.index('Find your station') < page.index('Network right now')
    assert 'class="intro-rail"' in page and 'aria-hidden="true" focusable="false"' in page
    assert page.index('class="homepage-lower"') < page.index('class="current-delays"')
    assert page.index('class="current-delays"') < page.index('class="daily"')
    for code in ("CNLLY", "PERSE", "HSTON", "TARA", "MHIDE"):
        assert f'<option value="{code}">{routes.STATION_NAMES[code]}</option>' in page
    assert '<a href="/stations/CNLLY">Dublin Connolly</a>' in page
    for code in ("PERSE", "TARA", "HSTON", "GCDK"):
        assert f'<a href="/stations/{code}">{routes.STATION_NAMES[code]}</a>' not in page
    assert "Check your station&#39;s" in page or "Check your station's" in page
    assert 'Independent project, not affiliated with Iarnród Éireann' in page
    assert 'Daniel English' in page
    assert page.index('Report a problem') > page.index('<footer')
    assert 'href="/about/data"' in page
    assert 'Route performance</a>' in page and '● Limited coverage</a>' in page


def test_static_names_cover_configured_stations(app, client, dashboard_data, monkeypatch):
    from pathlib import Path

    from app.stations import STATION_NAMES

    example = next(line.split("=", 1)[1] for line in Path(".env.example").read_text().splitlines()
                   if line.startswith("STATION_CODES="))
    assert len(example.split(",")) == len(set(example.split(",")))
    assert not any(line.startswith("STATION_CODES=") for line in (
        Path(".env.production.example").read_text().splitlines()
    ))
    codes = set(example.split(",")) | set(app.config["STATION_CODES"])
    assert all(STATION_NAMES.get(code) for code in codes)
    monkeypatch.setitem(app.config, "STATION_CODES", tuple(codes))
    for path in ("/", "/stations", "/routes"):
        page = client.get(path).get_data(as_text=True)
        assert "Unlisted station" not in page
        for code in codes:
            assert STATION_NAMES[code] in page
    assert "No monitored stations have a reading in the last 30 minutes" in (
        client.get("/status").get_data(as_text=True)
    )
    assert client.get("/stations/GCDK").status_code == 200


@pytest.mark.parametrize("all_reporting", [False, True])
def test_coverage_counts_stations_with_readings_in_window(
    app, client, dashboard_data, clock, monkeypatch, all_reporting,
):
    seed, context = dashboard_data
    monkeypatch.setitem(app.config, "STATION_CODES", ("CNLLY", "TARA", "GCDK"))
    seed(
        {"station": "CNLLY"},
        {"station": "CNLLY"},  # Multiple trains still count as one reporting station.
        {"station": "TARA", "fetched_at": clock.instant - timedelta(minutes=30)},
        {"station": "GCDK", "fetched_at": clock.instant - timedelta(
            minutes=29 if all_reporting else 30, seconds=1)},
        {"station": "PERSE"},  # Historical/unconfigured stations do not inflate coverage.
        {"station": "GCDK", "fetched_at": clock.instant + timedelta(minutes=1)},
    )
    page = client.get("/").get_data(as_text=True)
    assert context["coverage"] == {"reporting": 3 if all_reporting else 2, "total": 3}
    if all_reporting:
        assert "3/3 stations reporting" in page
        assert 'class="coverage limited"' not in page
        assert "● Data up to date" in page
    else:
        assert "2/3 stations reporting" in page
        assert "No recent readings" in client.get("/status").get_data(as_text=True)
    client.get("/stations/CNLLY")
    assert context["coverage"]["total"] == 3


@pytest.mark.parametrize("origin,destination,label", [
    ("Bray", "Dublin Connolly", "Terminates here"),
    ("Dublin Connolly", "Bray", "Starts here"),
    ("Malahide", "Bray", "Calling service"),
    ("Bray", "CNLLY", "Terminates here"),
    ("CNLLY", "Bray", "Starts here"),
    ("Bray", " dUbLiN cOnNoLlY ", "Terminates here"),
])
def test_service_direction_uses_station_name_map(
    client, dashboard_data, origin, destination, label,
):
    seed, _ = dashboard_data
    seed({"origin": origin, "destination": destination})
    page = client.get("/stations/CNLLY?view=delays").get_data(as_text=True)
    assert f"<small>{label}</small>" in page


def test_station_cookie_set_preselect_and_forget(client, dashboard_data):
    response = client.get("/stations?station=TARA")
    cookie = response.headers["Set-Cookie"]
    assert "station=TARA" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Max-Age=7776000" in cookie
    page = client.get("/").get_data(as_text=True)
    assert '<option value="TARA" selected>' in page
    assert "Forget my station" in page
    assert "Dublin rail services" in page  # Preference preselects; network stays network-wide.
    response = client.get("/forget-station")
    assert "Max-Age=0" in response.headers["Set-Cookie"]
    assert '<option value="TARA" selected>' not in client.get("/").get_data(as_text=True)
    assert client.get("/stations?station=INVALID").headers.get("Set-Cookie") is None
    client.set_cookie("station", "INVALID")
    assert "Forget my station" not in client.get("/").get_data(as_text=True)


def test_methodology_and_problem_link(client, dashboard_data):
    page = client.get("/about/data").get_data(as_text=True)
    for text in ("every 5 minutes", "Late field", "not a confirmed arrival delay",
                 "last 30 minutes", "fetch failures", "not affiliated with Iarnród Éireann",
                 "public realtime API", "name-based inferences"):
        assert text in page
    for path in ("/", "/stations", "/stations/CNLLY", "/routes", "/status",
                 "/about/data", "/missing"):
        assert 'href="https://github.com/DanSom0/irish-rail-tracker/issues/new"' in (
            client.get(path).get_data(as_text=True)
        )


def test_shortcut_station_can_be_reselected_without_configured_readings(client, dashboard_data):
    client.get("/stations/GCDK")
    page = client.get("/").get_data(as_text=True)
    assert '<option value="GCDK" selected>Grand Canal Dock</option>' in page
    assert client.get("/stations?station=GCDK").location == "/stations/GCDK"


def test_early_trains_count_as_on_time_with_sample_size(client, dashboard_data):
    seed, context = dashboard_data
    seed({"delay_minutes": -2}, {"delay_minutes": 1}, {"delay_minutes": 3})
    page = client.get("/").get_data(as_text=True)
    assert context["network"]["on_time"] == pytest.approx(200 / 3)
    assert context["summary"].on_time == pytest.approx(200 / 3)
    assert "Current figures: based on 3 trains" in page
    assert "Today: based on 3 trains" in page


def test_figure_explanations_are_disclosed_while_headlines_and_warnings_remain_visible(
    client, dashboard_data, clock,
):
    seed, _ = dashboard_data
    seed({"delay_minutes": 7, "fetched_at": clock.instant - timedelta(minutes=11)})
    home = client.get("/").get_data(as_text=True)
    before_details, explanation = home.split(
        '<details class="figures-details"><summary>About these figures</summary>', 1,
    )
    assert "1/20 stations reporting" in before_details
    assert "Updated 11 minutes ago" in before_details
    assert "Limited coverage" in before_details and "Latest board stale" in before_details
    assert "Coverage means" in explanation and "Today: based on 1 train" in explanation
    assert "On time includes early trains" in explanation
    assert home.count('<details class="figures-details">') == 1
    assert '<dl class="daily-metrics">' in home and "Trains tracked" in home
    station = client.get("/stations/CNLLY").get_data(as_text=True)
    assert '<details class="figures-details"><summary>About these figures</summary>' in station


@pytest.mark.parametrize("path", ["/", "/stations", "/routes", "/stations/CNLLY?view=delays"])
def test_negative_averages_use_early_wording(client, dashboard_data, path):
    seed, _ = dashboard_data
    seed({"delay_minutes": -1})
    page = client.get(path).get_data(as_text=True)
    assert "1 min early" in page
    assert "-1.0 min" not in page


def test_significant_delays_threshold_and_empty_state(client, dashboard_data):
    seed, context = dashboard_data
    seed({"delay_minutes": -2}, {"delay_minutes": 0}, {"delay_minutes": 1})
    page = client.get("/").get_data(as_text=True)
    assert context["current_delays"] == []
    assert "No significant delays in the latest readings" in page
    assert "Limited coverage" in page
    assert "departure-board" not in page
    seed({"train_code": "LATE", "delay_minutes": 2})
    client.get("/")
    assert [r.train_code for r in context["current_delays"]] == ["LATE"]


def test_aliases_share_one_picker_entry_and_coverage(app, client, dashboard_data, monkeypatch):
    seed, context = dashboard_data
    monkeypatch.setitem(app.config, "STATION_CODES", ("HZLCH", "HAZEF", "HAZES", "HZLCH"))
    seed({"station": "HAZEF", "train_code": "SAME", "delay_minutes": 10},
         {"station": "HZLCH", "train_code": "SAME", "delay_minutes": 2})
    page = client.get("/").get_data(as_text=True)
    assert page.count('<option value="HZLCH"') == 1
    assert '<option value="HAZEF"' not in page and '<option value="HAZES"' not in page
    assert context["coverage"] == {"reporting": 1, "total": 1}
    client.get("/stations/HZLCH?view=delays")
    assert context["pagination"]["total"] == 1
    assert context["summary"].average_delay == 2


@pytest.mark.parametrize("count", [1, 2])
def test_recent_reading_pluralisation(client, dashboard_data, count):
    seed, _ = dashboard_data
    seed(*({} for _ in range(count)))
    for path, phrase in (("/stations", "current service"), ("/status", "recent reading")):
        page = client.get(path).get_data(as_text=True)
        assert f"{count} {phrase}{'s' if count != 1 else ''}" in page
        assert f"1 {phrase}s" not in page


@pytest.mark.parametrize("path", ["/", "/routes", "/stations", "/stations/CNLLY"])
def test_empty_sections_and_yesterday_link(client, dashboard_data, clock, path):
    seed, _ = dashboard_data
    page = client.get(path).get_data(as_text=True)
    assert "No data yet" not in page and "<thead>" not in page and "0 routes" not in page
    assert "View yesterday" not in page
    seed({"train_date": "2026-09-21", "fetched_at": clock.instant - timedelta(days=1)})
    page = client.get(path).get_data(as_text=True)
    assert "View yesterday" in page
    page = client.get("/routes?day=yesterday&station=CNLLY").get_data(as_text=True)
    assert "Yesterday" in page and "1 train" in page


@pytest.mark.parametrize("value,expected", [
    (-1, "1 min early"), (-0.5, "0.5 min early"), (-0.01, "<0.1 min early"),
    (0, "On time"), (1, "1 min late"), (15, "15 min late"),
])
def test_delay_wording_for_averages_and_readings(value, expected):
    assert routes.delay_text(value) == expected


def test_official_place_names_and_defensive_picker_dedupe():
    from app.stations import STATION_ALIASES, station_codes

    assert routes.place_name("connolly") == routes.STATION_NAMES["CNLLY"]
    assert routes.place_name(" HazelHatch ") == routes.STATION_NAMES["HZLCH"]
    assert routes.place_name("dlery") == routes.STATION_NAMES["DLERY"]
    assert station_codes(("HAZES", "HZLCH", "HAZEF", "hzlch", "CNLLY", "CNLLY")) == (
        "CNLLY", "HZLCH",
    )
    assert len(routes.sort_stations(["CLDKN", "CLONF", "CLONS"])) == 1
    assert len(set(routes.STATION_NAMES.values())) == len(routes.STATION_NAMES)
    assert station_codes((*STATION_ALIASES, *STATION_ALIASES.values())) == (
        "ADMTN", "CLDKN", "HZLCH", "KISHO", "CHORC",
    )


@pytest.mark.parametrize("alias,canonical", [
    ("ADAMF", "ADMTN"), ("ADAMS", "ADMTN"),
    ("CLONF", "CLDKN"), ("CLONS", "CLDKN"),
    ("HAZEF", "HZLCH"), ("HAZES", "HZLCH"),
    ("KISHF", "KISHO"), ("KISHS", "KISHO"),
    ("PWESF", "CHORC"),
])
def test_legacy_station_aliases_share_one_board(client, dashboard_data, alias, canonical):
    seed, context = dashboard_data
    seed({"station": alias, "delay_minutes": 4})
    page = client.get(f"/stations/{canonical}?view=delays").get_data(as_text=True)
    assert context["pagination"]["total"] == 1
    assert routes.STATION_NAMES[canonical] in page
    assert routes.place_name(alias) == routes.STATION_NAMES[canonical]
    assert client.get(f"/stations/{alias}").location.startswith(f"/stations/{canonical}")


def test_yesterday_is_dublin_scoped_and_preserved_in_pagination(client, dashboard_data, clock):
    seed, _ = dashboard_data
    clock.instant = datetime.fromisoformat("2026-09-22T23:30:00+00:00")
    seed(*({"train_date": "2026-09-22", "origin": f"Origin {i}",
            "fetched_at": clock.instant - timedelta(hours=2)} for i in range(17)))
    assert "View yesterday" not in client.get("/stations/TARA").get_data(as_text=True)
    page = client.get("/routes?day=yesterday&station=CNLLY").get_data(as_text=True)
    assert "17 routes" in page and "day=yesterday" in page
    assert client.get("/routes?day=invalid").status_code == 404


def test_freshness_has_relative_text_and_precise_accessible_time(client, dashboard_data, clock):
    seed, _ = dashboard_data
    instant = clock.instant - timedelta(minutes=7)
    seed({"fetched_at": instant})
    page = client.get("/").get_data(as_text=True)
    assert "1/20 stations reporting · <time" in page
    assert "Updated 7 minutes ago</time>" in page
    assert f'datetime="{instant.isoformat()}"' in page
    assert 'title="22 Sep 2026, 12:53:00 IST (UTC+0100)"' in page


@pytest.mark.parametrize("minutes,expected", [
    (0, "just now"), (1, "1 minute"), (7, "7 minutes"),
    (60, "1 hour"), (720, "12 hours"), (1440, "1 day"),
])
def test_freshness_uses_readable_relative_units(minutes, expected):
    assert routes.age_text(minutes) == expected


@pytest.mark.parametrize("age,expected", [
    (timedelta(seconds=30), "Updated just now</time>"),
    (timedelta(minutes=1), "Updated 1 minute ago</time>"),
])
def test_freshness_handles_under_a_minute_and_singular(client, dashboard_data, clock, age, expected):
    seed, _ = dashboard_data
    seed({"fetched_at": clock.instant - age})
    assert expected in client.get("/").get_data(as_text=True)


@pytest.mark.parametrize("scheduled,delay,expected", [
    ("12:34", 0, ("12:34", 0)),
    ("12:34", 7, ("12:41", 0)),
    ("12:34", -2, ("12:32", 0)),
    ("23:58", 7, ("00:05", 1)),
    ("00:03", -5, ("23:58", -1)),
    ("broken", 7, (None, 0)),
])
def test_expected_time_uses_dublin_clock_and_marks_midnight(scheduled, delay, expected):
    row = SimpleNamespace(train_date="2026-09-22", scheduled_time=scheduled,
                          delay_minutes=delay)
    assert routes.expected_time(row) == expected


def test_expected_time_crosses_dublin_spring_clock_change():
    row = SimpleNamespace(train_date="2026-03-29", scheduled_time="00:59",
                          delay_minutes=2)
    assert routes.expected_time(row) == ("02:01", 0)


def test_service_boards_show_scheduled_expected_destination_and_status(client, dashboard_data):
    seed, _ = dashboard_data
    seed(
        {"train_code": "LATE", "scheduled_time": "23:58", "delay_minutes": 7},
        {"train_code": "EARLY", "scheduled_time": "00:03", "delay_minutes": -5},
        {"train_code": "ONTIME", "scheduled_time": "09:10", "delay_minutes": 0},
    )
    station = client.get("/stations/CNLLY?view=delays").get_data(as_text=True)
    assert station.index("Scheduled</th>") < station.index("Expected</th>")
    assert station.index("Expected</th>") < station.index("Destination</th>")
    assert station.index("Destination</th>") < station.index("Status</th>")
    assert "<time>00:05</time><small>+1 day</small>" in station
    assert "<time>23:58</time><small>−1 day</small>" in station
    assert 'class="expected-time unchanged"><time>09:10</time>' in station
    assert "7 min late" in station and "5 min early" in station and "On time" in station
    network = client.get("/").get_data(as_text=True)
    assert "Reported at <a" in network and "<time>00:05</time>" in network
    assert "5 min early" not in network  # The network list shows significant delays only.


def test_route_range_is_observed_scheduled_times_not_timetable(client, dashboard_data):
    seed, _ = dashboard_data
    seed(
        {"train_code": "MORNING", "scheduled_time": "06:12"},
        {"train_code": "EVENING", "scheduled_time": "18:45"},
        {"train_code": "UNKNOWN", "scheduled_time": "broken"},
    )
    page = client.get("/routes").get_data(as_text=True)
    assert "Seen today: 06:12 – 18:45" in page
    assert "not a full timetable" in page


def test_monitored_scope_excludes_historical_stations_from_network(
    app, client, dashboard_data, monkeypatch,
):
    seed, context = dashboard_data
    monkeypatch.setitem(app.config, "STATION_CODES", ("CNLLY", "TARA"))
    seed({"station": "CNLLY", "delay_minutes": 7},
         {"station": "GCDK", "delay_minutes": 20})
    page = client.get("/").get_data(as_text=True)
    assert context["coverage"] == {"reporting": 1, "total": 2}
    assert context["network"]["trains"] == 1
    assert [row.station for row in context["current_delays"]] == ["CNLLY"]
    assert "1/2 stations reporting" in page
    assert "across 2 monitored stations" in page
    assert '<a href="/stations/CNLLY">Dublin Connolly</a>' in page
    assert '<a href="/stations/GCDK">Grand Canal Dock</a>' not in page
    assert '<a href="/stations/TARA">Tara Street</a>' not in page
    assert '<option value="GCDK">Grand Canal Dock · Not currently monitored</option>' in page
    for path in ("/stations", "/status", "/stations/GCDK"):
        assert "Not currently monitored" in client.get(path).get_data(as_text=True)
