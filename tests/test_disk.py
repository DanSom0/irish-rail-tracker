"""Disk usage check shown on /status and logged by the worker."""

from types import SimpleNamespace

import pytest

from app import disk

GIB = 1024 ** 3


def fake_usage(monkeypatch, total, used, free):
    monkeypatch.setattr(disk.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=total, used=used, free=free))


@pytest.mark.parametrize("total,used,free,percent,high", [
    (19 * GIB, 6 * GIB, 13 * GIB, 32, False),
    (100, 80, 20, 80, False),  # Exactly the threshold is not above it.
    (1000, 801, 199, 81, True),  # Rounded up, as df does.
    (100, 50, 45, 53, False),  # Root-reserved space is excluded, as df does.
    (100, 0, 100, 0, False),
])
def test_disk_usage_matches_df_and_warns_only_above_threshold(
    monkeypatch, caplog, total, used, free, percent, high,
):
    fake_usage(monkeypatch, total, used, free)
    usage = disk.check_disk_usage()
    assert (usage.used_percent, usage.high, usage.free_bytes) == (percent, high, free)
    warnings = [record for record in caplog.records if record.message == "disk_usage_high"]
    assert len(warnings) == int(high)
    if high:
        assert (warnings[0].levelname, warnings[0].used_percent) == ("WARNING", percent)


def test_unreadable_disk_is_logged_not_raised(monkeypatch, caplog):
    def fail(path):
        raise OSError("gone")
    monkeypatch.setattr(disk.shutil, "disk_usage", fail)
    assert disk.check_disk_usage() is None
    assert "disk_usage_unavailable" in caplog.text
