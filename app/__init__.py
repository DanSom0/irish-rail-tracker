"""Irish Rail delay tracker application."""

from flask import Flask
from sqlalchemy import text

from app.config import Config
from app.extensions import db
from app.logging import configure_logging
from app.routes import dashboard


def create_app(config_object: type[Config] = Config) -> Flask:
    """Create and configure the Flask application."""
    configure_logging()
    app = Flask(__name__)
    app.config.from_object(config_object)

    db.init_app(app)
    app.register_blueprint(dashboard)

    with app.app_context():
        from app import models  # noqa: F401 - register model metadata

        if db.engine.dialect.name == "postgresql":
            # Web and worker start concurrently in Compose. Serialise the
            # first schema bootstrap so their CREATE TABLE statements cannot race.
            with db.engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_xact_lock(8421701)"))
                db.Model.metadata.create_all(bind=connection)
        else:
            db.create_all()

    return app
