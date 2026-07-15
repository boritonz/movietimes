"""Offline tests for adapters/silky.py. No network access.

silkyotter.co.nz never serves film data in HTML today (fully client-side
SPA -- see adapters/silky.py module docstring for the live investigation),
so the "real fixture" for this adapter is the empty SPA shell, and
fetch() raising AdapterError against it is the correct, expected behaviour
(scrape.py's fallback then routes to adapters/flicks.py). A second,
explicitly-synthetic fixture exercises the JSON-LD parsing path so that
logic isn't dead code.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import AdapterError
from adapters import silky

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def test_extract_films_real_shell_has_no_films():
    # Real response captured live from https://www.silkyotter.co.nz/films
    # with full browser headers (200 OK) -- just an empty Next.js shell.
    html = _read("silky_films_shell.html")
    assert silky._extract_films(html) == []


def test_extract_films_synthetic_jsonld():
    # Synthetic fixture (see file header) proving the JSON-LD extraction
    # path works, for whenever/if the live site ever ships it.
    html = _read("silky_populated_sample.html")
    films = silky._extract_films(html)

    titles = {f["title"] for f in films}
    assert titles == {"Toy Story 5", "The Invite", "Kids Club: Moana (2026)"}
    for f in films:
        assert f["sessions"] == []
        assert isinstance(f["tags"], list)

    kids_film = next(f for f in films if f["title"] == "Kids Club: Moana (2026)")
    assert "kids" in kids_film["tags"]


def test_fetch_raises_adaptererror_against_real_shell(monkeypatch):
    html = _read("silky_films_shell.html")

    def fake_get(url, headers=None, timeout=None):
        class Resp:
            status_code = 200
            text = html

        return Resp()

    monkeypatch.setattr(silky.requests, "get", fake_get)

    with pytest.raises(AdapterError):
        silky.fetch({"id": "silky-orakei"})


def test_fetch_raises_on_http_error(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        class Resp:
            status_code = 403
            text = "forbidden"

        return Resp()

    monkeypatch.setattr(silky.requests, "get", fake_get)

    with pytest.raises(AdapterError):
        silky.fetch({"id": "silky-ponsonby"})
