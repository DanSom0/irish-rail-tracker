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
from sqlalchemy import DateTime, case, cast, func, text, tuple_

from app.extensions import db
from app.models import Observation
from app.stations import (
    PLACE_NAMES,
    STATION_ALIASES,
    STATION_NAMES,
    canonical_station,
    station_codes,
)

dashboard = Blueprint("dashboard", __name__)
DUBLIN = ZoneInfo("Europe/Dublin")
CURRENT_WINDOW = timedelta(minutes=10)


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


@dashboard.app_template_filter("expected_time")
def expected_time(row):
    try:
        scheduled = datetime.combine(
            date.fromisoformat(row.train_date), time.fromisoformat(row.scheduled_time), DUBLIN,
        )
    except ValueError:
        return None, 0
    expected = (scheduled.astimezone(UTC) + timedelta(minutes=row.delay_minutes)).astimezone(DUBLIN)
    return expected.strftime("%H:%M"), (expected.date() - scheduled.date()).days


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


def _station_fetches(observations):
    return db.select(
        observations.station,
        func.max(observations.fetched_at).label("last_fetch"),
    ).group_by(observations.station).subquery()


def _context(station="", *, strict=False):
    """One grouped query supplies station coverage and current network figures."""
    station = canonical_station(station)
    observations = _observations()
    now = datetime.now(UTC)
    local_now = now.astimezone(DUBLIN)
    cutoff = now - timedelta(minutes=30)
    current_cutoff = now - CURRENT_WINDOW
    recent = observations.fetched_at.between(cutoff, now)
    station_fetches = _station_fetches(observations)
    current = (observations.fetched_at == station_fetches.c.last_fetch) & (
        observations.fetched_at >= current_cutoff
    )
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
    ).join(station_fetches, observations.station == station_fetches.c.station)
        .group_by(observations.station).order_by(observations.station)).all()
    observed = {row.station: row for row in rows}
    monitored_stations = station_codes(current_app.config["STATION_CODES"])
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
        station_status.append({
            "station": code, "monitored": code in configured, "last_fetch": last_fetch,
            "stale": code in configured and service_hours and (last_fetch is None or last_fetch < cutoff),
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
    last_updated = max((row.last_fetch for row in selected), default=None)
    coverage = {
        "reporting": sum(bool(observed[code].readings) for code in configured if code in observed),
        "total": len(configured),
    }
    remembered = canonical_station(request.cookies.get("station", ""))
    context = {
        "coverage": coverage,
        "yesterday": yesterday.isoformat(),
        "yesterday_url": url_for("dashboard.route_averages", day="yesterday", station=station)
        if any(row.yesterday_readings for row in selected) else None,
        "empty_reason": "Overnight service can be sparse; empty fetches and collection gaps are not recorded."
        if not service_hours else "Coverage is limited to stored readings; empty fetches and collection gaps are not recorded.",

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
        },
        "service_hours": service_hours, "today": local_now.strftime("%d %b %Y"),
        "today_date": local_now.date().isoformat(), "current_cutoff": current_cutoff,
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
        station_fetches = _station_fetches(observations)
        query = query.join(station_fetches, observations.station == station_fetches.c.station)
        query = query.where(
            observations.fetched_at == station_fetches.c.last_fetch,
            observations.fetched_at >= context["current_cutoff"],
        )
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


def _services(context, view="delays", *, significant=False):
    latest = _latest_readings(context, recent=True).c
    query = db.select(*latest)
    if significant:
        query = query.where(latest.delay_minutes >= 2)
    if view == "next":
        # Stored times can include arrivals; only valid local schedules support this view.
        scheduled = case((
            latest.scheduled_time.op("~")(r"^([01][0-9]|2[0-3]):[0-5][0-9]$"),
            cast(latest.train_date + " " + latest.scheduled_time, DateTime),
        ))
        expected = func.timezone("Europe/Dublin", scheduled) + (
            latest.delay_minutes * text("INTERVAL '1 minute'")
        )
        return query.where(expected >= context["now"]).order_by(expected, latest.id)
    return query.order_by(
        latest.delay_minutes.desc(), latest.fetched_at.desc(), latest.id.desc(),
    )


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


def _paginate(query, per_page=15):
    """Paginate grouped rows as well as services, without loading the full result."""
    page = max(1, request.args.get("page", 1, type=int))
    total = db.session.scalar(db.select(func.count()).select_from(query.order_by(None).subquery()))
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        abort(404)
    return {
        "items": db.session.execute(query.limit(per_page).offset((page - 1) * per_page)).all(),
        "page": page, "pages": pages, "total": total,
    }


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
    pagination = _paginate(_services(context, view))
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


@dashboard.get("/status")
def status():
    context = _context()
    return render_template("status.html", title="Data status", active="status", **context)


@dashboard.app_errorhandler(404)
def not_found(error):
    return render_template(
        "404.html", title="Page not found",
        active="stations" if request.path.startswith("/stations/") else "", **_context(),
    ), 404
