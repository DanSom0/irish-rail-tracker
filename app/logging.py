"""Structured logging configuration."""

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line for container log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        standard_fields = set(logging.makeLogRecord({}).__dict__)
        for key, value in record.__dict__.items():
            if key not in standard_fields and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload)


def configure_logging() -> None:
    root = logging.getLogger()
    if any(getattr(handler, "_irish_rail", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler._irish_rail = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
