"""Station configuration follows the repo default unless explicitly overridden."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.stations import station_codes


@pytest.mark.parametrize("override", [None, "", "   ", ",,"])
def test_unset_or_empty_station_codes_uses_repo_default(override):
    listed = next(line.split("=", 1)[1] for line in Path(".env.example").read_text().splitlines()
                  if line.startswith("STATION_CODES="))
    assert len(listed.split(",")) == 20
    environment = os.environ.copy()
    environment.pop("STATION_CODES", None)
    if override is not None:
        environment["STATION_CODES"] = override
    actual = subprocess.check_output(
        [sys.executable, "-c", "from app.config import Config; print(','.join(Config.STATION_CODES))"],
        env=environment, text=True,
    ).strip().split(",")
    assert actual == list(station_codes(listed.split(",")))
