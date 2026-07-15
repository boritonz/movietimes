"""Reading Cinemas NZ adapter (New Lynn / LynnMall).

readingcinemas.co.nz is a React SPA (create-react-app, static-hosted on S3 +
CloudFront — the raw HTML is a ~3KB shell with no server-rendered content) that
talks to a single AU/NZ-shared, Vista-backed API at
https://prod-api.readingcinemas.com.au. Two calls, discovered by pulling apart
the app's minified bundle (see below) and confirming live 2026-07-15:

  1. GET /settings/{countryId}   (countryId=2 is New Zealand; 1=AU, 3=Angelika AU)
     Anonymous, no auth needed. Returns
     {"data": {"settings": {"token": "<Cognito JWT, ~1h TTL>", "VistaUrl":
     "prod-nz-vista.readingcinemas.co.nz", "defaultCinema": "Rotorua", ...}}}.
     The `token` must be sent as `Authorization: Bearer <token>` on every
     subsequent call. This isn't a login — it's just how the SPA bootstraps a
     short-lived access token for itself on every page load (confirmed by
     reading the redux reducer: GET_SETTINGS_SUCCESS stores payload.token and
     every other axios call attaches it via an Authorization header
     interceptor). Skipping it gets a bare 401 {"message":"Unauthorized"} from
     API Gateway.

  2. GET /films?cinemaId=<slug>&status=nowShowing&countryId=2   (Bearer-authed)
     Returns {"data": [{"name", "status" ("Now showing"|"Coming soon"),
     "release_date", "showdates": [{"date": "YYYY-MM-DD", "showtypes":
     [{"type": "Standard"|"Gold"|"TitanXC", "showtimes": [{"date_time":
     "2026-07-15T15:45:00+12:00", ...}]}]}], ...}]}. One call returns *every*
     film (now-showing and coming-soon/advance-ticket) with every scheduled
     date already attached — live sample spanned today out to ~4-5 weeks with
     no pagination needed, unlike the EVT platform's one-request-per-day API.

Cinema slugs, from GET /getcinemas?countryId=2 (also Bearer-authed): Dunedin,
Invercargi, Lynnmall, Napier, Porirua, Rotorua, Thepalms. New Lynn / LynnMall
mall cinema = "Lynnmall".

Bundle-reading notes for future maintainers: fetch the JS bundle from the apex
domain (readingcinemas.co.nz/static/js/main.<hash>.js), not `www.` — the `www`
CloudFront distribution serves the SPA-fallback index.html (200, text/html)
for hashed static-asset paths for reasons that weren't investigated further,
while the apex domain serves the real file. The minified axios path constants
are `HO="settings"`, `VO="getcinemas"`, `$O="films"` in that bundle as of the
2026-07-15 build; they will drift on every deploy since they're just
webpack-assigned single/double-letter identifiers — re-grep for the string
literals ("settings", "getcinemas", "films") if this breaks, not the variable
names.

No CAPTCHA/WAF challenge was hit for any of this — it's the same JSON the
public site's own JS fetches, just called directly.
"""
from __future__ import annotations

from datetime import date, datetime

import requests

from adapters import AdapterError
from normalise import detect_tags, now_nz_iso, sanity_check_dates

_API_BASE = "https://prod-api.readingcinemas.com.au"
_COUNTRY_ID_NZ = 2

# cinemaId (adapters.CINEMAS) -> Reading's own cinema slug.
_CINEMA_SLUGS = {
    "reading-newlynn": "Lynnmall",
}

_SOURCE_URL = "https://www.readingcinemas.co.nz/cinemas"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-NZ,en;q=0.9",
    "Referer": "https://www.readingcinemas.co.nz/",
    "Origin": "https://www.readingcinemas.co.nz",
}
_TIMEOUT = 15


def _fmt_time(dt: datetime) -> str:
    """datetime(...,15,45) -> '3:45pm'"""
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{suffix}"


def _get_token(session: requests.Session) -> str:
    url = f"{_API_BASE}/settings/{_COUNTRY_ID_NZ}"
    try:
        resp = session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise AdapterError(f"reading: network error fetching {url}: {exc}") from exc

    if resp.status_code == 403:
        raise AdapterError(f"reading: 403 Forbidden fetching {url} (bot wall)")
    if resp.status_code != 200:
        raise AdapterError(f"reading: HTTP {resp.status_code} fetching {url}")

    try:
        token = resp.json()["data"]["settings"]["token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AdapterError(f"reading: unexpected /settings response shape: {exc}") from exc
    if not token:
        raise AdapterError("reading: /settings response had no token")
    return token


def _note_for(film: dict) -> str | None:
    if film.get("status") != "Coming soon":
        return None
    release_date = film.get("release_date")
    if not release_date:
        return None
    try:
        rd = datetime.fromisoformat(release_date).date()
    except ValueError:
        return None
    return f"opens {rd.strftime('%a')} {rd.day} {rd.strftime('%b')}"


def _parse_films_payload(raw_films: list, today: date | None = None) -> list[dict]:
    """Turn the /films response's `data` list into the adapter contract's films
    list. Pure/offline — used both by fetch() (live response) and by tests
    (against a saved fixture)."""
    films = []
    for f in raw_films:
        title = (f.get("name") or "").strip()
        if not title:
            continue

        raw_sessions = []
        for showdate in f.get("showdates") or []:
            for showtype in showdate.get("showtypes") or []:
                for showtime in showtype.get("showtimes") or []:
                    raw_dt = showtime.get("date_time")
                    if not raw_dt:
                        continue
                    try:
                        dt = datetime.fromisoformat(raw_dt)
                    except ValueError:
                        continue
                    raw_sessions.append((raw_dt, {"date": dt.date().isoformat(),
                                                   "time": _fmt_time(dt)}))

        sessions = [s for _, s in sorted(raw_sessions, key=lambda pair: pair[0])]
        note = _note_for(f)

        films.append({
            "title": title,
            "sessions": sessions,
            "note": note,
            "tags": detect_tags(title, note),
        })

    return sanity_check_dates(films, today=today)


def fetch(cinema: dict) -> dict:
    cinema_id = cinema["id"]
    slug = _CINEMA_SLUGS.get(cinema_id)
    if not slug:
        raise AdapterError(f"reading: no cinema slug mapping for '{cinema_id}'")

    session = requests.Session()
    token = _get_token(session)

    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    params = {"cinemaId": slug, "status": "nowShowing", "countryId": _COUNTRY_ID_NZ}

    try:
        resp = session.get(f"{_API_BASE}/films", params=params, headers=headers,
                            timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise AdapterError(f"reading: network error fetching films for {slug}: {exc}") from exc

    if resp.status_code == 403:
        raise AdapterError(f"reading: 403 Forbidden fetching films for {slug} (bot wall)")
    if resp.status_code != 200:
        raise AdapterError(f"reading: HTTP {resp.status_code} fetching films for {slug}")

    try:
        raw_films = resp.json()["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AdapterError(f"reading: unexpected /films response shape: {exc}") from exc

    if not isinstance(raw_films, list) or not raw_films:
        raise AdapterError(f"reading: no films returned for cinemaId={slug}")

    films = _parse_films_payload(raw_films)

    return {
        "cinemaId": cinema_id,
        "films": films,
        "fetchedAt": now_nz_iso(),
        "sourceUrl": _SOURCE_URL,
    }
