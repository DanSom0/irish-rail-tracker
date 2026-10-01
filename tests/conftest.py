"""Shared test application and PostgreSQL database fixtures."""

import os

import pytest
from alembic import command
from migration_helpers import alembic_config, drop_all_tables
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import Observation, StationPoll, WorkerHeartbeat


class TestConfig(Config):
    """Configuration for tests backed by the disposable PostgreSQL database."""

    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://irishrail:irishrail@localhost:5432/irishrail_test",
    )


@pytest.fixture(scope="session")
def app():
    """Migrate the test database to head, as deploy does, once for the test session."""
    url = TestConfig.SQLALCHEMY_DATABASE_URI
    drop_all_tables(url)  # A schema left by an older checkout would block the baseline.
    command.upgrade(alembic_config(url), "head")
    application = create_app(TestConfig)
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()
    drop_all_tables(url)


@pytest.fixture(autouse=True)
def clear_observations(app):
    """Keep each test independent while retaining the test schema."""
    with app.app_context():
        for model in (Observation, StationPoll, WorkerHeartbeat):
            db.session.execute(delete(model))
        db.session.commit()
    yield
    with app.app_context():
        db.session.rollback()
        db.session.remove()


@pytest.fixture
def client(app):
    """Return a Flask test client."""
    return app.test_client()


@pytest.fixture
def scratch_database():
    """Create empty databases beside the test database; return their URLs; drop them after."""
    base = make_url(TestConfig.SQLALCHEMY_DATABASE_URI)
    admin = create_engine(base, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    created = []

    def create(suffix: str) -> str:
        name = f"{base.database}_{suffix}"
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return base.set(database=name).render_as_string(hide_password=False)

    yield create
    with admin.connect() as connection:
        for name in created:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
