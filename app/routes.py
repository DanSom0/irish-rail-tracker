"""Server-rendered rail dashboard and health routes."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import Date, DateTime, Integer, case, cast, func, text, tuple_

from app.disk import check_disk_usage
from app.extensions import db
from app.models import Observation, StationPoll, WorkerHeartbeat
from app.stations import (
    PLACE_NAMES,
    STATION_ALIASES,
    STATION_COORDINATES,
    STATION_NAMES,
    canonical_station,
    station_codes,
)
from app.train_positions import current_trains

dashboard = Blueprint("dashboard", __name__)
DUBLIN = ZoneInfo("Europe/Dublin")
# A station's board is stale once its latest successful poll is this old, or its last poll failed.
STALE_AFTER = timedelta(minutes=15)
# /health/ingestion is ready while the worker completed a cycle within this time.
HEARTBEAT_MAX_AGE = timedelta(minutes=15)


@dashboard.app_template_filter("station_name")
def station_name(code):
    return STATION_NAMES.get(canonical_station(code), code)


@dashboard.app_template_filter("sort_stations")
def sort_stations(codes):
    return list(station_codes(codes))


@dashboard.app_template_filter("place_name")
def place_name(value):
    return STATION_NAMES.get(canonical_station(value), PLACE_NAMES.get(value.strip().casefold(), value))


@dashboard.app_template_filter("delay_text")
def delay_text(value):
    if value == 0:
        return "On time"
    amount = f"{abs(value):.1f}".rstrip("0").rstrip(".")
    if amount == "0":
        amount = "<0.1"
    return f"{amount} min {'early' if value < 0 else 'late'}"


@dashboard.app_template_filter("minutes_ago")
def minutes_ago(value):
    return max(0, int((datetime.now(UTC) - value).total_seconds() // 60))


@dashboard.app_template_filter("age_text")
def age_text(minutes):
    if minutes == 0:
        return "just now"
    for unit, duration in (("day", 1440), ("hour", 60)):
        if minutes >= duration:
            count = minutes // duration
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


@dashboard.app_template_filter("observation_age")
def observation_age(value):
    return f"Updated {age_text(minutes_ago(datetime.fromisoformat(value)))} ago"


@dashboard.app_template_filter("observation_title")
def observation_title(value):
    return dublin_time(datetime.fromisoformat(value))


def scheduled_and_expected(row):
    """Scheduled and expected Dublin datetimes for a reading, or None if unparseable.

    The one conversion used for display and filtering. A wall time repeated when
    clocks go back is its first occurrence (Irish Summer Time); a time skipped when
    clocks go forward uses the pre-change offset, so 01:30 on that date is 02:30.
    """
    try:
        scheduled = datetime.combine(
            date.fromisoformat(row.train_date), time.fromisoformat(row.scheduled_time), DUBLIN,
        )
    except ValueError:
        return None
    expected = (scheduled.astimezone(UTC) + timedelta(minutes=row.delay_minutes)).astimezone(DUBLIN)
    return scheduled, expected


@dashboard.app_template_filter("expected_time")
def expected_time(row):
    times = scheduled_and_expected(row)
    if times is None:
        return None, 0
    scheduled, expected = times
    return expected.strftime("%H:%M"), (expected.date() - scheduled.date()).days


@dashboard.app_template_filter("clocks_went_back")
def clocks_went_back(row):
    """True when a not-early train's expected clock reads earlier than its schedule."""
    times = scheduled_and_expected(row)
    if times is None:
        return False
    scheduled, expected = times
    # Same-zone datetimes compare by wall clock, so compare instants in UTC.
    return (expected.astimezone(UTC) >= scheduled.astimezone(UTC)
            and expected.replace(tzinfo=None) < scheduled.replace(tzinfo=None))


@dashboard.app_template_filter("service_kind")
def service_kind(row):
    """Infer board direction from place names; old readings need no new storage."""
    name = station_name(row.station).casefold()

    def matches(value):
        return place_name(value.strip().upper()).casefold() == name or value.strip().casefold() == name

    if matches(row.destination):
        return "Terminates here"
    if matches(row.origin):
        return "Starts here"
    return "Calling service"


