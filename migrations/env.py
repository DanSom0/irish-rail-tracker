"""Run migrations against DATABASE_URL, the database the app itself uses."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app import models  # noqa: F401 - register model metadata
from app.config import Config
from app.extensions import db

config = context.config
# Tests pass configure_logger=False so Alembic leaves their logging alone.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = db.Model.metadata


def database_url() -> str:
    """Tests set sqlalchemy.url; everywhere else uses the app's DATABASE_URL setting."""
    return config.get_main_option("sqlalchemy.url") or Config.SQLALCHEMY_DATABASE_URI


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(), target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
