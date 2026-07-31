"""Core pipeline tests. No network access - adapters are monkeypatched or
never invoked."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import normalise  # noqa: E402
import render  # noqa: E402
import scrape  # noqa: E402
from adapters import CINEMAS, AdapterError  # noqa: E402


# ---------------------------------------------------------------------------
# normalise.dedupe_key
# ---------------------------------------------------------------------------

def test_dedupe_key_merges_case_and_punctuation_variants():
    assert normalise.dedupe_key("The Odyssey") == normalise.dedupe_key("THE ODYSSEY!")


def test_dedupe_key_strips_release_year():
    assert normalise.dedupe_key("The Odyssey (2026)") == normalise.dedupe_key("The Odyssey")
    assert normalise.dedupe_key("The Odyssey (2026)") == "the odyssey"


# ---------------------------------------------------------------------------
# normalise.sanity_check_dates
# ---------------------------------------------------------------------------

def test_sanity_check_dates_drops_out_of_window_sessions():
    today = normalise.today_nz()
    stale = today.replace(year=today.year - 1).isoformat()
    films = [{
        "title": "Foo",
        "sessions": [
            {"date": today.isoformat(), "time": "6:00pm"},
            {"date": stale, "time": "8:00pm"},
        ],
    }]
    out = normalise.sanity_check_dates(films)
    assert len(out[0]["sessions"]) == 1
    assert out[0]["sessions"][0]["date"] == today.isoformat()


def test_sanity_check_dates_raises_staledata_when_all_stale():
    today = normalise.today_nz()
    stale = today.replace(year=today.year - 1).isoformat()
    films = [{"title": "Foo", "sessions": [{"date": stale, "time": "6:00pm"}]}]
    with pytest.raises(normalise.StaleDataError):
        normalise.sanity_check_dates(films)


def test_sanity_check_dates_ok_when_no_sessions_at_all():
    # No sessions scraped at all (e.g. a listings-only page) is not "stale" -
    # it's just empty, and must not raise.
    films = [{"title": "Foo", "sessions": []}]
    out = normalise.sanity_check_dates(films)
    assert out[0]["sessions"] == []


# ---------------------------------------------------------------------------
# scrape.build_snapshot
# ---------------------------------------------------------------------------

def test_scrape_keeps_previous_entry_when_adapter_raises(tmp_path, monkeypatch):
    latest_path = tmp_path / "latest.json"
    seed_path = tmp_path / "seed.json"

    previous_entry = {
        "cinemaId": "academy",
        "films": [{"title": "Old Film", "sessions": [], "note": None, "tags": []}],
        "info": "old info",
        "fetchedAt": "2026-07-10T09:00:00+12:00",
        "sourceUrl": "https://www.academycinemas.co.nz",
    }
    latest_path.write_text(
        json.dumps({
            "fetchedAt": "2026-07-10T09:00:00+12:00",
            "errors": {},
            "cinemas": {"academy": previous_entry},
        }),
        encoding="utf-8",
    )
    seed_path.write_text(
        json.dumps({"fetchedAt": "x", "errors": {}, "cinemas": {}}),
        encoding="utf-8",
    )

    def _raise_adapter_error(cinema_id):
        raise AdapterError(f"no adapter module for {cinema_id}")

    monkeypatch.setattr(scrape, "get_adapter", _raise_adapter_error)
    monkeypatch.setattr(scrape, "get_fallback", lambda cinema_id: None)

    academy = next(c for c in CINEMAS if c["id"] == "academy")
    snapshot = scrape.build_snapshot(
        cinemas=[academy], latest_path=latest_path, seed_path=seed_path
    )

    assert snapshot["cinemas"]["academy"] == previous_entry
    assert "academy" in snapshot["errors"]


def test_scrape_falls_back_to_seed_when_no_previous_entry(tmp_path, monkeypatch):
    latest_path = tmp_path / "latest.json"  # does not exist
    seed_path = tmp_path / "seed.json"

    seed_entry = {
        "cinemaId": "academy",
        "films": [{"title": "Seed Film", "sessions": [], "note": None, "tags": []}],
        "info": "seed info",
        "fetchedAt": "2026-07-15T12:30:00+12:00",
        "sourceUrl": "https://www.academycinemas.co.nz",
    }
    seed_path.write_text(
        json.dumps({
            "fetchedAt": "2026-07-15T12:30:00+12:00",
            "errors": {},
            "cinemas": {"academy": seed_entry},
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(scrape, "get_adapter", lambda cid: (_ for _ in ()).throw(ImportError("no module")))
    monkeypatch.setattr(scrape, "get_fallback", lambda cinema_id: None)

    academy = next(c for c in CINEMAS if c["id"] == "academy")
    snapshot = scrape.build_snapshot(
        cinemas=[academy], latest_path=latest_path, seed_path=seed_path
    )

    assert snapshot["cinemas"]["academy"] == seed_entry
    assert "academy" in snapshot["errors"]


def test_scrape_uses_fallback_when_primary_adapter_fails(tmp_path, monkeypatch):
    latest_path = tmp_path / "latest.json"
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps({"fetchedAt": "x", "errors": {}, "cinemas": {}}), encoding="utf-8")

    fallback_result = {
        "cinemaId": "academy",
        "films": [{"title": "Fallback Film", "sessions": [], "note": None, "tags": []}],
        "info": None,
        "fetchedAt": normalise.now_nz_iso(),
        "sourceUrl": "https://www.academycinemas.co.nz",
    }

    def _raise_adapter_error(cinema_id):
        raise AdapterError("primary adapter down")

    def _fallback(cinema_id):
        return lambda cinema: dict(fallback_result)

    monkeypatch.setattr(scrape, "get_adapter", _raise_adapter_error)
    monkeypatch.setattr(scrape, "get_fallback", _fallback)

    academy = next(c for c in CINEMAS if c["id"] == "academy")
    snapshot = scrape.build_snapshot(
        cinemas=[academy], latest_path=latest_path, seed_path=seed_path
    )

    assert snapshot["cinemas"]["academy"]["films"][0]["title"] == "Fallback Film"
    assert "academy" not in snapshot["errors"]


# ---------------------------------------------------------------------------
# render.main
# ---------------------------------------------------------------------------

def test_render_produces_index_html_from_seed(tmp_path):
    docs_dir = tmp_path / "docs"
    seed_path = ROOT / "data" / "seed.json"
    missing_latest = tmp_path / "does-not-exist.json"

    render.main(latest_path=missing_latest, seed_path=seed_path, docs_dir=docs_dir)

    html = (docs_dir / "index.html").read_text(encoding="utf-8")
    assert "The Odyssey" in html
    assert "Academy Cinemas" in html


def test_render_produces_index_html_from_seed_markup(tmp_path):
    # seed.json has zero sessions on every film - everything should be
    # demoted to the "Also showing" list, and the day-mode/search UI should
    # still be present regardless.
    docs_dir = tmp_path / "docs"
    seed_path = ROOT / "data" / "seed.json"
    missing_latest = tmp_path / "does-not-exist.json"

    render.main(latest_path=missing_latest, seed_path=seed_path, docs_dir=docs_dir)

    html = (docs_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-mode="tonight"' in html
    assert 'data-mode="today"' in html
    assert 'data-mode="upcoming"' in html
    assert "Also showing" in html
    assert 'id="film-search"' in html
    assert 'id="kids-pill"' in html
    assert "data-rendered-date=" in html


# ---------------------------------------------------------------------------
# render.parse_time_to_mins
# ---------------------------------------------------------------------------

def test_parse_time_to_mins_basic():
    assert render.parse_time_to_mins("6:15pm") == 18 * 60 + 15


def test_parse_time_to_mins_noon_is_720():
    assert render.parse_time_to_mins("12:00pm") == 720


def test_parse_time_to_mins_just_after_midnight_is_15():
    assert render.parse_time_to_mins("12:15am") == 15


def test_parse_time_to_mins_midnight_is_zero():
    assert render.parse_time_to_mins("12:00am") == 0


def test_parse_time_to_mins_morning_single_digit_hour():
    assert render.parse_time_to_mins("9:05am") == 9 * 60 + 5


def test_parse_time_to_mins_is_case_insensitive():
    assert render.parse_time_to_mins("6:15PM") == render.parse_time_to_mins("6:15pm")


# ---------------------------------------------------------------------------
# render.truncate_note
# ---------------------------------------------------------------------------

def test_truncate_note_keeps_short_notes_untouched():
    assert render.truncate_note("opens Thu 16 Jul") == "opens Thu 16 Jul"


def test_truncate_note_keeps_exactly_60_chars_untouched():
    note = "x" * 60
    assert render.truncate_note(note) == note


def test_truncate_note_truncates_long_notes_with_ellipsis():
    note = "x" * 61
    out = render.truncate_note(note)
    assert out == ("x" * 60) + "…"
    assert len(out) == 61  # 60 chars + the ellipsis marker


def test_truncate_note_passes_through_none():
    assert render.truncate_note(None) is None


# ---------------------------------------------------------------------------
# render.session_views - "Coming up" 2-day cap
# ---------------------------------------------------------------------------

def test_session_views_coming_up_caps_at_two_days_ahead():
    today = date(2026, 7, 31)
    sessions = [
        {"date": "2026-07-31", "time": "6:00pm"},   # today
        {"date": "2026-08-01", "time": "6:00pm"},   # today + 1 - in window
        {"date": "2026-08-02", "time": "6:00pm"},   # today + 2 - in window
        {"date": "2026-08-03", "time": "6:00pm"},   # today + 3 - outside the render cut
    ]
    today_times, upcoming = render.session_views(sessions, today)

    assert len(today_times) == 1
    upcoming_dates = [u["date"] for u in upcoming]
    assert upcoming_dates == ["2026-08-01", "2026-08-02"]
    assert "2026-08-03" not in upcoming_dates


def test_session_views_today_times_carry_display_and_mins():
    today = date(2026, 7, 31)
    sessions = [{"date": "2026-07-31", "time": "6:15pm"}]
    today_times, _ = render.session_views(sessions, today)
    assert today_times == [{"display": "6:15pm", "mins": 18 * 60 + 15}]


def test_session_views_today_times_sorted_by_minutes_not_string():
    # "10:30am" < "9:15am" as strings, but 9:15am is numerically earlier -
    # the old string-sort had this latent bug.
    today = date(2026, 7, 31)
    sessions = [
        {"date": "2026-07-31", "time": "10:30am"},
        {"date": "2026-07-31", "time": "9:15am"},
    ]
    today_times, _ = render.session_views(sessions, today)
    assert [t["display"] for t in today_times] == ["9:15am", "10:30am"]


# ---------------------------------------------------------------------------
# render.build_context - demotion / "Also showing" cut
# ---------------------------------------------------------------------------

def _minimal_cinemas():
    return [
        {"id": "a", "name": "Cinema A", "area": "Area A", "tier": 1, "url": "https://a.example"},
        {"id": "b", "name": "Cinema B", "area": "Area B", "tier": 1, "url": "https://b.example"},
    ]


def test_build_context_demotes_films_with_zero_rendered_sessions():
    today = date(2026, 7, 31)
    data = {
        "fetchedAt": "2026-07-31T07:00:00+12:00",
        "errors": {},
        "cinemas": {
            "a": {
                "sourceUrl": "https://a.example",
                "films": [
                    {"title": "No Times At All", "sessions": [], "note": None, "tags": []},
                ],
            },
        },
    }
    ctx = render.build_context(data, today, cinemas=_minimal_cinemas())
    assert ctx["films"] == []
    assert len(ctx["also_showing"]) == 1
    assert ctx["also_showing"][0]["title"] == "No Times At All"


def test_build_context_keeps_films_with_today_sessions_as_cards():
    today = date(2026, 7, 31)
    data = {
        "fetchedAt": "2026-07-31T07:00:00+12:00",
        "errors": {},
        "cinemas": {
            "a": {
                "sourceUrl": "https://a.example",
                "films": [
                    {
                        "title": "Has A Session",
                        "sessions": [{"date": "2026-07-31", "time": "6:30pm"}],
                        "note": None,
                        "tags": [],
                    },
                ],
            },
        },
    }
    ctx = render.build_context(data, today, cinemas=_minimal_cinemas())
    assert len(ctx["films"]) == 1
    assert ctx["films"][0]["title"] == "Has A Session"
    assert ctx["films"][0]["hasToday"] is True
    assert "from 6:30pm" in ctx["films"][0]["summary"]
    assert ctx["also_showing"] == []


def test_build_context_sorts_today_films_before_upcoming_only_films():
    today = date(2026, 7, 31)
    data = {
        "fetchedAt": "2026-07-31T07:00:00+12:00",
        "errors": {},
        "cinemas": {
            "a": {
                "sourceUrl": "https://a.example",
                "films": [
                    {
                        "title": "Future Only",
                        "sessions": [{"date": "2026-08-01", "time": "6:00pm"}],
                        "note": None,
                        "tags": [],
                    },
                    {
                        "title": "Showing Tonight",
                        "sessions": [{"date": "2026-07-31", "time": "9:00pm"}],
                        "note": None,
                        "tags": [],
                    },
                ],
            },
        },
    }
    ctx = render.build_context(data, today, cinemas=_minimal_cinemas())
    titles = [f["title"] for f in ctx["films"]]
    assert titles == ["Showing Tonight", "Future Only"]
