"""Server-rendered rail dashboard and health routes."""

from datetime import UTC, datetime, time, timedelta
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

dashboard = Blueprint("dashboard", __name__)
DUBLIN = ZoneInfo("Europe/Dublin")
STATION_NAMES = {
    "CNLLY": "Dublin Connolly", "PERSE": "Dublin Pearse", "HSTON": "Dublin Heuston",
    "TARA": "Tara Street", "MHIDE": "Malahide",
}


@dashboard.app_template_filter("station_name")
def station_name(code):
    return STATION_NAMES.get(code, f"Unlisted station ({code})")


@dashboard.app_template_filter("place_name")
def place_name(value):
    return STATION_NAMES.get(value, value)


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


def _context(station="", *, strict=False):
    """One grouped query supplies station coverage and current network figures."""
    now = datetime.now(UTC)
    local_now = now.astimezone(DUBLIN)
    cutoff = now - timedelta(minutes=30)
    recent = Observation.fetched_at.between(cutoff, now)
    today = Observation.train_date == local_now.date().isoformat()
    rows = db.session.execute(db.select(
        Observation.station,
        func.max(Observation.fetched_at).label("last_fetch"),
        func.count().filter(recent).label("readings"),
        func.avg(Observation.delay_minutes).filter(recent).label("average_delay"),
        func.avg(Observation.delay_minutes).filter(today).label("today_average"),
    ).group_by(Observation.station).order_by(Observation.station)).all()
    observed = {row.station: row for row in rows}
    stations = sorted(set(current_app.config["STATION_CODES"]) | observed.keys())
    if station not in stations:
        if strict:
            abort(404)
        station = ""
    service_hours = time(6) <= local_now.time() <= time(23, 30)
    station_status = []
    for code in stations:
        row = observed.get(code)
        last_fetch = row.last_fetch if row else None
        station_status.append({
            "station": code, "last_fetch": last_fetch,
            "stale": service_hours and (last_fetch is None or last_fetch < cutoff),
            "readings": row.readings if row else 0,
            "average_delay": row.average_delay if row else None,
            "today_average": row.today_average if row else None,
        })
    selected = [row for row in rows if not station or row.station == station]
    readings = sum(row.readings for row in selected)
    active = [row for row in selected if row.readings]
    highest_delay = max(active, key=lambda row: row.average_delay, default=None)
    last_updated = max((row.last_fetch for row in selected), default=None)
    context = {
        "stations": stations, "station": station, "station_status": station_status,
        "highest_delay": highest_delay, "last_updated": last_updated,
        "age_minutes": max(0, int((now - last_updated).total_seconds() // 60))
        if last_updated else None,
        "network": {
            "readings": readings,
            "reporting": len(active), "total": 1 if station else len(stations),
        },
        "service_hours": service_hours, "today": local_now.strftime("%d %b %Y"),
        "today_date": local_now.date().isoformat(), "cutoff": cutoff, "now": now,
    }
    current = _train_summary(context, recent=True)
    context["network"].update(
        trains=current.trains, on_time=current.on_time,
        average_delay=current.average_delay, major=current.major,
    )
    return context


def _latest_readings(context, *, recent=False):
    """One latest stored station reading per train/date, within the selected scope."""
    query = db.select(Observation).where(Observation.fetched_at <= context["now"])
    if recent:
        query = query.where(Observation.fetched_at >= context["cutoff"])
    else:
        query = query.where(Observation.train_date == context["today_date"])
    if context["station"]:
        query = query.where(Observation.station == context["station"])
    return query.distinct(Observation.train_code, Observation.train_date).order_by(
        Observation.train_code, Observation.train_date,
        Observation.fetched_at.desc(), Observation.id.desc(),
    ).subquery()


def _train_summary(context, *, recent=False):
    latest = _latest_readings(context, recent=recent).c
    return db.session.execute(db.select(
        func.count().label("trains"),
        (100.0 * func.count().filter(latest.delay_minutes.between(0, 1))
         / func.nullif(func.count(), 0)).label("on_time"),
        func.avg(latest.delay_minutes).label("average_delay"),
        func.count().filter(latest.delay_minutes >= 6).label("major"),
    )).one()


def _services(context, view="delays"):
    latest = _latest_readings(context, recent=True).c
    query = db.select(*latest)
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
    services = db.session.execute(_services(context).limit(5)).all()
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
    latest = _latest_readings(context).c
    query = db.select(
        latest.origin, latest.destination,
        func.avg(latest.delay_minutes).label("average_delay"),
        func.count().label("trains"),
    ).group_by(latest.origin, latest.destination).order_by(
        func.avg(latest.delay_minutes).desc(), latest.origin, latest.destination,
    )
    return render_template(
        "routes.html", title="Route performance", active="routes", **context,
        pagination=_paginate(query),
    )


@dashboard.get("/status")
def status():
    context = _context()
    context["station_status"].sort(key=lambda row: (not row["stale"], row["station"]))
    return render_template("status.html", title="Data status", active="status", **context)


@dashboard.app_errorhandler(404)
def not_found(error):
    return render_template(
        "404.html", title="Page not found",
        active="stations" if request.path.startswith("/stations/") else "",
    ), 404
