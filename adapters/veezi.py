"""Veezi ticketing adapter.

Covers cinemas whose booking system is Veezi (https://www.veezi.com/), which
serves a server-rendered session listing at:

    https://ticketing.oz.veezi.com/sessions/?siteToken=<token>

That page has two tab panels with the same data laid out differently:
  - #sessionsByDateConent: grouped by date, one entry per film per date.
  - #sessionsByFilmConent: grouped by film, each film div containing one
    "sessions" block with a "date-container" per date and one or more
    <time> entries per date-container.

We parse the by-film panel since it gives every date for a film in one
place. See tests/fixtures/veezi_hollywood.html and veezi_bridgeway.html for
real captured markup.

Known real failure mode (see README learnings): Veezi sometimes serves stale
cached HTML with only long-past dates. We always run parsed films through
normalise.sanity_check_dates() before returning, and let StaleDataError
propagate so scrape.py keeps the previous good snapshot.

Some cinema sites (capitol, bridgeway) don't expose their Veezi siteToken in
static homepage HTML — it only shows up in booking links rendered by their
own front-end JS, which itself pulls session data from a same-origin JSON
API (e.g. /api/movie/playing-now) that embeds ticketing.oz.veezi.com
purchase URLs. discover_token() checks the homepage HTML first, then a small
set of well-known API paths used by that site template.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from adapters import AdapterError
from normalise import detect_tags, now_nz_iso, sanity_check_dates, today_nz

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
_TIMEOUT = 15

_SESSIONS_URL_TMPL = "https://ticketing.oz.veezi.com/sessions/?siteToken={token}"

# Booking links look like:
#   https://ticketing.oz.veezi.com/purchase/1234?siteToken=abc123...
# or relative /purchase/1234?siteToken=abc123 - either way the token is
# the same across a whole site, so a plain siteToken= search is enough once
# we know we're looking at content that mentions Veezi at all.
_TOKEN_RE = re.compile(r"siteToken=([A-Za-z0-9]+)")
_VEEZI_HOST_RE = re.compile(r"ticketing\.oz\.veezi\.com")

# Candidate paths to probe (relative to a cinema's own site) when the Veezi
# token isn't sitting in plain homepage HTML. Several small NZ cinema sites
# share a Next.js template that fetches session data client-side from these
# same-origin endpoints, which embed Veezi purchase links.
_DISCOVERY_PATHS = [
    "",  # the homepage itself
    "/api/movie/playing-now",
    "/api/movie/coming-soon",
    "/api/cinema/info",
    "/api/cinema/carousel",
    "/sessions",
    "/whats-on",
    "/now-showing",
]

_token_cache: dict[str, str] = {}


def discover_token(site_url: str) -> str:
    """Find a cinema's Veezi siteToken by crawling its own site.

    Cached per site_url for the lifetime of the process.
    """
    if site_url in _token_cache:
        return _token_cache[site_url]

    base = site_url.rstrip("/")
    last_error: str | None = None
    for path in _DISCOVERY_PATHS:
        url = base + path
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            last_error = f"{url}: {exc}"
            continue
        if resp.status_code != 200:
            last_error = f"{url}: HTTP {resp.status_code}"
            continue
        text = resp.text
        if not _VEEZI_HOST_RE.search(text) and "siteToken=" not in text:
            continue
        m = _TOKEN_RE.search(text)
        if m:
            token = m.group(1)
            _token_cache[site_url] = token
            return token

    detail = f" (last attempt: {last_error})" if last_error else ""
    raise AdapterError(
        f"could not discover veezi siteToken for {site_url}{detail}"
    )


def _format_time(raw: str) -> str:
    """'7:30 PM' -> '7:30pm'."""
    return re.sub(r"\s+", "", raw.strip().lower())


def _parse_date_heading(text: str, today: date) -> str | None:
    """'Thursday 16, July' -> '2026-07-16' (rolling into next year if the
    month/day would otherwise land well in the past)."""
    m = re.search(r"(\d{1,2})\s*,\s*([A-Za-z]+)", text)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2)[:3]
    try:
        month = datetime.strptime(month_name, "%b").month
    except ValueError:
        return None

    year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None

    # Veezi listings only ever look forward. If the naive same-year date is
    # well in the past (e.g. page fetched in December showing January
    # dates), it must belong to next year.
    if candidate < today - timedelta(days=60):
        try:
            candidate = date(year + 1, month, day)
        except ValueError:
            return None
    return candidate.isoformat()


def _parse_sessions_html(html: str, sessions_url: str, today: date | None = None) -> list[dict]:
    today = today or today_nz()
    soup = BeautifulSoup(html, "html.parser")

    container = soup.find(id="sessionsByFilmConent")
    if container is None:
        raise AdapterError(
            f"unexpected markup at {sessions_url}: #sessionsByFilmConent not found"
        )

    films: list[dict] = []
    for film_div in container.find_all("div", class_="film"):
        title_tag = film_div.find("h3", class_="title")
        if title_tag is None:
            continue
        title = " ".join(title_tag.get_text().split())
        if not title:
            continue

        sessions: list[dict] = []
        sessions_block = film_div.find("div", class_="sessions")
        if sessions_block is not None:
            for date_container in sessions_block.find_all("div", class_="date-container"):
                date_heading = date_container.find("h4", class_="date")
                if date_heading is None:
                    continue
                iso_date = _parse_date_heading(date_heading.get_text(strip=True), today)
                if iso_date is None:
                    continue
                for time_tag in date_container.find_all("time"):
                    raw_time = time_tag.get_text(strip=True)
                    if not raw_time:
                        continue
                    sessions.append({"date": iso_date, "time": _format_time(raw_time)})

        note = None
        films.append({
            "title": title,
            "sessions": sessions,
            "note": note,
            "tags": detect_tags(title, note),
        })

    if not films:
        raise AdapterError(
            f"unexpected markup at {sessions_url}: no film entries found in #sessionsByFilmConent"
        )

    return films


def _fetch_with_token(cinema_id: str, token: str) -> dict:
    sessions_url = _SESSIONS_URL_TMPL.format(token=token)

    try:
        resp = requests.get(sessions_url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise AdapterError(f"network error fetching {sessions_url}: {exc}") from exc
    if resp.status_code != 200:
        raise AdapterError(f"HTTP {resp.status_code} fetching {sessions_url}")

    films = _parse_sessions_html(resp.text, sessions_url)
    films = sanity_check_dates(films)  # raises StaleDataError if all dates are stale

    return {
        "cinemaId": cinema_id,
        "films": films,
        "fetchedAt": now_nz_iso(),
        "sourceUrl": sessions_url,
    }


def fetch(cinema: dict) -> dict:
    hardcoded = cinema.get("veeziToken")
    try:
        token = hardcoded or discover_token(cinema["url"])
        return _fetch_with_token(cinema["id"], token)
    except AdapterError:
        # A hard-coded token can rot if the cinema reprovisions Veezi —
        # try once more with a freshly discovered one before giving up.
        if not hardcoded:
            raise
        fresh = discover_token(cinema["url"])
        if fresh == hardcoded:
            raise
        return _fetch_with_token(cinema["id"], fresh)
