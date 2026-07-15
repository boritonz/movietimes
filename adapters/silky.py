"""Adapter for Silky Otter cinemas (silky-orakei, silky-ponsonby).

Investigated live on 2026-07-15. Summary: silkyotter.co.nz cannot currently
be scraped for film or session data by this adapter, by design of the site,
not by lack of effort -- see "What was tried" below. Both cinemas have a
flicksSlug in the registry (silky-otter-cinemas-orakei /
-ponsonby) and flicks.co.nz *does* serve a working "all movies" page for
both, so adapters.__init__.get_fallback() already routes to adapters/flicks.py
whenever this adapter raises AdapterError -- which is what it does. This is
the intended v1 outcome for these two cinemas, not a bug.

What was tried
---------------
1. Plain requests.get() to https://www.silkyotter.co.nz/films: browser-style
   headers get a 200 (a bare curl/no-UA request gets 403), but the response
   body is only a Next.js/Turbopack SPA shell -- an empty
   <div id="__next"></div> plus a __NEXT_DATA__ blob containing nothing but
   a short-lived GAS auth token (`gasToken`, Vista "Lumos" platform) and CMS
   config. No film titles, no JSON-LD, nothing server-rendered. Every other
   route (/, /robots.txt, /sitemap.xml) 404-catches to the same empty shell,
   so there's no static/SEO fallback content anywhere on the site.
2. Loaded the same page in a real (headless) browser via the preview tool
   and watched network traffic for 15+ seconds: the SPA never hydrates in
   that environment (#__next stays empty, zero XHR/fetch fires), so there is
   nothing to observe even with full JS execution.
3. Downloaded and grepped all 32 Next.js JS chunks for the API this page
   would call once hydrated. Found two real client factories:
   - `createCmsClient` -> hits `{cmsConfig.apiUrl}/api/v1/sales-channels/web/...`
     (confirmed live: cms-api-www.silkyotter.co.nz responds with real JSON
     404s under that base), used for CMS text/config, not films.
   - `createOcapiClient` -> Vista's Omnichannel API, exposing
     `ocapi/v1/sites`, `ocapi/v1/sites/{siteId}/films`,
     `ocapi/v1/showtimes/by-business-date/{date}` etc (this is where
     per-site film/session data actually lives). Its base URL isn't a
     static constant in the bundle -- it's resolved at runtime through a
     GAS/MovieXchange discovery/auth flow this adapter does not implement.
     Reverse-engineering that flow (and finding the Ōrākei vs Ponsonby site
     IDs it would need) is out of scope for a v1 scraper; per the brief,
     "titles-only... rather than burning hours" -- and in this case even
     titles-only isn't reachable without that same auth flow, since the
     site never serves any film data outside of it.

If silkyotter.co.nz ever ships server-rendered film data (a JSON-LD block,
or plain markup) `_extract_films` below will pick it up for free and this
adapter will stop raising -- it isn't a stub, it's just never had real input
to work with yet.
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
FILMS_URL = "https://www.silkyotter.co.nz/films"


def _extract_films(html: str) -> list[dict]:
    """Best-effort, non-stub extraction in case silkyotter.co.nz ever ships
    server-rendered film data. Tries, in order: schema.org JSON-LD
    (Movie/ItemList), then plain <a href="/films/...">Title</a> markup."""
    soup = BeautifulSoup(html, "html.parser")
    films: list[dict] = []
    seen: set[str] = set()

    import json

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for entry in data if isinstance(data, list) else [data]:
            if not isinstance(entry, dict):
                continue
            items = entry.get("itemListElement") if entry.get("@type") == "ItemList" else [entry]
            for item in items or []:
                item = item.get("item", item) if isinstance(item, dict) else item
                if isinstance(item, dict) and item.get("@type") == "Movie":
                    title = item.get("name")
                    if title and title not in seen:
                        seen.add(title)
                        films.append({"title": title, "sessions": [], "note": None})

    if not films:
        for a in soup.select('a[href^="/films/"]'):
            title = a.get_text(strip=True)
            if title and title not in seen:
                seen.add(title)
                films.append({"title": title, "sessions": [], "note": None})

    for f in films:
        f["tags"] = detect_tags(f["title"], f.get("note"))

    return films


def fetch(cinema: dict) -> dict:
    try:
        resp = requests.get(FILMS_URL, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise AdapterError(f"{cinema['id']}: request to {FILMS_URL} failed: {exc}") from exc

    if resp.status_code != 200:
        raise AdapterError(f"{cinema['id']}: {FILMS_URL} returned HTTP {resp.status_code}")

    films = _extract_films(resp.text)

    if not films:
        raise AdapterError(
            f"{cinema['id']}: no film data on {FILMS_URL} -- silkyotter.co.nz "
            "is a fully client-rendered SPA (Vista 'Lumos' platform) whose "
            "film/session API requires a GAS/MovieXchange auth flow this "
            "adapter doesn't implement (see module docstring). Falls back "
            "to flicks.co.nz via adapters.get_fallback()."
        )

    if any(f["sessions"] for f in films):
        films = sanity_check_dates(films)

    return {
        "cinemaId": cinema["id"],
        "films": films,
        "fetchedAt": now_nz_iso(),
        "sourceUrl": FILMS_URL,
    }
