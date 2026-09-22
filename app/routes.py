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
from sqlalchemy import func, text

from app.extensions import db
from app.models import Observation

dashboard = Blueprint("dashboard", __name__)
DUBLIN = ZoneInfo("Europe/Dublin")


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
        func.count().filter(recent, Observation.delay_minutes.between(0, 1)).label("on_time"),
        func.count().filter(recent, Observation.delay_minutes >= 6).label("major"),
        func.sum(Observation.delay_minutes).filter(recent).label("delay_sum"),
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
    return {
        "stations": stations, "station": station, "station_status": station_status,
        "highest_delay": highest_delay, "last_updated": last_updated,
        "age_minutes": max(0, int((now - last_updated).total_seconds() // 60))
        if last_updated else None,
        "network": {
            "readings": readings,
            "on_time": 100 * sum(row.on_time for row in selected) / readings if readings else None,
            "average_delay": sum(row.delay_sum or 0 for row in selected) / readings
            if readings else None,
            "major": sum(row.major for row in selected),
            "reporting": len(active), "total": 1 if station else len(stations),
        },
        "service_hours": service_hours, "today": local_now.strftime("%d %b %Y"),
        "today_date": local_now.date().isoformat(), "cutoff": cutoff, "now": now,
    }


def _daily_summary(context):
    query = db.select(
        func.count(func.distinct(Observation.train_code)).label("trains"),
        (100.0 * func.count().filter(Observation.delay_minutes.between(0, 1))
         / func.nullif(func.count(), 0)).label("on_time"),
        func.avg(Observation.delay_minutes).label("average_delay"),
    ).where(Observation.train_date == context["today_date"])
    if context["station"]:
        query = query.where(Observation.station == context["station"])
    return db.session.execute(query).one()


def _services(context):
    query = db.select(
        Observation.station, Observation.train_code, Observation.origin,
        Observation.destination, Observation.scheduled_time, Observation.delay_minutes,
    ).where(Observation.fetched_at.between(context["cutoff"], context["now"]))
    if context["station"]:
        query = query.where(Observation.station == context["station"])
    return query.order_by(
        Observation.delay_minutes.desc(), Observation.fetched_at.desc(), Observation.id,
    )


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
    return render_template(
        "dashboard.html", title="Overview", active="overview", **context,
        summary=_daily_summary(context),
        current_delays=db.session.execute(_services(context).limit(5)).all(),
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
    return render_template(
        "station.html", title=f"{code} station", active="stations", **context,
        summary=_daily_summary(context), pagination=_paginate(_services(context)),
    )


@dashboard.get("/routes")
def route_averages():
    context = _context(request.args.get("station", ""))
    query = db.select(
        Observation.origin, Observation.destination,
        func.avg(Observation.delay_minutes).label("average_delay"),
        func.count().label("readings"),
    ).where(Observation.train_date == context["today_date"])
    if context["station"]:
        query = query.where(Observation.station == context["station"])
    query = query.group_by(Observation.origin, Observation.destination).order_by(
        func.avg(Observation.delay_minutes).desc(), Observation.origin, Observation.destination,
    )
    return render_template(
        "routes.html", title="Routes", active="routes", **context,
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
