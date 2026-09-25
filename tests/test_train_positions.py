"""Tests for current train positions: parsing, bounds, caching and the API route."""

import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from app import train_positions
from app.stations import STATION_COORDINATES
from app.train_positions import (
    TRAIN_BOUNDS_MARGIN_DEGREES,
    TRAIN_CACHE_SECONDS,
    TrainPositionsCache,
    monitored_bounds,
    parse_train_positions,
    trains_in_bounds,
)

FIXTURES = Path(__file__).parent / "fixtures"
FEED_XML = (FIXTURES / "train_positions_namespaced.xml").read_bytes()
EMPTY_FEED_XML = b'<ArrayOfObjTrainPositions xmlns="http://api.irishrail.ie/realtime/" />'
URL = "https://example.test/getCurrentTrainsXML"
BOUNDS = monitored_bounds(("BRAY", "CNLLY", "DLERY", "TARA"))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _trains():
    return {train["train_code"]: train for train in parse_train_positions(FEED_XML)[0]}


def _response(content=FEED_XML):
    return Mock(content=content, raise_for_status=Mock())


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def upstream(monkeypatch):
    get = Mock(return_value=_response())
    monkeypatch.setattr("app.train_positions.requests.get", get)
    return get


@pytest.fixture
def fresh_cache(monkeypatch, clock):
    cache = TrainPositionsCache(clock=clock)
    monkeypatch.setattr(train_positions, "cache", cache)
    return cache


def test_parse_supports_default_xml_namespace():
    """Namespaced rows and child fields are both found."""
    assert set(_trains()) == {"E846", "E130", "A230"}


def test_parse_strips_train_code_whitespace():
    """Trailing whitespace in TrainCode does not leak into the response."""
    assert _trains()["E846"]["train_code"] == "E846"


def test_parse_keeps_only_running_trains():
    """Trains with a status other than R are not shown as running."""
    assert "E949" not in _trains()


def test_parse_skips_and_logs_malformed_record(caplog):
    """A record with an unreadable coordinate is logged without hiding the others."""
    trains, counts = parse_train_positions(FEED_XML)

    assert "E999" not in {train["train_code"] for train in trains}
    assert "skipping malformed train position" in caplog.text
    assert counts == {"entries_parsed": 6, "trains_running": 5}


def test_parse_skips_running_train_reported_at_zero_zero():
    """The feed's 0,0 placeholder is not treated as a real position."""
    assert "D424" not in _trains()


def test_parse_normalises_literal_newlines_in_public_message():
    """Literal backslash-n separators become lines and the repeated train code is dropped."""
    assert _trains()["E846"]["public_message"] == (
        "21:39 - Bray to Malahide (0 mins late)\nArrived Shankill next stop Killiney"
    )


def test_parse_omits_destination_when_feed_has_no_field():
    """The live feed has no Destination field, so no destination key is invented."""
    assert "destination" not in _trains()["E846"]


def test_parse_reports_empty_destination_as_missing():
    """A Destination field that exists but is empty is returned as missing."""
    trains, _ = parse_train_positions((FIXTURES / "train_positions_with_destination.xml").read_bytes())

    assert [train["destination"] for train in trains] == ["Howth", None]


def test_parse_rejects_document_that_is_not_a_train_feed():
    """An unrelated XML document is an upstream failure, not an empty train list."""
    with pytest.raises(train_positions.UpstreamFeedError):
        parse_train_positions(b"<html><body>Service unavailable</body></html>")


def test_bounds_include_train_just_inside_margin():
    """A train within the margin of the outermost monitored station is kept."""
    latitude, longitude = STATION_COORDINATES["TARA"]
    train = {"latitude": latitude + TRAIN_BOUNDS_MARGIN_DEGREES - 0.001, "longitude": longitude}

    assert trains_in_bounds([train], monitored_bounds(("TARA",))) == [train]


def test_bounds_exclude_train_just_outside_margin():
    """A train beyond the margin is filtered out."""
    latitude, longitude = STATION_COORDINATES["TARA"]
    train = {"latitude": latitude, "longitude": longitude - TRAIN_BOUNDS_MARGIN_DEGREES - 0.001}

    assert trains_in_bounds([train], monitored_bounds(("TARA",))) == []


def test_bounds_are_unavailable_without_station_coordinates():
    """Stations with no known coordinates produce no bounding box."""
    assert monitored_bounds(("NOWHERE",)) is None


def test_cache_reuses_success_within_sixty_seconds(upstream, clock):
    """A second call within the cache window makes no new upstream request."""
    cache = TrainPositionsCache(clock=clock)
    cache.get(URL, 1, BOUNDS)
    clock.now += TRAIN_CACHE_SECONDS - 1
    cache.get(URL, 1, BOUNDS)

    assert upstream.call_count == 1


