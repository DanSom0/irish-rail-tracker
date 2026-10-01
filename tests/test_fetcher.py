"""Tests for Irish Rail XML parsing and fetch failure handling."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from app.fetcher import StationPollError, fetch_station, parse_station_data

FIXTURES = Path(__file__).parent / "fixtures"
EMPTY_BOARD = (b'<?xml version="1.0" encoding="utf-8"?>'
               b'<ArrayOfObjStationData xmlns="http://api.irishrail.ie/realtime/" />')


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


def test_parse_station_data_returns_no_rows_for_a_valid_empty_board():
    """A well-formed board with no services is a successful, empty result."""
    assert parse_station_data(EMPTY_BOARD, "CNLLY") == []


def test_parse_station_data_rejects_malformed_xml():
    """Malformed XML is a failed poll, never an empty board."""
    with pytest.raises(StationPollError, match="^invalid XML$"):
        parse_station_data(b"<not-valid-xml", "CNLLY")


def test_parse_station_data_rejects_a_document_that_is_not_a_station_board():
    """Well-formed XML of another kind (an error page, say) does not clear the board."""
    with pytest.raises(StationPollError, match="^unexpected XML document$"):
        parse_station_data(b"<html><body>Service unavailable</body></html>", "CNLLY")


def test_fetch_station_rejects_an_empty_response_body(monkeypatch):
    """An empty body is a failure; only a valid empty board clears the station."""
    response = Mock(content=b"   \n")
    monkeypatch.setattr("app.fetcher.requests.get", Mock(return_value=response))

    with pytest.raises(StationPollError, match="^empty response body$"):
        fetch_station("CNLLY", "https://example.test", 1)
    response.raise_for_status.assert_called_once_with()


@pytest.mark.parametrize("error,reason", [
    (requests.Timeout("Read timed out: https://example.test/?StationCode=CNLLY"), "request timed out"),
    (requests.ConnectionError("https://example.test/?StationCode=CNLLY refused"), "connection error"),
    (requests.HTTPError("503 for url: https://example.test/?StationCode=CNLLY",
                        response=Mock(status_code=503)), "HTTP 503"),
    (requests.TooManyRedirects("https://example.test/?StationCode=CNLLY"), "request failed"),
])
def test_fetch_station_request_failures_have_short_sanitised_reasons(monkeypatch, error, reason):
    """Reasons are fixed labels: the request URL and its parameters never reach them."""
    monkeypatch.setattr("app.fetcher.requests.get", Mock(side_effect=error))

    with pytest.raises(StationPollError) as raised:
        fetch_station("CNLLY", "https://example.test", 1)
    assert raised.value.reason == reason
    assert raised.value.__cause__ is None
