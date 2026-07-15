"""Core pipeline tests. No network access - adapters are monkeypatched or
never invoked."""
from __future__ import annotations

import json
import sys
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
