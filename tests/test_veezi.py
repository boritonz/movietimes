"""Offline tests for adapters/veezi.py — parses saved fixtures, no network.

Fixtures were captured live from real Veezi session pages:
  - veezi_hollywood.html: Hollywood Avondale, single date/time per film.
  - veezi_bridgeway.html: The Bridgeway, trimmed but keeps films with
    multiple dates and multiple times per date (synopses trimmed for size).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from adapters import AdapterError
from adapters.veezi import _parse_date_heading, _parse_sessions_html
from normalise import StaleDataError, sanity_check_dates

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# A fixed "today" inside the window the fixtures were captured in, so
# sanity_check_dates() keeps everything.
FRESH_TODAY = date(2026, 7, 15)

# A "today" far beyond the fixtures' dates, so every session should look
# stale and sanity_check_dates() should raise.
STALE_TODAY = date(2027, 6, 1)


def test_hollywood_parses_films_and_sessions():
    html = _load("veezi_hollywood.html")
    films = _parse_sessions_html(html, "https://example.invalid/sessions", today=FRESH_TODAY)

    assert len(films) > 0
    titles = [f["title"] for f in films]
    assert any("Bullet in the Head" in t for t in titles)

    for film in films:
        assert isinstance(film["title"], str) and film["title"]
        assert film["note"] is None
        assert isinstance(film["tags"], list)
        for session in film["sessions"]:
            # Valid ISO date.
            date.fromisoformat(session["date"])
            # Lowercase am/pm time string, no space before am/pm.
            assert session["time"][-2:] in ("am", "pm")
            assert " " not in session["time"]


def test_hollywood_has_at_least_one_multi_word_session_time_format():
    html = _load("veezi_hollywood.html")
    films = _parse_sessions_html(html, "https://example.invalid/sessions", today=FRESH_TODAY)
    all_times = [s["time"] for f in films for s in f["sessions"]]
    assert all_times, "expected at least one parsed session time"
    # e.g. "7:30pm" not "7:30 PM"
    assert all(t == t.lower() for t in all_times)
    assert all(":" in t for t in all_times)


def test_bridgeway_parses_multi_date_and_multi_time_sessions():
    html = _load("veezi_bridgeway.html")
    films = _parse_sessions_html(html, "https://example.invalid/sessions", today=FRESH_TODAY)

    assert len(films) > 0

    by_title = {f["title"]: f for f in films}
    # Glenrothan (kept in the trimmed fixture) has many dates and multiple
    # times per date - exercise the date-container loop properly.
    glenrothan = next((f for t, f in by_title.items() if "Glenrothan" in t), None)
    assert glenrothan is not None
    dates_seen = {s["date"] for s in glenrothan["sessions"]}
    assert len(dates_seen) > 1
    assert len(glenrothan["sessions"]) > len(dates_seen)  # multiple times on some dates

    for film in films:
        for session in film["sessions"]:
            date.fromisoformat(session["date"])


def test_parse_date_heading_same_year():
    assert _parse_date_heading("Thursday 16, July", date(2026, 7, 15)) == "2026-07-16"
    assert _parse_date_heading("Sunday 30, August", date(2026, 7, 15)) == "2026-08-30"


def test_parse_date_heading_rolls_into_next_year():
    # Fetched in December, showing a January date -> next calendar year.
    assert _parse_date_heading("Friday 8, January", date(2026, 12, 20)) == "2027-01-08"


def test_parse_date_heading_unparseable_returns_none():
    assert _parse_date_heading("garbage", date(2026, 7, 15)) is None


def test_missing_sessions_by_film_container_raises_adapter_error():
    with pytest.raises(AdapterError):
        _parse_sessions_html("<html><body>nothing here</body></html>", "https://example.invalid")


def test_sanity_check_dates_raises_when_everything_is_stale():
    html = _load("veezi_hollywood.html")
    films = _parse_sessions_html(html, "https://example.invalid/sessions", today=FRESH_TODAY)
    assert any(f["sessions"] for f in films)  # sanity: fixture actually has sessions

    with pytest.raises(StaleDataError):
        sanity_check_dates(films, today=STALE_TODAY)


def test_sanity_check_dates_keeps_films_within_window():
    html = _load("veezi_hollywood.html")
    films = _parse_sessions_html(html, "https://example.invalid/sessions", today=FRESH_TODAY)
    total_sessions_before = sum(len(f["sessions"]) for f in films)

    films = sanity_check_dates(films, today=FRESH_TODAY)
    total_sessions_after = sum(len(f["sessions"]) for f in films)

    assert total_sessions_after == total_sessions_before
    assert total_sessions_after > 0
