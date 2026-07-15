"""Offline tests for adapters/flicks.py. No network access -- everything
parses fixtures saved from a live fetch on 2026-07-15."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import AdapterError
from adapters import flicks

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def test_extract_films_from_academy_all_movies_view():
    html = _read("flicks_academy.html")
    films = flicks._extract_films(html)

    assert len(films) == 44
    titles = {f["title"] for f in films}
    assert "The Invite" in titles
    assert "Obsession" in titles
    assert "Before Sunrise" in titles

    # Contract shape + today's-sessions-if-present behaviour: flicks doesn't
    # server-render per-film times (see module docstring), so every film
    # should have sessions == [] rather than fabricated data.
    for f in films:
        assert isinstance(f["title"], str) and f["title"]
        assert f["sessions"] == []
        assert isinstance(f["tags"], list)


def test_extract_films_default_view_is_empty():
    # The plain cinema page (no ?view=all-movies) never had the all-movies
    # grid for Academy in what we captured live -- this fixture proves the
    # retry-without-view-param branch in fetch() has something real to fall
    # back past (i.e. it also comes back empty here, so fetch() must raise).
    html = _read("flicks_academy_default.html")
    films = flicks._extract_films(html)
    assert films == []


def test_fetch_builds_contract_shape(monkeypatch):
    html = _read("flicks_academy.html")

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)

        class Resp:
            status_code = 200
            text = html

        return Resp()

    monkeypatch.setattr(flicks.requests, "get", fake_get)

    cinema = {"id": "academy", "flicksSlug": "academy-cinemas"}
    result = flicks.fetch(cinema)

    assert calls == ["https://www.flicks.co.nz/cinema/academy-cinemas/?view=all-movies"]
    assert result["cinemaId"] == "academy"
    assert result["sourceUrl"] == "https://www.flicks.co.nz/cinema/academy-cinemas/"
    assert len(result["films"]) == 44
    assert "fetchedAt" in result and result["fetchedAt"]


def test_fetch_retries_without_view_param_then_raises(monkeypatch):
    empty_html = _read("flicks_academy_default.html")

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)

        class Resp:
            status_code = 200
            text = empty_html

        return Resp()

    monkeypatch.setattr(flicks.requests, "get", fake_get)

    cinema = {"id": "bridgeway", "flicksSlug": "bridgeway-cinema"}
    with pytest.raises(AdapterError):
        flicks.fetch(cinema)

    # Retried the plain cinema page before giving up.
    assert calls == [
        "https://www.flicks.co.nz/cinema/bridgeway-cinema/?view=all-movies",
        "https://www.flicks.co.nz/cinema/bridgeway-cinema/",
    ]


def test_fetch_raises_on_http_error(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        class Resp:
            status_code = 404
            text = "not found"

        return Resp()

    monkeypatch.setattr(flicks.requests, "get", fake_get)

    with pytest.raises(AdapterError):
        flicks.fetch({"id": "bad-slug", "flicksSlug": "does-not-exist"})


def test_fetch_raises_when_no_slug():
    with pytest.raises(AdapterError):
        flicks.fetch({"id": "no-slug-cinema"})