@dashboard.after_request
def remember_station(response):
    code = request.args.get("station") if request.endpoint == "dashboard.stations" else None
    if request.endpoint == "dashboard.station_detail":
        code = request.view_args["code"]
    if code and response.status_code in (200, 302) and (
        canonical_station(code) in STATION_NAMES or code in current_app.config["STATION_CODES"]
    ):
        response.set_cookie("station", canonical_station(code), max_age=90 * 24 * 60 * 60,
                            httponly=True, samesite="Lax", secure=request.is_secure)
    return response


@dashboard.get("/forget-station")
def forget_station():
    response = redirect(url_for("dashboard.index"))
    response.delete_cookie("station", httponly=True, samesite="Lax")
    return response


@dashboard.get("/about/data")
def about_data():
    return render_template("about_data.html", title="How the data works", active="", **_context())


@dashboard.app_template_filter("dublin_time")
def dublin_time(value):
    """Display stored UTC timestamps with an unambiguous local offset."""
    return value.astimezone(DUBLIN).strftime("%d %b %Y, %H:%M:%S %Z (UTC%z)")


@dashboard.get("/health")
def health():
    """Report whether the application can reach PostgreSQL."""
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        current_app.logger.exception("database health check failed")
        return jsonify(status="unhealthy", database="disconnected"), 503
    return jsonify(status="ok", database="connected")


@dashboard.get("/health/ingestion")
def ingestion_health():
    """Readiness of data collection: 503 unless the worker completed a cycle recently.

    Station errors are reported but do not fail the check on their own, and an empty
    board (normal overnight) is a successful poll.
    """
    now = datetime.now(UTC)
    monitored = station_codes(current_app.config["STATION_CODES"])
    try:
        completed_at = db.session.scalar(db.select(WorkerHeartbeat.last_cycle_completed_at))
        polls = _poll_states(monitored, now)
    except Exception:
        current_app.logger.exception("ingestion health check failed")
        return jsonify(status="unhealthy", database="disconnected"), 503
    stations = {"ok": 0, "empty": 0, "error": 0, "awaiting_first_poll": 0}
    for poll in polls.values():
        stations[poll["outcome"]] += 1
    ready = completed_at is not None and now - completed_at < HEARTBEAT_MAX_AGE
    body = {
        "status": "ok" if ready else "not ready",
        "heartbeat": {
            "last_cycle_completed_at": completed_at.isoformat() if completed_at else None,
            "age_seconds": max(0, int((now - completed_at).total_seconds())) if completed_at else None,
            "max_age_seconds": int(HEARTBEAT_MAX_AGE.total_seconds()),
        },
        "stations": stations,
    }
    return jsonify(body), 200 if ready else 503


def _observations():
    """Read legacy alias rows as one station, keeping the newest train reading."""
    station = case(STATION_ALIASES, value=Observation.station, else_=Observation.station)
    return db.select(
        *(column for column in Observation.__table__.columns if column.name != "station"),
        station.label("station"),
    ).where(Observation.fetched_at <= datetime.now(UTC)).distinct(
        station, Observation.train_code, Observation.train_date,
    ).order_by(station, Observation.train_code, Observation.train_date,
               Observation.fetched_at.desc(), Observation.id.desc()).subquery().c


def _successful_polls(monitored_stations):
    """Each monitored station's latest successful poll."""
    return db.select(StationPoll.station_code, StationPoll.succeeded_at).where(
        StationPoll.station_code.in_(monitored_stations), StationPoll.succeeded_at.is_not(None),
    ).subquery()


def _current_station_poll(observations, polls):
    """Exactly the readings saved by the station's latest successful poll: they share its time."""
    return (observations.station == polls.c.station_code) & (
        observations.fetched_at == polls.c.succeeded_at
    )


def _poll_states(monitored_stations, now):
    """Latest poll outcome per monitored station; no record yet means awaiting a first poll."""
    polls = {poll.station_code: poll for poll in db.session.scalars(
        db.select(StationPoll).where(StationPoll.station_code.in_(monitored_stations))
    )}
    states = {}
    for code in monitored_stations:
        poll = polls.get(code)
        succeeded_at = poll.succeeded_at if poll else None
        states[code] = {
            "outcome": poll.outcome if poll else "awaiting_first_poll",
            "attempted_at": poll.attempted_at if poll else None,
            "succeeded_at": succeeded_at,
            "train_count": poll.train_count if poll else None,
            "error_reason": poll.error_reason if poll else None,
            # Data from an earlier successful poll is shown, but marked as not current.
            "stale": succeeded_at is not None and (
                poll.outcome == "error" or now - succeeded_at >= STALE_AFTER
            ),
        }
    return states


