"""Tests for application HTTP routes."""

from app.extensions import db
from app.models import Observation


def test_health_reports_database_connectivity(client):
    """The health endpoint succeeds when PostgreSQL is available."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"database": "connected", "status": "ok"}


def test_dashboard_renders_observations_and_aggregates(app, client):
    """The dashboard exposes current delays and both summary views."""
    with app.app_context():
        db.session.add(
            Observation(
                station="CNLLY",
                train_code="A123",
                train_date="2026-09-16",
                origin="Connolly",
                destination="Bray",
                scheduled_time="09:10",
                delay_minutes=7,
            )
        )
        db.session.commit()

    response = client.get("/")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "A123" in page
    assert "Connolly → Bray" in page
    assert "7.0 min" in page
