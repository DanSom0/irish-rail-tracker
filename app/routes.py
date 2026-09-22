"""Dashboard and health HTTP routes."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, jsonify, render_template, request
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


@dashboard.get("/")
def index():
    """Render current train delay and aggregate views."""
    now = datetime.now(UTC)
    local_now = now.astimezone(DUBLIN)
    cutoff = now - timedelta(minutes=30)
    station_fetches = dict(db.session.execute(
        db.select(Observation.station, func.max(Observation.fetched_at))
        .group_by(Observation.station)
    ).all())
    stations = sorted(set(current_app.config["STATION_CODES"]) | station_fetches.keys())
    station = request.args.get("station", "")
    if station not in stations:
        station = ""
    selected = [Observation.station == station] if station else []
    today = [*selected, Observation.train_date == local_now.date().isoformat()]
    recent = [*selected, Observation.fetched_at >= cutoff]

    current_delays = db.session.scalars(
        db.select(Observation).where(*recent)
        .order_by(Observation.fetched_at.desc(), Observation.station, Observation.train_code)
        .limit(100)
    ).all()
    summary = db.session.execute(
        db.select(
            func.count(func.distinct(Observation.train_code)).label("trains"),
            (100.0 * func.count().filter(Observation.delay_minutes.between(0, 1))
             / func.nullif(func.count(), 0)).label("on_time"),
            func.avg(Observation.delay_minutes).label("average_delay"),
        ).where(*today)
    ).one()
    highest_delay = db.session.execute(
        db.select(Observation.station, func.avg(Observation.delay_minutes).label("average_delay"))
        .where(*recent)
        .group_by(Observation.station)
        .order_by(func.avg(Observation.delay_minutes).desc(), Observation.station)
        .limit(1)
    ).first()
    by_station = db.session.execute(
        db.select(Observation.station, func.avg(Observation.delay_minutes).label("average_delay"))
        .where(*today)
        .group_by(Observation.station)
        .order_by(Observation.station)
    ).all()
    by_route = db.session.execute(
        db.select(
            Observation.origin,
            Observation.destination,
            func.avg(Observation.delay_minutes).label("average_delay"),
        )
        .where(*today)
        .group_by(Observation.origin, Observation.destination)
        .order_by(Observation.origin, Observation.destination)
    ).all()
    last_updated = (
        station_fetches.get(station) if station else max(station_fetches.values(), default=None)
    )
    service_hours = time(6) <= local_now.time() <= time(23, 30)
    station_status = [
        {
            "station": code,
            "last_fetch": station_fetches.get(code),
            "stale": service_hours and (
                code not in station_fetches or station_fetches[code] < cutoff
            ),
        }
        for code in stations
    ]
    return render_template(
        "dashboard.html",
        current_delays=current_delays,
        by_station=by_station,
        by_route=by_route,
        last_updated=last_updated,
        age_minutes=max(0, int((now - last_updated).total_seconds() // 60))
        if last_updated else None,
        stations=stations,
        station=station,
        summary=summary,
        highest_delay=highest_delay,
        station_status=station_status,
        service_hours=service_hours,
        today=local_now.strftime("%d %b %Y"),
    )
