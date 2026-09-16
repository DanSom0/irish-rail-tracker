"""Dashboard and health HTTP routes."""

from flask import Blueprint, current_app, jsonify, render_template
from sqlalchemy import func, text

from app.extensions import db
from app.models import Observation

dashboard = Blueprint("dashboard", __name__)


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
    current_delays = db.session.scalars(
        db.select(Observation).order_by(Observation.fetched_at.desc()).limit(100)
    ).all()
    by_station = db.session.execute(
        db.select(Observation.station, func.avg(Observation.delay_minutes).label("average_delay"))
        .group_by(Observation.station)
        .order_by(Observation.station)
    ).all()
    by_route = db.session.execute(
        db.select(
            Observation.origin,
            Observation.destination,
            func.avg(Observation.delay_minutes).label("average_delay"),
        )
        .group_by(Observation.origin, Observation.destination)
        .order_by(Observation.origin, Observation.destination)
    ).all()
    last_updated = db.session.scalar(db.select(func.max(Observation.fetched_at)))
    return render_template(
        "dashboard.html",
        current_delays=current_delays,
        by_station=by_station,
        by_route=by_route,
        last_updated=last_updated,
    )