def test_cache_reuses_failure_within_sixty_seconds(upstream, clock):
    """An outage does not trigger an upstream request on every page refresh."""
    upstream.side_effect = requests.Timeout("timed out")
    cache = TrainPositionsCache(clock=clock)
    cache.get(URL, 1, BOUNDS)
    clock.now += TRAIN_CACHE_SECONDS - 1
    cache.get(URL, 1, BOUNDS)

    assert upstream.call_count == 1


def test_cache_refetches_after_sixty_seconds(upstream, clock):
    """Cached results expire after the cache window."""
    cache = TrainPositionsCache(clock=clock)
    cache.get(URL, 1, BOUNDS)
    clock.now += TRAIN_CACHE_SECONDS
    cache.get(URL, 1, BOUNDS)

    assert upstream.call_count == 2


def test_concurrent_calls_share_one_fetch(monkeypatch):
    """Requests arriving during an in-flight fetch wait for it instead of starting another."""
    started, release = threading.Event(), threading.Event()

    def slow_get(*args, **kwargs):
        started.set()
        release.wait(5)
        return _response()

    get = Mock(side_effect=slow_get)
    monkeypatch.setattr("app.train_positions.requests.get", get)
    cache = TrainPositionsCache()
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get(URL, 1, BOUNDS))) for _ in range(3)]
    threads[0].start()
    started.wait(5)
    for thread in threads[1:]:
        thread.start()
    threading.Event().wait(0.1)
    release.set()
    for thread in threads:
        thread.join(5)

    assert get.call_count == 1
    assert len(results) == 3 and all(result is results[0] for result in results)


def test_success_logs_parsed_running_and_in_bounds_counts(upstream, caplog):
    """A silent zero is visible: each successful fetch logs how many trains survived each step."""
    TrainPositionsCache().get(URL, 1, BOUNDS)
    record = next(record for record in caplog.records if record.getMessage() == "train positions parsed")

    assert (record.entries_parsed, record.trains_running, record.trains_in_bounds) == (6, 5, 2)


def test_programming_error_in_parser_is_not_swallowed(upstream, monkeypatch):
    """Only upstream failures are handled; bugs still raise."""
    monkeypatch.setattr(train_positions, "parse_train_positions", Mock(side_effect=TypeError("bug")))

    with pytest.raises(TypeError):
        TrainPositionsCache().get(URL, 1, BOUNDS)


def test_api_returns_ok_with_trains_in_bounds(client, upstream, fresh_cache):
    """A successful fetch returns the running trains inside the bounding box."""
    body = client.get("/api/trains").get_json()

    assert body["status"] == "ok"
    assert body["fetched_at"]
    assert {train["train_code"] for train in body["trains"]} == {"E846", "E130"}


def test_api_returns_ok_with_no_trains(client, upstream, fresh_cache):
    """A valid feed with no running trains is a success, not an error."""
    upstream.return_value = _response(EMPTY_FEED_XML)
    body = client.get("/api/trains").get_json()

    assert (body["status"], body["trains"]) == ("ok", [])


def test_api_returns_stale_trains_when_refresh_fails(client, upstream, fresh_cache, clock):
    """After a failure, the last good trains are returned with their original fetch time."""
    first = client.get("/api/trains").get_json()
    upstream.side_effect = requests.ConnectionError("down")
    clock.now += TRAIN_CACHE_SECONDS
    body = client.get("/api/trains").get_json()

    assert body["status"] == "stale"
    assert (body["trains"], body["fetched_at"]) == (first["trains"], first["fetched_at"])


def test_api_returns_error_when_refresh_fails_without_cache(client, upstream, fresh_cache):
    """Without an earlier success, a failure returns an empty list and an error status."""
    upstream.return_value = _response(b"<not-valid-xml")
    body = client.get("/api/trains").get_json()

    assert (body["status"], body["trains"], body["fetched_at"]) == ("error", [], None)


def test_api_treats_empty_response_as_failure(client, upstream, fresh_cache):
    """An empty body is an upstream failure rather than a valid empty feed."""
    upstream.return_value = _response(b"  \n")

    assert client.get("/api/trains").get_json()["status"] == "error"


def test_api_reports_error_when_no_station_has_coordinates(app, client, upstream, fresh_cache, monkeypatch, caplog):
    """Missing coordinates are a configuration error, not an empty success."""
    monkeypatch.setitem(app.config, "STATION_CODES", ("NOWHERE",))
    body = client.get("/api/trains").get_json()

    assert (body["status"], body["reason"]) == ("error", "No monitored station has map coordinates.")
    assert "no monitored station has coordinates" in caplog.text
    upstream.assert_not_called()