@dashboard.app_template_filter("poll_text")
def poll_text(poll):
    """One wording for a station's latest poll, shared by the status page, boards and map API."""
    outcome = poll["outcome"]
    if outcome == "awaiting_first_poll":
        return "Awaiting first poll"
    if outcome == "error":
        if poll["succeeded_at"] is None:
            return "Update failed; no data yet"
        return f"Update failed; showing data from {poll['succeeded_at'].astimezone(DUBLIN):%H:%M}"
    text = ("No services returned" if outcome == "empty"
            else f"OK, {poll['train_count']} train{'' if poll['train_count'] == 1 else 's'}")
    if poll["stale"]:
        text += f"; not updated since {poll['succeeded_at'].astimezone(DUBLIN):%H:%M}"
    return text


def _poll_json(poll):
    return {
        **{key: value.isoformat() if isinstance(value, date) else value
           for key, value in poll.items()},
        "text": poll_text(poll),
    }


def _current_station_rows(monitored_stations):
    """Each monitored station's current board: the readings of its latest successful poll."""
    observations = _observations()
    polls = _successful_polls(monitored_stations)
    return db.session.execute(db.select(*observations).join(
        polls, _current_station_poll(observations, polls),
    ).order_by(observations.station, observations.scheduled_time, observations.train_code)).all()


def _network_stations(now):
    monitored = station_codes(current_app.config["STATION_CODES"])
    rows = _current_station_rows(monitored)
    polls = _poll_states(monitored, now)
    by_station = {code: [] for code in monitored}
    for row in rows:
        expected, day_offset = expected_time(row)
        by_station[row.station].append({
            "train_code": row.train_code,
            "train_date": row.train_date,
            "origin": place_name(row.origin),
            "destination": place_name(row.destination),
            "direction": "arrival" if service_kind(row) == "Terminates here" else "departure",
            "scheduled": row.scheduled_time,
            "expected": expected,
            "expected_day_offset": day_offset,
            "clocks_went_back": clocks_went_back(row),
            "delay": row.delay_minutes,
            "reading_at": row.fetched_at.isoformat(),
        })
    stations = []
    for code in monitored:
        trains = by_station[code]
        average = sum(max(0, train["delay"]) for train in trains) / len(trains) if trains else None
        status = ("no recent data" if average is None else "on time" if average <= 1
                  else "minor delay" if average < 6 else "significant delay")
        coordinates = STATION_COORDINATES.get(code)
        succeeded_at = polls[code]["succeeded_at"]
        stations.append({
            "code": code, "name": station_name(code),
            "lat": coordinates[0] if coordinates else None,
            "lon": coordinates[1] if coordinates else None,
            "status": status,
            "average_reported_delay": average,
            # The time of the board shown: the latest successful poll, even an empty one.
            "latest_observation_at": succeeded_at.isoformat() if succeeded_at else None,
            "poll": _poll_json(polls[code]),
            "current_trains": trains,
        })
    return stations


