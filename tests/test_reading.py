"""Offline tests for adapters/reading.py — parses a saved fixture, no network.

Fixture (reading_lynnmall_films.json) is a trimmed real response from
  GET https://prod-api.readingcinemas.com.au/films?cinemaId=Lynnmall&status=nowShowing&countryId=2
(New Lynn / LynnMall, Bearer-authed, captured live 2026-07-15). Trimmed to 6
films (4 "Now showing" + 2 "Coming soon", to exercise the release-date note
logic) with bulky prose fields (synopsis, cast, etc.) blanked out; the
showdates/showtypes/showtimes shape our parser actually reads is untouched.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from adapters import AdapterError
from adapters.reading import _note_for, _parse_films_payload
from normalise import StaleDataError

FIXTURES = Path(__file__).parent / "fixtures"

# A fixed "today" inside the window the fixture was captured in, so
# sanity_check_dates() keeps everything.
FRESH_TODAY = date(2026, 7, 15)

# A "today" far beyond the fixture's dates, so every session should look
# stale and sanity_check_dates() should raise.
STALE_TODAY = date(2027, 6, 1)


def _load_raw_films() -> list:
    raw = json.loads((FIXTURES / "reading_lynnmall_films.json").read_text(encoding="utf-8"))
    return raw["data"]


def test_parses_films_and_sessions():
    films = _parse_films_payload(_load_raw_films(), today=FRESH_TODAY)

    assert len(films) > 0
    titles = [f["title"] for f in films]
    assert any("Moana" in t for t in titles)
    assert any("Toy Story" in t for t in titles)

    for film in films:
        assert isinstance(film["title"], str) and film["title"]
        assert isinstance(film["tags"], list)
        for session in film["sessions"]:
            date.fromisoformat(session["date"])  # valid ISO date
            assert session["time"][-2:] in ("am", "pm")
            assert " " not in session["time"]
            assert session["time"] == session["time"].lower()


def test_sessions_are_sorted_chronologically():
    films = _parse_films_payload(_load_raw_films(), today=FRESH_TODAY)
    by_title = {f["title"]: f for f in films}
    moana = next(f for t, f in by_title.items() if "Moana" in t)
    assert len(moana["sessions"]) > 1
    dates_seen = [s["date"] for s in moana["sessions"]]
    assert dates_seen == sorted(dates_seen)


def test_coming_soon_films_get_an_opens_note():
    films = _parse_films_payload(_load_raw_films(), today=FRESH_TODAY)
    by_title = {f["title"]: f for f in films}
    odyssey = next(f for t, f in by_title.items() if "Odyssey" in t)
    assert odyssey["note"] is not None
    assert odyssey["note"].startswith("opens ")
    # e.g. "opens Thu 16 Jul"
    parts = odyssey["note"].split()
    assert len(parts) == 4
    assert parts[2].isdigit()


def test_now_showing_films_have_no_note():
    films = _parse_films_payload(_load_raw_films(), today=FRESH_TODAY)
    by_title = {f["title"]: f for f in films}
    minions = next(f for t, f in by_title.items() if "Minions" in t)
    assert minions["note"] is None


def test_note_for_missing_release_date_returns_none():
    assert _note_for({"status": "Coming soon"}) is None
    assert _note_for({"status": "Now showing", "release_date": "2026-07-16"}) is None


def test_note_for_bad_release_date_returns_none():
    assert _note_for({"status": "Coming soon", "release_date": "not-a-date"}) is None


def test_missing_showdates_yields_empty_sessions_not_a_crash():
    films = _parse_films_payload([{"name": "No Sessions Yet", "status": "Coming soon"}],
                                  today=FRESH_TODAY)
    assert len(films) == 1
    assert films[0]["sessions"] == []


def test_blank_title_films_are_skipped():
    films = _parse_films_payload([{"name": "", "status": "Now showing", "showdates": []}],
                                  today=FRESH_TODAY)
    assert films == []


def _count_raw_showtimes(raw_films: list) -> int:
    """Count showtimes directly from the raw fixture, bypassing
    _parse_films_payload's built-in sanity_check_dates() call entirely."""
    total = 0
    for f in raw_films:
        for showdate in f.get("showdates") or []:
            for showtype in showdate.get("showtypes") or []:
                total += len(showtype.get("showtimes") or [])
    return total


def test_sanity_check_dates_raises_when_everything_is_stale():
    films = _parse_films_payload(_load_raw_films(), today=FRESH_TODAY)
    assert any(f["sessions"] for f in films)  # sanity: fixture actually has sessions

    with pytest.raises(StaleDataError):
        _parse_films_payload(_load_raw_films(), today=STALE_TODAY)


def test_sanity_check_dates_keeps_films_within_window():
    raw_films = _load_raw_films()
    total_before = _count_raw_showtimes(raw_films)

    films = _parse_films_payload(raw_films, today=FRESH_TODAY)
    total_after = sum(len(f["sessions"]) for f in films)

    assert total_after == total_before
    assert total_after > 0


def test_adapter_error_is_exception_subclass():
    assert issubclass(AdapterError, Exception)
