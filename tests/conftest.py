"""Shared test application and PostgreSQL database fixtures."""

import os

import pytest
from sqlalchemy import delete

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import Observation


class TestConfig(Config):
    """Configuration for tests backed by the disposable PostgreSQL database."""

    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://irishrail:irishrail@localhost:5432/irishrail_test",
    )


@pytest.fixture(scope="session")
def app():
    """Create the application and its schema once for the test session."""
    application = create_app(TestConfig)
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def clear_observations(app):
    """Keep each test independent while retaining the test schema."""
    with app.app_context():
        db.session.execute(delete(Observation))
        db.session.commit()
    yield
    with app.app_context():
        db.session.rollback()
        db.session.remove()


@pytest.fixture
def client(app):
    """Return a Flask test client."""
    return app.test_client()