def _context(station="", *, strict=False):
    """One grouped query supplies station coverage and current network figures."""
    station = canonical_station(station)
    observations = _observations()
    now = datetime.now(UTC)
    local_now = now.astimezone(DUBLIN)
    cutoff = now - timedelta(minutes=30)
    recent = observations.fetched_at.between(cutoff, now)
    monitored_stations = station_codes(current_app.config["STATION_CODES"])
    polls = _successful_polls(monitored_stations)
    current = _current_station_poll(observations, polls)
    yesterday = local_now.date() - timedelta(days=1)
    today = observations.train_date == local_now.date().isoformat()
    rows = db.session.execute(db.select(
        observations.station,
        func.count().filter(observations.train_date == yesterday.isoformat()).label("yesterday_readings"),
        func.max(observations.fetched_at).label("last_fetch"),
        func.count().filter(recent).label("readings"),
        func.count().filter(current).label("current_readings"),
        func.avg(observations.delay_minutes).filter(current).label("average_delay"),
        func.avg(observations.delay_minutes).filter(today).label("today_average"),
    ).outerjoin(polls, observations.station == polls.c.station_code)
        .group_by(observations.station).order_by(observations.station)).all()
    observed = {row.station: row for row in rows}
    poll_states = _poll_states(monitored_stations, now)
    configured = set(monitored_stations)
    stations = sort_stations(configured | observed.keys())
    if station not in stations and station not in STATION_NAMES:
        if strict:
            abort(404)
        station = ""
    service_hours = time(6) <= local_now.time() <= time(23, 30)
    station_status = []
    for code in stations:
        row = observed.get(code)
        last_fetch = row.last_fetch if row else None
        poll = poll_states.get(code)
        station_status.append({
            "station": code, "monitored": code in configured, "last_fetch": last_fetch,
            "poll": poll, "stale": bool(poll and poll["stale"]),
            "readings": row.readings if row else 0,
            "current_readings": row.current_readings if row else 0,
            "average_delay": row.average_delay if row else None,
            "today_average": row.today_average if row else None,
        })
    selected = [row for row in rows if row.station == station] if station else [
        row for row in rows if row.station in configured
    ]
    readings = sum(row.current_readings for row in selected)
    active = [row for row in selected if row.current_readings]
    highest_delay = max(active, key=lambda row: row.average_delay, default=None)
    # Monitored boards are as fresh as their latest successful poll, even an empty one.
    scope = [station] if station in configured else [] if station else monitored_stations
    last_updated = max(
        (poll_states[code]["succeeded_at"] for code in scope if poll_states[code]["succeeded_at"]),
        default=None,
    ) if scope else max((row.last_fetch for row in selected), default=None)
    coverage = {
        "reporting": sum(poll_states[code]["outcome"] in ("ok", "empty") and not poll_states[code]["stale"]
                         for code in configured),
        "total": len(configured),
    }
    remembered = canonical_station(request.cookies.get("station", ""))
    context = {
        "coverage": coverage,
        "yesterday": yesterday.isoformat(),
        "yesterday_url": url_for("dashboard.route_averages", day="yesterday", station=station)
        if any(row.yesterday_readings for row in selected) else None,
        "empty_reason": "Overnight service can be sparse; see Data status for each station's latest update."
        if not service_hours else "See Data status for each station's latest update.",
        "remembered_station": remembered if remembered in stations or remembered in STATION_NAMES else "",
        "stations": stations, "station": station, "station_status": station_status,
        "monitored_stations": monitored_stations,
        "reporting_shortcuts": [code for code in ("CNLLY", "PERSE", "TARA", "HSTON", "GCDK")
                                if code in configured and code in observed
                                and observed[code].current_readings],
        "any_stored_readings": bool(rows),
        "has_station_metrics": any(row["current_readings"] or row["today_average"] is not None
                                   for row in station_status),
        "highest_delay": highest_delay, "last_updated": last_updated,
        "age_minutes": max(0, int((now - last_updated).total_seconds() // 60))
        if last_updated else None,
        "network": {
            "readings": readings,
            "reporting": len(active) if not station or station in configured else 0,
            "total": int(station in configured) if station else len(configured),
            "stale": sum(poll_states[code]["stale"] for code in scope),
        },
        "station_poll": poll_states.get(station),
        "service_hours": service_hours, "today": local_now.strftime("%d %b %Y"),
        "today_date": local_now.date().isoformat(),
        "now": now,
    }
    current = _train_summary(context, recent=True)
    context["network"].update(
        trains=current.trains, on_time=current.on_time,
        average_delay=current.average_delay, major=current.major,
    )
    return context


def _latest_readings(context, *, recent=False):
    """One latest stored station reading per train/date, within the selected scope."""
    observations = _observations()
    query = db.select(*observations)
    if recent:
        polls = _successful_polls(context["monitored_stations"])
        query = query.join(polls, _current_station_poll(observations, polls))
    else:
        query = query.where(observations.train_date == context.get("report_date", context["today_date"]))
    if context["station"]:
        query = query.where(observations.station == context["station"])
    else:
        query = query.where(observations.station.in_(context["monitored_stations"]))
    return query.distinct(observations.train_code, observations.train_date).order_by(
        observations.train_code, observations.train_date,
        observations.fetched_at.desc(), observations.id.desc(),
    ).subquery()


def _train_summary(context, *, recent=False):
    latest = _latest_readings(context, recent=recent).c
    return db.session.execute(db.select(
        func.count().label("trains"),
        (100.0 * func.count().filter(latest.delay_minutes <= 1)
         / func.nullif(func.count(), 0)).label("on_time"),
        func.avg(latest.delay_minutes).label("average_delay"),
        func.count().filter(latest.delay_minutes >= 6).label("major"),
    )).one()


def _services(context, *, significant=False):
    latest = _latest_readings(context, recent=True).c
    query = db.select(*latest)
    if significant:
        query = query.where(latest.delay_minutes >= 2)
    return query.order_by(
        latest.delay_minutes.desc(), latest.fetched_at.desc(), latest.id.desc(),
    )


def _next_services(context):
    """Current services not yet expected, ordered with the same conversion as the board."""
    latest = _latest_readings(context, recent=True).c
    # Stored times can include arrivals; only valid local schedules support this view.
    rows = db.session.execute(db.select(*latest).where(
        latest.scheduled_time.op("~")(r"^([01][0-9]|2[0-3]):[0-5][0-9]$"),
    )).all()
    upcoming = [
        (times[1].astimezone(UTC), row.id, row) for row in rows
        if (times := scheduled_and_expected(row)) and times[1] >= context["now"]
    ]
    return [row for _, _, row in sorted(upcoming)]


def _reading_details(rows, now):
    """Load available earlier station readings in one query, not one per service."""
    if not rows:
        return {}
    readings = db.session.execute(db.select(Observation).where(
        tuple_(Observation.train_code, Observation.train_date).in_(
            [(row.train_code, row.train_date) for row in rows]
        ),
        Observation.fetched_at <= now,
    ).order_by(Observation.fetched_at.desc(), Observation.id.desc())).scalars().all()
    grouped = {}
    for reading in readings:
        grouped.setdefault((reading.train_code, reading.train_date), []).append(reading)
    return {
        row.id: [reading for reading in grouped[(row.train_code, row.train_date)]
                 if reading.id != row.id and reading.fetched_at <= row.fetched_at]
        for row in rows
    }


def _page(total, per_page):
    page = max(1, request.args.get("page", 1, type=int))
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        abort(404)
    return page, pages


def _paginate(query, per_page=15):
    """Paginate grouped rows as well as services, without loading the full result."""
    total = db.session.scalar(db.select(func.count()).select_from(query.order_by(None).subquery()))
    page, pages = _page(total, per_page)
    return {
        "items": db.session.execute(query.limit(per_page).offset((page - 1) * per_page)).all(),
        "page": page, "pages": pages, "total": total,
    }


def _paginate_rows(rows, per_page=15):
    page, pages = _page(len(rows), per_page)
    start = (page - 1) * per_page
    return {"items": rows[start:start + per_page], "page": page, "pages": pages, "total": len(rows)}


@dashboard.get("/")
def index():
    context = _context(request.args.get("station", ""))
    services = db.session.execute(_services(context, significant=True).limit(5)).all()
    return render_template(
        "dashboard.html", title="Overview", active="overview", **context,
        summary=_train_summary(context), current_delays=services,
        reading_details=_reading_details(services, context["now"]),
    )


@dashboard.get("/stations")
def stations():
    context = _context(request.args.get("station", ""))
    if context["station"]:
        return redirect(url_for("dashboard.station_detail", code=context["station"]))
    return render_template("stations.html", title="Stations", active="stations", **context)


@dashboard.get("/stations/<code>")
def station_detail(code):
    if code != canonical_station(code):
        return redirect(url_for("dashboard.station_detail", code=canonical_station(code),
                                view=request.args.get("view", "next")))
    context = _context(code, strict=True)
    view = "next" if request.args.get("view", "next") == "next" else "delays"
    pagination = (_paginate_rows(_next_services(context)) if view == "next"
                  else _paginate(_services(context)))
    return render_template(
        "station.html", title=station_name(code), active="stations", **context,
        summary=_train_summary(context), pagination=pagination, view=view,
        reading_details=_reading_details(pagination["items"], context["now"]),
    )


@dashboard.get("/routes")
def route_averages():
    context = _context(request.args.get("station", ""))
    day = request.args.get("day", "today")
    if day not in ("today", "yesterday"):
        abort(404)
    if day == "yesterday":
        context["report_date"] = context["yesterday"]
        context["today"] = datetime.fromisoformat(context["yesterday"]).strftime("%d %b %Y")
    latest = _latest_readings(context).c
    valid_time = latest.scheduled_time.op("~")(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    query = db.select(
        latest.origin, latest.destination,
        func.avg(latest.delay_minutes).label("average_delay"),
        func.count().label("trains"),
        func.min(latest.scheduled_time).filter(valid_time).label("first_scheduled"),
        func.max(latest.scheduled_time).filter(valid_time).label("last_scheduled"),
    ).group_by(latest.origin, latest.destination).order_by(
        func.avg(latest.delay_minutes).desc(), latest.origin, latest.destination,
    )
    return render_template(
        "routes.html", title="Route performance", active="routes", **context,
        pagination=_paginate(query), day=day,
    )


def _delay_patterns_query(station, monitored_stations):
    """Aggregate latest train–station readings by scheduled Dublin wall time."""
    observations = _observations()
    valid_time = observations.scheduled_time.op("~")(r"^(0[5-9]|1[0-9]|2[0-3]):[0-5][0-9]$")
    scheduled = cast(observations.train_date + " " + observations.scheduled_time, DateTime)
    weekday = cast(func.extract("isodow", scheduled), Integer)
    hour = cast(func.extract("hour", scheduled), Integer)
    scheduled_date = cast(scheduled, Date)
    scope = [station] if station else monitored_stations
    return db.select(
        weekday.label("weekday"), hour.label("hour"),
        func.count().label("readings"),
        func.avg(func.greatest(observations.delay_minutes, 0)).label("average_delay"),
        func.min(func.min(scheduled_date)).over().label("first_date"),
        func.max(func.max(scheduled_date)).over().label("last_date"),
    ).where(observations.station.in_(scope), valid_time).group_by(weekday, hour).order_by(
        weekday, hour,
    )


@dashboard.get("/patterns")
def delay_patterns():
    context = _context(request.args.get("station", ""))
    rows = db.session.execute(_delay_patterns_query(
        context["station"], context["monitored_stations"],
    )).all()
    cells = {(row.weekday, row.hour): row for row in rows}
    worst = max((row for row in rows if row.readings >= 10),
                key=lambda row: row.average_delay, default=None)
    return render_template(
        "patterns.html", title="Delays by day and hour", active="patterns", **context,
        cells=cells, worst=worst, days_with_data={row.weekday for row in rows},
        first_date=rows[0].first_date if rows else None,
        last_date=rows[0].last_date if rows else None,
        days=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        hours=range(5, 24),
    )


@dashboard.get("/api/network")
def network_api():
    return jsonify(stations=_network_stations(datetime.now(UTC)))


@dashboard.get("/api/trains")
def trains_api():
    return jsonify(current_trains(
        current_app.config["IRISH_RAIL_TRAINS_API_URL"],
        current_app.config["TRAINS_REQUEST_TIMEOUT_SECONDS"],
        current_app.config["STATION_CODES"],
    ))


@dashboard.get("/map")
def live_map():
    context = _context()
    selected = canonical_station(request.args.get("station", ""))
    if selected and selected not in context["monitored_stations"]:
        abort(404)
    return render_template(
        "map.html", title="Live station delays", active="map", **context,
        map_stations=_network_stations(context["now"]), map_selected=selected,
    )


@dashboard.get("/status")
def status():
    context = _context()
    return render_template("status.html", title="Data status", active="status", **context,
                           disk=check_disk_usage())


@dashboard.app_errorhandler(404)
def not_found(error):
    return render_template(
        "404.html", title="Page not found",
        active="stations" if request.path.startswith("/stations/") else "", **_context(),
    ), 404
