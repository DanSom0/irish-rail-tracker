"""Alembic migrations: the baseline matches the create_all schema and is safe to deploy."""

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

from app.extensions import db


def test_upgrade_creates_the_schema_in_an_empty_database(scratch_database):
    url = scratch_database("fresh")
    command.upgrade(alembic_config(url), "head")
    assert revision(url) == head()
    assert tables(url) == {"observations", "alembic_version"}


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
