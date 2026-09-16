"""Scheduled worker process entry point."""

import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler

from app import create_app
from app.fetcher import run_fetch_cycle


def main() -> None:
    app = create_app()
    logger = logging.getLogger(__name__)

    def fetch() -> None:
        with app.app_context():
            run_fetch_cycle(app)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        fetch,
        trigger="interval",
        minutes=app.config["FETCH_INTERVAL_MINUTES"],
        id="irish_rail_fetch",
        max_instances=1,
        coalesce=True,
    )
    logger.info("worker started", extra={"interval_minutes": app.config["FETCH_INTERVAL_MINUTES"]})
    fetch()

    def shutdown(*_args) -> None:
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    scheduler.start()


if __name__ == "__main__":
    main()
