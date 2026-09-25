"""Seed fresh station observations into a disposable review database.

Refuses any database whose name does not end in ``_review`` so it cannot touch
the normal local volume or production.
"""

import random
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.engine import make_url

from app import create_app
from app.config import DEFAULT_STATION_CODES, Config
from app.extensions import db
from app.models import Observation

NO_DATA_STATION = "GSTNS"


def main() -> None:
    database = make_url(Config.SQLALCHEMY_DATABASE_URI).database or ""
    if not database.endswith("_review"):
        sys.exit(f"refusing to seed {database!r}: use a disposable database named *_review")
    app = create_app()
    now = datetime.now(UTC)
    rng = random.Random(4)
    with app.app_context():
        db.session.execute(delete(Observation))
        for index, station in enumerate(DEFAULT_STATION_CODES):
            if station == NO_DATA_STATION:
                continue
            for n in range(3):
                db.session.add(Observation(
                    station=station,
                    train_code=f"S{index:02}{n}",
                    train_date=now.date().isoformat(),
                    origin="Howth",
                    destination="Bray",
                    scheduled_time=(now + timedelta(minutes=5 + n * 10)).strftime("%H:%M"),
                    delay_minutes=rng.choice([0, 0, 1, 3, 4, 8, 12]),
                    fetched_at=now - timedelta(minutes=1),
                ))
        db.session.commit()
        print(f"seeded {db.session.query(Observation).count()} observations into {database}")


if __name__ == "__main__":
    main()
