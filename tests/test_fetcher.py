"""Tests for Irish Rail XML parsing and fetch failure handling."""

from pathlib import Path
from unittest.mock import Mock

import requests

from app.fetcher import fetch_station, parse_station_data

FIXTURES = Path(__file__).parent / "fixtures"


def _observations():
    return parse_station_data((FIXTURES / "station_data_namespaced.xml").read_bytes(), "CNLLY")


def test_parse_station_data_supports_default_xml_namespace():
    """The API's default XML namespace does not prevent row discovery."""
    assert len(_observations()) == 2


def test_parse_station_data_supports_responses_without_namespace():
    """Rows are still found when the response has no default namespace."""
    rows = parse_station_data((FIXTURES / "station_data_plain.xml").read_bytes(), "CNLLY")

    assert [row["train_code"] for row in rows] == ["E101"]


def test_parse_station_data_strips_traincode_whitespace():
    """Train codes are normalised before they become deduplication keys."""
    assert _observations()[0]["train_code"] == "A123"


def test_parse_station_data_normalises_irish_rail_train_date():
    """The API's human-readable train date becomes an ISO date."""
    assert _observations()[0]["train_date"] == "2026-09-16"


def test_parse_station_data_uses_arrival_for_terminating_train():
    """A 00:00 departure uses the scheduled arrival time instead."""
    assert _observations()[1]["scheduled_time"] == "10:30"


def test_fetch_station_returns_no_rows_for_empty_response(monkeypatch):
    """Empty API responses do not raise or create observations."""
    response = Mock(content=b"   \n")
    monkeypatch.setattr("app.fetcher.requests.get", Mock(return_value=response))

    assert fetch_station("CNLLY", "https://example.test", 1) == []
    response.raise_for_status.assert_called_once_with()


def test_parse_station_data_returns_no_rows_for_malformed_xml():
    """Malformed XML is logged and ignored rather than escaping the worker."""
    assert parse_station_data(b"<not-valid-xml", "CNLLY") == []


def test_fetch_station_logs_timeout_without_raising(monkeypatch, caplog):
    """A request timeout is logged and converted into an empty station result."""
    monkeypatch.setattr(
        "app.fetcher.requests.get", Mock(side_effect=requests.Timeout("request timed out"))
    )

    assert fetch_station("CNLLY", "https://example.test", 1) == []
    assert "station API request failed" in caplog.text
