"""Offline tests for adapters/evt.py — parses a saved fixture, no network.

Fixture (evt_queenstreet_getsessions.json) is a trimmed real response from
  GET https://www.eventcinemas.co.nz/Cinemas/GetSessions?cinemaIds=502
(Event Queen St, cinemaId=502, captured live 2026-07-15). Trimmed to 6 movies
and the first 10 of ~37 available dates to keep the fixture small; the shape
(Data.Movies[].CinemaModels[].Sessions[].{StartTime,Attributes}) is untouched.
Rialto and the other Event cinemas use the identical response shape, just a
different cinemaId, so one fixture covers the whole platform's parsing logic.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from adapters import AdapterError
from adapters.evt import _finalize_films, _merge_payload
from normalise import StaleDataError, sanity_check_dates

FIXTURES = Path(__file__).parent / "fixtures"

# The cinemaId embedded in the fixture (Event Queen St).
CINEMA_ID = 502

# A fixed "today" inside the window the fixture was captured in, so
# sanity_check_dates() keeps everything.
FRESH_TODAY = date(2026, 7, 15)

# A "today" far beyond the fixture's dates, so every session should look
# stale and sanity_check_dates() should raise.
STALE_TODAY = date(2027, 6, 1)


def _load_payload() -> dict:
    raw = json.loads((FIXTURES / "evt_queenstreet_getsessions.json").read_text(encoding="utf-8"))
    return raw["Data"]


def _parse(today: date | None = FRESH_TODAY) -> list[dict]:
    films_by_id: dict[int, dict] = {}
    _merge_payload(films_by_id, _load_payload(), CINEMA_ID)
    return _finalize_films(films_by_id, today=today)


def test_parses_films_and_sessions():
    films = _parse()

    assert len(films) > 0
    titles = [f["title"] for f in films]
    assert any("Moana" in t for t in titles)

    for film in films:
        assert isinstance(film["title"], str) and film["title"]
        assert isinstance(film["tags"], list)
        for session in film["sessions"]:
            date.fromisoformat(session["date"])  # valid ISO date
            assert session["time"][-2:] in ("am", "pm")
            assert " " not in session["time"]
            assert session["time"] == session["time"].lower()


def test_sessions_are_sorted_chronologically():
    films = _parse()
    for film in films:
        times = [(s["date"], s["time"]) for s in film["sessions"]]
        # Sessions were merged from Attributes-bearing raw StartTime strings and
        # sorted by that ISO string before formatting -- at minimum, dates
        # should never go backwards film-to-film.
        dates_seen = [d for d, _ in times]
        assert dates_seen == sorted(dates_seen)


def test_only_sessions_for_the_requested_cinema_id_are_kept():
    # _merge_payload filters CinemaModels by evt_id -- asking for a cinema id
    # that isn't in the fixture should yield movies with zero sessions each,
    # which sanity_check_dates then can't complain about (no sessions to
    # begin with is fine; only "had sessions but all stale" raises).
    films_by_id: dict[int, dict] = {}
    _merge_payload(films_by_id, _load_payload(), 999999)
    films = _finalize_films(films_by_id, today=FRESH_TODAY)
    assert all(f["sessions"] == [] for f in films)


def test_merge_payload_accumulates_across_multiple_calls():
    # fetch() calls _merge_payload once per date and relies on it accumulating
    # into the same films_by_id dict rather than overwriting.
    films_by_id: dict[int, dict] = {}
    payload = _load_payload()
    _merge_payload(films_by_id, payload, CINEMA_ID)
    count_after_one = sum(len(e["raw_sessions"]) for e in films_by_id.values())
    _merge_payload(films_by_id, payload, CINEMA_ID)
    count_after_two = sum(len(e["raw_sessions"]) for e in films_by_id.values())
    assert count_after_two == 2 * count_after_one


def test_sanity_check_dates_raises_when_everything_is_stale():
    films = _parse(today=FRESH_TODAY)
    assert any(f["sessions"] for f in films)  # sanity: fixture actually has sessions

    with pytest.raises(StaleDataError):
        _parse(today=STALE_TODAY)


def test_sanity_check_dates_keeps_films_within_window():
    films_by_id: dict[int, dict] = {}
    _merge_payload(films_by_id, _load_payload(), CINEMA_ID)
    films = [
        {
            "title": e["title"],
            "sessions": [s for _, s in sorted(e["raw_sessions"], key=lambda p: p[0])],
            "note": None,
            "tags": [],
        }
        for e in films_by_id.values() if e["title"]
    ]
    total_before = sum(len(f["sessions"]) for f in films)

    films = sanity_check_dates(films, today=FRESH_TODAY)
    total_after = sum(len(f["sessions"]) for f in films)

    assert total_after == total_before
    assert total_after > 0


def test_no_films_at_all_would_be_an_adapter_error_upstream():
    # _finalize_films itself doesn't raise on empty input (fetch() does, after
    # checking films_by_id) -- confirm the empty-dict path is at least inert
    # and doesn't blow up, since fetch()'s own emptiness check is what's load
    # bearing for AdapterError.
    assert _finalize_films({}, today=FRESH_TODAY) == []


def test_adapter_error_is_exception_subclass():
    assert issubclass(AdapterError, Exception)
