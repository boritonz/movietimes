"""Letterboxd watchlist fetch for the "on your watchlist" badge.

Letterboxd has no public API; the watchlist page is public HTML with film
titles in the poster grid's img alt attributes, paginated at 28 per page.
Results are cached to data/watchlist.json so render.py never needs the
network, and a failed fetch keeps the previous cache (same degradation
philosophy as the cinema adapters).
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from normalise import now_nz_iso

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "watchlist.json"
USERNAME = "kavic"
PER_PAGE = 28
_TIMEOUT = 15
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1"),
    "Accept-Language": "en-NZ,en;q=0.9",
}


def _page_titles(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.select("ul.poster-list img[alt], li.poster-container img[alt], "
                       "section img.image[alt]")
    titles = [img["alt"].strip() for img in grid if img["alt"].strip()]
    if not titles:  # markup drifted? fall back to any alt inside the content area
        content = soup.find("section", id="content") or soup
        titles = [img["alt"].strip() for img in content.find_all("img", alt=True)
                  if img["alt"].strip()]
    return titles


def _film_count(html: str) -> int | None:
    m = re.search(r"([\d,]+)(?:&nbsp;|\s)films?", html)
    return int(m.group(1).replace(",", "")) if m else None


def fetch_watchlist(username: str = USERNAME) -> list[str]:
    """All film titles on the user's public watchlist. Raises on failure."""
    base = f"https://letterboxd.com/{username}/watchlist/"
    resp = requests.get(base, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    titles = _page_titles(resp.text)
    total = _film_count(resp.text)

    if total and total > len(titles):
        for page in range(2, math.ceil(total / PER_PAGE) + 1):
            r = requests.get(f"{base}page/{page}/", headers=_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            titles.extend(_page_titles(r.text))

    # The profile avatar's alt is the display name, not a film — it appears
    # outside the poster grid, but the fallback path can catch it.
    return [t for t in dict.fromkeys(titles) if t.lower() != username.lower()
            and t.lower() != "kavi"]


def refresh_cache(username: str = USERNAME) -> dict:
    """Fetch and cache the watchlist; on failure keep the previous cache."""
    try:
        titles = fetch_watchlist(username)
        cache = {"username": username, "fetchedAt": now_nz_iso(), "films": titles}
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"[letterboxd] {len(titles)} films on {username}'s watchlist")
        return cache
    except Exception as exc:  # noqa: BLE001 - never break the scrape
        print(f"[letterboxd] fetch failed ({exc}); keeping previous cache")
        return load_cache()


def load_cache() -> dict:
    if CACHE_PATH.exists():
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"username": USERNAME, "fetchedAt": None, "films": []}


if __name__ == "__main__":
    refresh_cache()
