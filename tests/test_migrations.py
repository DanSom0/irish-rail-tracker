"""Alembic migrations: the baseline matches the create_all schema, and each migration is safe to deploy."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from migration_helpers import (
    BASELINE,
    INSERT,
    alembic_config,
    create_all,
    head,
    revision,
    schema,
    sql,
    tables,
)
from sqlalchemy.exc import IntegrityError

from app.extensions import db


def test_upgrade_creates_the_schema_in_an_empty_database(scratch_database):
    url = scratch_database("fresh")
    command.upgrade(alembic_config(url), "head")
    assert revision(url) == head()
    assert tables(url) == {"observations", "station_polls", "worker_heartbeat", "alembic_version"}


def test_baseline_matches_the_create_all_schema(scratch_database):
    migrated, legacy = scratch_database("migrated"), scratch_database("legacy")
    command.upgrade(alembic_config(migrated), BASELINE)
    create_all(legacy)
    migrated_schema = schema(migrated)
    assert migrated_schema == schema(legacy)
    assert ("observations", "uq_observation_train", "u",
            "UNIQUE (station, train_code, train_date)") in migrated_schema["constraints"]


def test_models_match_the_migrations(app):
    """An autogenerate diff at head is empty: every model change has a migration."""
    with app.app_context(), db.engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), db.Model.metadata) == []


def test_upgrade_to_head_again_changes_nothing(scratch_database):
    url = scratch_database("idempotent")
    command.upgrade(alembic_config(url), "head")
    sql(url, INSERT)
    before = schema(url, exclude=())
    command.upgrade(alembic_config(url), "head")
    assert schema(url, exclude=()) == before
    assert revision(url) == head()
    assert sql(url, "SELECT train_code, delay_minutes FROM observations") == [("E101", 3)]


def test_baseline_downgrade_is_refused_and_keeps_the_data(scratch_database):
    url = scratch_database("downgrade")
    command.upgrade(alembic_config(url), BASELINE)
    sql(url, INSERT)
    with pytest.raises(RuntimeError, match="Downgrading the baseline is not supported"):
        command.downgrade(alembic_config(url), "base")
    assert revision(url) == BASELINE
    assert sql(url, "SELECT count(*) FROM observations") == [(1,)]


def test_upgrade_refuses_an_unstamped_database_with_tables(scratch_database):
    url = scratch_database("unstamped")
    create_all(url)
    before = schema(url)
    with pytest.raises(RuntimeError, match="no Alembic revision.*stamp-baseline.sh"):
        command.upgrade(alembic_config(url), "head")
    assert tables(url) == {"observations"}  # alembic_version was rolled back too.
    assert schema(url) == before


POLLS = "0002_station_polls"

# What the release before 0002 runs (e799634: app/fetcher.py and app/routes.py): the worker's
# upsert, and the current-board query, the latest fetch per station within ten minutes.
PREVIOUS_RELEASE_UPSERT = """
    INSERT INTO observations (station, train_code, train_date, origin, destination,
                              scheduled_time, delay_minutes, fetched_at)
    VALUES ('TARA', 'E101', '2026-09-28', 'Howth', 'Bray', '10:00', 9, now()),
           ('TARA', 'E202', '2026-09-28', 'Bray', 'Howth', '10:05', 0, now())
    ON CONFLICT ON CONSTRAINT uq_observation_train DO UPDATE SET
        origin = excluded.origin, destination = excluded.destination,
        scheduled_time = excluded.scheduled_time, delay_minutes = excluded.delay_minutes,
        fetched_at = excluded.fetched_at"""
PREVIOUS_RELEASE_BOARD = """
    WITH latest AS (SELECT station, max(fetched_at) AS last_fetch FROM observations GROUP BY station)
    SELECT o.train_code, o.delay_minutes FROM observations o
    JOIN latest ON o.station = latest.station
    WHERE o.fetched_at = latest.last_fetch AND o.fetched_at >= now() - interval '10 minutes'
    ORDER BY o.train_code"""


def test_station_polls_migration_is_additive_on_the_baseline(scratch_database):
    url = scratch_database("polls")
    command.upgrade(alembic_config(url), BASELINE)
    sql(url, INSERT)
    before = schema(url)
    command.upgrade(alembic_config(url), POLLS)
    after = schema(url)
    assert revision(url) == POLLS
    for kind, rows in before.items():  # observations is exactly as the previous release left it.
        assert [row for row in after[kind] if row[0].startswith("observations")] == rows
    assert tables(url) == {"observations", "station_polls", "worker_heartbeat", "alembic_version"}
    assert sql(url, "SELECT train_code, delay_minutes FROM observations") == [("E101", 3)]
    assert sql(url, "SELECT count(*) FROM station_polls") == [(0,)]


def test_previous_release_queries_work_after_the_station_polls_migration(scratch_database):
    url = scratch_database("previous_release")
    command.upgrade(alembic_config(url), POLLS)
    sql(url, INSERT)
    sql(url, PREVIOUS_RELEASE_UPSERT)
    assert sql(url, "SELECT count(*) FROM observations") == [(3,)]
    assert sql(url, PREVIOUS_RELEASE_BOARD) == [("E101", 9), ("E202", 0)]
    assert sql(url, "SELECT 1") == [(1,)]  # The previous release's /health query.


@pytest.mark.parametrize("values", [
    "('CNLLY', now(), now(), 'ok', 0, NULL)",
    "('CNLLY', now(), now(), 'empty', 2, NULL)",
    "('CNLLY', now(), NULL, 'ok', 2, NULL)",
    "('CNLLY', now(), now(), 'error', 2, 'request timed out')",
    "('CNLLY', now(), NULL, 'error', NULL, NULL)",
    "('CNLLY', now(), NULL, 'unknown', NULL, 'x')",
])
def test_station_polls_reject_inconsistent_outcomes(scratch_database, values):
    url = scratch_database("poll_checks")
    command.upgrade(alembic_config(url), POLLS)
    with pytest.raises(IntegrityError, match="ck_station_polls_outcome"):
        sql(url, f"INSERT INTO station_polls VALUES {values}")


def test_worker_heartbeat_holds_one_row(scratch_database):
    url = scratch_database("heartbeat")
    command.upgrade(alembic_config(url), POLLS)
    sql(url, "INSERT INTO worker_heartbeat VALUES (1, now())")
    with pytest.raises(IntegrityError, match="ck_worker_heartbeat_single_row"):
        sql(url, "INSERT INTO worker_heartbeat VALUES (2, now())")


def test_station_polls_downgrade_keeps_observations(scratch_database):
    """Local development only: production never downgrades."""
    url = scratch_database("polls_downgrade")
    command.upgrade(alembic_config(url), POLLS)
    sql(url, INSERT)
    command.downgrade(alembic_config(url), BASELINE)
    assert revision(url) == BASELINE
    assert tables(url) == {"observations", "alembic_version"}
    assert sql(url, "SELECT count(*) FROM observations") == [(1,)]
