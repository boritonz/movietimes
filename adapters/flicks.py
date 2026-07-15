"""Adapter for flicks.co.nz cinema pages.

flicks.co.nz is the universal fallback (every cinema in the registry has a
flicksSlug) and the primary source for Academy Cinemas. Investigated live on
2026-07-15:

- https://www.flicks.co.nz/cinema/{slug}/?view=all-movies server-renders a
  full "all movies currently screening" grid: one
  <article class="cinema-single-all-movies"> per film, with the title in
  <h3 class="cinema-single-all-movies__title">. This is reliable across every
  cinema tested (Academy, Bridgeway, Capitol, Rialto Newmarket, the three
  Event sites, Reading LynnMall, Hollywood Avondale, both Silky Otter sites).
- Per-film session times are NOT in that markup. Each film card only has a
  "<button ... data-modal-id="all-movies-sessions-popup" data-slug="...">N
  showtimes</button>" that opens a JS-driven modal; the times themselves load
  via a client-side call this adapter does not replicate. The default (no
  ?view=all-movies) page has an empty JS-hydrated "timetable" day-tab strip
  with the same story: no times in the static HTML. So sessions is always
  [] in practice today -- the extraction hook below is still real code (not
  a stub) in case flicks.co.nz ever starts server-rendering times.
- KNOWN QUIRK per the brief ("SSR inconsistent per cinema, worked for
  Academy, not Bridgeway") turned out to be a bad slug in the registry
  (adapters/__init__.py has "bridgeway-cinema", which 404s -- the live slug
  is "bridgeway"). See fetch()'s retry-without-view-param behaviour below,
  which is kept as insurance per the brief even though the actual fix for
  Bridgeway is a registry correction (reported separately, not made here
  since adapters/__init__.py is out of scope for this file).
"""
from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from adapters import AdapterError
from normalise import detect_tags, now_nz_iso, sanity_check_dates

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-NZ,en;q=0.9",
}

TIMEOUT = 15
BASE_URL = "https://www.flicks.co.nz/cinema/{slug}/"


def _extract_films(html: str) -> list[dict]:
    """Parse the flicks.co.nz "all movies" grid (or whatever markup is
    given) into our film dict shape. Title is required; sessions come from
    whatever time-like text is attached to the film block, defaulting to []
    since flicks doesn't currently server-render per-film times."""
    soup = BeautifulSoup(html, "html.parser")
    films: list[dict] = []
    seen: set[str] = set()

    for h3 in soup.select("h3.cinema-single-all-movies__title"):
        title = h3.get_text(strip=True)
        if not title or title in seen:
            continue
        seen.add(title)

        article = h3.find_parent("article") or h3.parent
        sessions = _extract_sessions(article) if article else []
        note = _extract_note(article) if article else None

        films.append({
            "title": title,
            "sessions": sessions,
            "note": note,
            "tags": detect_tags(title, note),
        })

    return films


def _extract_sessions(article) -> list[dict]:
    """Best-effort: pull {date, time} pairs out of a film's markup if
    flicks ever ships them server-side (e.g. a data-date/data-time
    attribute, or "6:15pm" style text near the film block). Today this
    always returns [] because times load behind a JS modal -- see module
    docstring -- but the hook is real so a future markup change picks up
    times automatically."""
    sessions: list[dict] = []
    for node in article.select("[data-date][data-time]"):
        date = node.get("data-date")
        time = node.get("data-time")
        if date and time:
            sessions.append({"date": date, "time": time})
    return sessions


def _extract_note(article) -> str | None:
    """Surface an "opens <date>" style label if present near the title."""
    note_node = article.select_one(".cinema-single-all-movies__opens, .js--openingDate")
    if note_node:
        text = note_node.get_text(strip=True)
        return text or None
    return None


def _fetch_html(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise AdapterError(f"request to {url} failed: {exc}") from exc

    if resp.status_code != 200:
        raise AdapterError(f"{url} returned HTTP {resp.status_code}")

    return resp.text


def fetch(cinema: dict) -> dict:
    slug = cinema.get("flicksSlug")
    if not slug:
        raise AdapterError(f"{cinema['id']}: no flicksSlug configured")

    base = BASE_URL.format(slug=slug)
    all_movies_url = base + "?view=all-movies"

    html = _fetch_html(all_movies_url)
    films = _extract_films(html)

    if not films:
        # KNOWN QUIRK: SSR is inconsistent per cinema -- retry the plain
        # cinema page before giving up.
        html = _fetch_html(base)
        films = _extract_films(html)

    if not films:
        raise AdapterError(
            f"{cinema['id']}: no films found on {all_movies_url} or {base} "
            "-- flicks.co.nz served an empty/unrecognised page (wrong "
            "flicksSlug, or the cinema has no current listing)"
        )

    if any(f["sessions"] for f in films):
        films = sanity_check_dates(films)

    return {
        "cinemaId": cinema["id"],
        "films": films,
        "fetchedAt": now_nz_iso(),
        "sourceUrl": base,
    }
