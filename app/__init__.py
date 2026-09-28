"""Dublin Rail Tracker application."""

from flask import Flask

from app.config import Config
from app.extensions import db
from app.logging import configure_logging
from app.routes import dashboard


def create_app(config_object: type[Config] = Config) -> Flask:
    """Create and configure the Flask application.

    The schema is owned by Alembic migrations (migrations/); the app never creates tables.
    """
    configure_logging()
    app = Flask(__name__)
    app.config.from_object(config_object)

    db.init_app(app)
    app.register_blueprint(dashboard)

    return app
