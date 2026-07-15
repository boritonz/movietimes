"""EVT-platform adapter — rialto.co.nz + eventcinemas.co.nz (Queen St, Newmarket,
St Lukes) all run the same "EVO" cinema-management platform. Each site is a
server-rendered-shell + client-templated (jQuery templates) app: the cinema and
sessions *pages* are real HTML (no headless browser needed), but the session
data itself is loaded via a same-origin JSON XHR that the page's own JS
(EVO.sessions / EVO.cinemas in the bundled site.js) calls once per day:

    GET https://<site>/Cinemas/GetSessions?cinemaIds=<id>[&date=YYYY-MM-DD]

Omitting `date` returns "today" plus a `Data.Dates` array listing every date
the site currently has sessions for (spot checks: Event Cinemas ~30-35 dates
out to ~7 weeks, Rialto ~150 dates because a handful of far-future placeholder
sessions exist — we only walk dates inside normalise.DATE_WINDOW_DAYS).
Response shape (`Data.Movies[].CinemaModels[].Sessions[]`) confirmed live
2026-07-15 — see scratch fetch in session notes for a full sample.

Numeric EVT cinema ids + the human `/cinema/<slug>/sessions` page were found by
fetching the server-rendered cinema-finder page (eventcinemas.co.nz/cinemas),
which embeds `<a href="/cinema/<Slug>" data-cinema-name="...">` links, then
loading each `/cinema/<slug>/sessions` page for the hidden
`<input id="Cinema_Id" value="...">` that the site's own JS reads before
calling GetSessions. Confirmed live 2026-07-15:

    rialto           -> https://www.rialto.co.nz          cinemaId=751  slug=newmarket
    event-queen      -> https://www.eventcinemas.co.nz     cinemaId=502  slug=Queen-Street
    event-newmarket  -> https://www.eventcinemas.co.nz     cinemaId=520  slug=Newmarket-Westfield
    event-stlukes    -> https://www.eventcinemas.co.nz     cinemaId=509  slug=st-lukes

Note the registry's `url` field for the three Event cinemas
(eventcinemas.co.nz/cinemas/<lowercase>) 404s on the live site — the real,
working, human-facing URL is /cinema/<Slug>/sessions, which is what we use for
`sourceUrl` here.

No API key / auth needed for any of this — it's the same JSON the public page
uses. GitHub Actions runner IPs are a real risk (EVT/Vista-family sites are
known to bot-wall datacenter IPs); a 403 here is treated as a clean
AdapterError rather than retried, so scrape.py falls back to Flicks.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import requests

from adapters import AdapterError
from normalise import DATE_WINDOW_DAYS, detect_tags, now_nz_iso, sanity_check_dates, today_nz

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-NZ,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}
_TIMEOUT = 15

# cinemaId (adapters.CINEMAS) -> (site base URL, numeric EVT cinema id, URL slug
# used for the human-facing /cinema/<slug>/sessions page).
_SITES = {
    "rialto": ("https://www.rialto.co.nz", 751, "newmarket"),
    "event-queen": ("https://www.eventcinemas.co.nz", 502, "Queen-Street"),
    "event-newmarket": ("https://www.eventcinemas.co.nz", 520, "Newmarket-Westfield"),
    "event-stlukes": ("https://www.eventcinemas.co.nz", 509, "st-lukes"),
}

# Session-level attribute codes (see Data.Movies[].CinemaModels[].Sessions[].
# Attributes[].Code) that mark a screening as a special "event" or "kids"
# session even when detect_tags() finds nothing in the title.
_EVENT_ATTR_CODES = {"Q&A", "SENS", "SENR", "Alt Cont"}
_KIDS_ATTR_CODES = {"Little Kids", "SENS"}


def _fmt_time(iso_dt: str) -> str:
    """'2026-07-15T15:40' -> '3:40pm'"""
    dt = datetime.fromisoformat(iso_dt)
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{suffix}"


def _get_sessions(session: requests.Session, base: str, evt_id: int, referer: str,
                   date_str: str | None = None) -> dict:
    params = {"cinemaIds": str(evt_id)}
    if date_str:
        params["date"] = date_str
    headers = dict(_HEADERS)
    headers["Referer"] = referer

    try:
        resp = session.get(f"{base}/Cinemas/GetSessions", params=params, headers=headers,
                            timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise AdapterError(
            f"evt: network error fetching {base}/Cinemas/GetSessions "
            f"cinemaId={evt_id} date={date_str or 'today'}: {exc}") from exc

    if resp.status_code == 403:
        raise AdapterError(
            f"evt: 403 Forbidden from {base} (bot wall) fetching cinemaId={evt_id}")
    if resp.status_code != 200:
        raise AdapterError(
            f"evt: HTTP {resp.status_code} from {base}/Cinemas/GetSessions cinemaId={evt_id}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise AdapterError(
            f"evt: non-JSON response from {base} (likely a robot/interstitial page)") from exc

    if not isinstance(payload, dict) or not payload.get("Success"):
        raise AdapterError(f"evt: API reported failure for cinemaId={evt_id}: {payload!r:.200}")

    data = payload.get("Data")
    if not isinstance(data, dict):
        raise AdapterError(f"evt: unexpected response shape for cinemaId={evt_id}")
    return data


def _merge_payload(films_by_id: dict[int, dict], payload: dict, evt_id: int) -> None:
    """Merge one GetSessions `Data` payload's movies/sessions into films_by_id
    (movieId -> {"title", "raw_sessions": [(StartTime, session_dict)], "attrs"}).
    Pure/offline — used both by fetch() (per live payload) and by tests
    (against a saved fixture payload).
    """
    for movie in payload.get("Movies", []):
        movie_id = movie.get("Id")
        entry = films_by_id.setdefault(movie_id, {
            "title": (movie.get("Name") or "").strip(),
            "raw_sessions": [],
            "attrs": set(),
        })
        for cinema_model in movie.get("CinemaModels", []):
            if cinema_model.get("Id") != evt_id:
                continue
            for sess in cinema_model.get("Sessions", []):
                start = sess.get("StartTime")
                if not start:
                    continue
                entry["raw_sessions"].append(
                    (start, {"date": start[:10], "time": _fmt_time(start)}))
                for attr in sess.get("Attributes", []) or []:
                    code = attr.get("Code")
                    if code:
                        entry["attrs"].add(code)


def _finalize_films(films_by_id: dict[int, dict], today: date | None = None) -> list[dict]:
    """Turn accumulated films_by_id into the adapter contract's films list,
    applying tag detection and normalise.sanity_check_dates()."""
    films = []
    for entry in films_by_id.values():
        title = entry["title"]
        if not title:
            continue
        sessions = [s for _, s in sorted(entry["raw_sessions"], key=lambda pair: pair[0])]
        tags = set(detect_tags(title))
        if entry["attrs"] & _EVENT_ATTR_CODES:
            tags.add("event")
        if entry["attrs"] & _KIDS_ATTR_CODES:
            tags.add("kids")
        films.append({
            "title": title,
            "sessions": sessions,
            "note": None,
            "tags": sorted(tags),
        })
    return sanity_check_dates(films, today=today)


def fetch(cinema: dict) -> dict:
    cinema_id = cinema["id"]
    site = _SITES.get(cinema_id)
    if site is None:
        raise AdapterError(f"evt: no site mapping for cinema '{cinema_id}'")
    base, evt_id, slug = site
    source_url = f"{base}/cinema/{slug}/sessions"

    session = requests.Session()

    data = _get_sessions(session, base, evt_id, source_url)
    dates = data.get("Dates") or []
    today = today_nz()
    horizon = (today + timedelta(days=DATE_WINDOW_DAYS)).isoformat()
    today_iso = today.isoformat()
    dates_in_window = [d for d in dates if today_iso <= d <= horizon]

    films_by_id: dict[int, dict] = {}
    _merge_payload(films_by_id, data, evt_id)  # "today"/default response already included
    selected_date = data.get("SelectedDate")

    for d in dates_in_window:
        if d == selected_date:
            continue  # already ingested above
        payload = _get_sessions(session, base, evt_id, source_url, date_str=d)
        _merge_payload(films_by_id, payload, evt_id)

    if not films_by_id:
        raise AdapterError(f"evt: no films returned for cinemaId={evt_id} at {base}")

    films = _finalize_films(films_by_id)

    return {
        "cinemaId": cinema_id,
        "films": films,
        "fetchedAt": now_nz_iso(),
        "sourceUrl": source_url,
    }
