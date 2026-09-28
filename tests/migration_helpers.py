"""Helpers for tests that build, migrate and inspect PostgreSQL schemas."""

from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.pool import NullPool

from app.extensions import db

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> AlembicConfig:
    """Alembic configured as in production, but pointed at a test database."""
    config = AlembicConfig(str(ROOT / "alembic.ini"), attributes={"configure_logger": False})
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def drop_all_tables(url: str) -> None:
    """Drop every table, including alembic_version."""
    engine = create_engine(url, poolclass=NullPool)
    with engine.begin() as connection:
        metadata = MetaData()
        metadata.reflect(connection)
        metadata.drop_all(connection)
    engine.dispose()


BASELINE = "0001_baseline"

# Every schema object PostgreSQL reports, in a form that compares equal between databases.
SCHEMA_QUERIES = {
    "columns": """
        SELECT table_name, column_name, ordinal_position, data_type, character_maximum_length,
               numeric_precision, datetime_precision, is_nullable, column_default, is_identity
        FROM information_schema.columns WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position""",
    "constraints": """
        SELECT cls.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
        FROM pg_constraint con JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        WHERE ns.nspname = 'public' ORDER BY cls.relname, con.conname""",
    "indexes": """
        SELECT tablename, indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' ORDER BY tablename, indexname""",
    "sequences": """
        SELECT sequence_name, data_type, start_value, minimum_value, maximum_value, increment
        FROM information_schema.sequences WHERE sequence_schema = 'public'
        ORDER BY sequence_name""",
    "sequence_owners": """
        SELECT seq.relname, tbl.relname, att.attname
        FROM pg_depend dep JOIN pg_class seq ON seq.oid = dep.objid AND seq.relkind = 'S'
        JOIN pg_class tbl ON tbl.oid = dep.refobjid
        JOIN pg_attribute att ON att.attrelid = tbl.oid AND att.attnum = dep.refobjsubid
        WHERE dep.deptype = 'a' ORDER BY seq.relname""",
}


def schema(url, exclude=("alembic_version",)):
    engine = create_engine(url, poolclass=NullPool)
    with engine.connect() as connection:
        snapshot = {
            name: [tuple(row) for row in connection.execute(text(query))
                   if not str(row[0]).startswith(exclude)]
            for name, query in SCHEMA_QUERIES.items()
        }
    engine.dispose()
    return snapshot


def tables(url):
    engine = create_engine(url, poolclass=NullPool)
    names = set(inspect(engine).get_table_names())
    engine.dispose()
    return names


def revision(url):
    engine = create_engine(url, poolclass=NullPool)
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    engine.dispose()
    return current


def create_all(url):
    """Build the schema the way the app did before migrations."""
    engine = create_engine(url, poolclass=NullPool)
    db.Model.metadata.create_all(engine)
    engine.dispose()


def sql(url, statement):
    engine = create_engine(url, poolclass=NullPool)
    with engine.begin() as connection:
        result = connection.execute(text(statement))
        rows = result.all() if result.returns_rows else None
    engine.dispose()
    return rows


INSERT = ("INSERT INTO observations (station, train_code, train_date, origin, destination, "
          "scheduled_time, delay_minutes) VALUES ('TARA', 'E101', '28 Sep 2026', 'Howth', "
          "'Bray', '10:00', 3)")


def head():
    return ScriptDirectory.from_config(alembic_config("")).get_current_head()
