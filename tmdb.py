"""TMDB poster/rating resolution with a committed on-disk cache.

Maps film titles (by normalise.dedupe_key) to TMDB poster paths and vote
averages. The cache (data/tmdb_cache.json) is committed to the repo so the
daily Action only queries TMDB for titles it has never seen; unmatched
titles are cached as null so one-off events ("Return to Trashathon") aren't
re-queried every night. Posters are progressive enhancement — a missing
match must never break the page.

Key lookup order: TMDB_API_KEY env var, then .env.local in the repo root.
Without a key, resolve_all() is a no-op that returns the existing cache.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

from normalise import dedupe_key, now_nz_iso

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "tmdb_cache.json"
SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
POSTER_BASE = "https://image.tmdb.org/t/p/w342"
_TIMEOUT = 15

# Re-check unmatched titles this many days after the last attempt: posters
# often appear on TMDB a week or two before a local one-off gets listed.
RECHECK_MISSES_AFTER_DAYS = 14


def api_key() -> str | None:
    key = os.environ.get("TMDB_API_KEY")
    if key:
        return key.strip()
    env_local = ROOT / ".env.local"
    if env_local.exists():
        for line in env_local.read_text(encoding="utf-8").splitlines():
            if line.startswith("TMDB_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def load_cache() -> dict:
    if CACHE_PATH.exists():
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def split_year(title: str) -> tuple[str, int | None]:
    """"Moana (2026)" -> ("Moana", 2026); years are re-release/remake hints."""
    m = re.search(r"\((?:19|20)(\d{2})\)", title)
    if not m:
        return title.strip(), None
    year = int(m.group(0)[1:-1])
    return re.sub(r"\s*\((?:19|20)\d{2}\)", "", title).strip(), year


def _search(key: str, query: str, year: int | None) -> list[dict]:
    params = {"api_key": key, "query": query}
    if year:
        params["primary_release_year"] = year
    resp = requests.get(SEARCH_URL, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("results", [])


def resolve_title(key: str, title: str) -> dict | None:
    """Return a cache entry for one title, or None when TMDB has no match."""
    clean, year = split_year(title)
    try:
        results = _search(key, clean, year)
        if not results and year:
            results = _search(key, clean, None)
    except requests.RequestException:
        return None  # transient failure: treat as miss, recheck window applies

    if not results:
        return None

    want = dedupe_key(clean)
    exact = [r for r in results
             if dedupe_key(r.get("title", "")) == want
             or dedupe_key(r.get("original_title", "")) == want]
    match, quality = (exact[0], "exact") if exact else (results[0], "fuzzy")

    release = match.get("release_date") or ""
    return {
        "tmdbId": match["id"],
        "title": match.get("title"),
        "year": int(release[:4]) if release[:4].isdigit() else None,
        "posterPath": match.get("poster_path"),
        "rating": round(match["vote_average"], 1) if match.get("vote_average") else None,
        "matched": quality,
        "checkedAt": now_nz_iso(),
    }


def _stale_miss(entry: dict | None) -> bool:
    from datetime import datetime, timedelta
    if entry is not None and entry.get("tmdbId"):
        return False  # hits never go stale (poster paths are permanent)
    checked = (entry or {}).get("checkedAt")
    if not checked:
        return True
    try:
        age = datetime.fromisoformat(now_nz_iso()) - datetime.fromisoformat(checked)
    except ValueError:
        return True
    return age > timedelta(days=RECHECK_MISSES_AFTER_DAYS)


def resolve_all(titles: list[str]) -> dict:
    """Ensure every title has a cache row (hit or recorded miss); returns the
    full cache. Only queries TMDB for unseen titles and stale misses."""
    cache = load_cache()
    key = api_key()
    if not key:
        print("[tmdb] no API key found - skipping poster resolution")
        return cache

    queried = 0
    for title in titles:
        k = dedupe_key(title)
        if not k:
            continue
        existing = cache.get(k)
        if not _stale_miss(existing):
            continue
        entry = resolve_title(key, title)
        queried += 1
        cache[k] = entry if entry else {
            "tmdbId": None, "posterPath": None, "rating": None,
            "matched": None, "checkedAt": now_nz_iso(),
        }

    if queried:
        save_cache(cache)
    hits = sum(1 for v in cache.values() if v and v.get("tmdbId"))
    print(f"[tmdb] {len(cache)} titles cached, {hits} matched, {queried} queried this run")
    return cache


def poster_url(entry: dict | None) -> str | None:
    if entry and entry.get("posterPath"):
        return POSTER_BASE + entry["posterPath"]
    return None


if __name__ == "__main__":
    with open(ROOT / "data" / "latest.json", encoding="utf-8") as f:
        latest = json.load(f)
    titles = {f["title"] for c in latest["cinemas"].values() for f in c["films"]}
    cache = resolve_all(sorted(titles))
    misses = [t for t in sorted(titles) if not (cache.get(dedupe_key(t)) or {}).get("tmdbId")]
    if misses:
        print("[tmdb] unmatched:", "; ".join(misses[:20]) + (" …" if len(misses) > 20 else ""))
