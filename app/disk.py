"""Root filesystem usage, shared by /status and the worker."""

import logging
import math
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)
WARNING_PERCENT = 80


@dataclass(frozen=True)
class DiskUsage:
    path: str
    used_percent: int
    free_bytes: int
    total_bytes: int
    warning_percent: int = WARNING_PERCENT

    @property
    def high(self):
        return self.used_percent > self.warning_percent


def check_disk_usage(path="/"):
    """Return usage like `df`, logging a warning above the threshold; None if unreadable.

    A container's overlay root reports the host filesystem that holds Docker's data.
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        logger.exception("disk_usage_unavailable", extra={"path": path})
        return None
    # df's Use%: space reserved for root counts as neither used nor available.
    percent = math.ceil(100 * usage.used / (usage.used + usage.free)) if usage.used else 0
    result = DiskUsage(path, percent, usage.free, usage.total)
    if result.high:
        logger.warning("disk_usage_high", extra={
            "path": path, "used_percent": percent,
            "free_bytes": usage.free, "threshold_percent": WARNING_PERCENT,
        })
    return result
