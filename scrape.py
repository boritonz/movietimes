"""Orchestrator: fetch listings for every cinema in adapters.CINEMAS and
write data/latest.json.

Per cinema:
  1. Try the cinema's primary adapter (adapters.get_adapter).
  2. On any failure (missing module, network error, StaleDataError, ...)
     try the Flicks fallback (adapters.get_fallback).
  3. On total failure, keep whatever entry data/latest.json already had for
     that cinema (old fetchedAt and all), or if there is none, fall back to
     the verified data/seed.json snapshot. The failure is recorded in the
     top-level "errors" dict either way.

A single cinema failing never fails the run: main() always returns 0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import letterboxd
import tmdb
from adapters import CINEMAS, get_adapter, get_fallback
from normalise import detect_tags, now_nz_iso, sanity_check_dates

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
SEED_PATH = DATA_DIR / "seed.json"


def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _apply_missing_tags(entry: dict) -> dict:
    """Fill in film["tags"] via normalise.detect_tags() for any film an
    adapter didn't already tag."""
    for film in entry.get("films", []):
        if not film.get("tags"):
            film["tags"] = detect_tags(film.get("title", ""), film.get("note"))
    return entry


def _fetch_one(cinema: dict) -> tuple[dict | None, str | None, str | None]:
    """Try the primary adapter, then the Flicks fallback.

    Returns (result, error, fallback_reason):
      (result, None, None)    primary adapter succeeded
      (result, None, reason)  Flicks fallback succeeded; reason = primary error
      (None, error, None)     both failed (or no fallback available)
    """
    cinema_id = cinema["id"]
    primary_error = "unknown error"
    try:
        fetch = get_adapter(cinema_id)
        result = fetch(cinema)
        result["films"] = sanity_check_dates(result.get("films", []))
        return result, None, None
    except Exception as exc:  # noqa: BLE001 - any adapter failure counts
        primary_error = f"{type(exc).__name__}: {exc}"

    try:
        fallback = get_fallback(cinema_id)
    except Exception as exc:  # noqa: BLE001
        return None, f"{primary_error}; fallback unavailable: {exc}", None

    if fallback is None:
        return None, primary_error, None

    try:
        result = fallback(cinema)
        result["films"] = sanity_check_dates(result.get("films", []))
        return result, None, primary_error
    except Exception as exc:  # noqa: BLE001
        return None, f"{primary_error}; fallback failed: {type(exc).__name__}: {exc}", None


def build_snapshot(
    cinemas: list[dict] = CINEMAS,
    latest_path: Path = LATEST_PATH,
    seed_path: Path = SEED_PATH,
) -> dict:
    """Fetch every cinema and return the data/latest.json-shaped dict.

    Pure w.r.t. the filesystem other than reading the previous latest.json
    and seed.json for fallback data - does not write anything.
    """
    previous = _load_json(latest_path)
    prev_cinemas = previous.get("cinemas", {})
    seed = _load_json(seed_path)
    seed_cinemas = seed.get("cinemas", {})

    cinemas_out: dict = {}
    errors: dict = {}
    fallbacks: dict = {}

    for cinema in cinemas:
        cid = cinema["id"]
        result, err, fallback_reason = _fetch_one(cinema)

        if result is not None:
            cinemas_out[cid] = _apply_missing_tags(result)
            if fallback_reason:
                fallbacks[cid] = fallback_reason
                print(f"[ok*]  {cid}: {len(result.get('films', []))} films "
                      f"via flicks fallback (primary: {fallback_reason})")
            else:
                print(f"[ok]   {cid}: {len(result.get('films', []))} films")
            continue

        errors[cid] = err or "unknown error"
        if cid in prev_cinemas:
            cinemas_out[cid] = prev_cinemas[cid]
            print(f"[FAIL] {cid}: {err} -- kept previous listing")
        elif cid in seed_cinemas:
            cinemas_out[cid] = seed_cinemas[cid]
            print(f"[FAIL] {cid}: {err} -- fell back to seed data")
        else:
            print(f"[FAIL] {cid}: {err} -- no previous or seed data available")

    return {
        "fetchedAt": now_nz_iso(),
        "errors": errors,
        "fallbacks": fallbacks,  # primary adapter failed, Flicks covered it
        "cinemas": cinemas_out,
    }


def main() -> int:
    snapshot = build_snapshot()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Poster/rating resolution is progressive enhancement - a TMDB outage or
    # API change must never break the scrape itself.
    try:
        titles = sorted({
            film["title"]
            for entry in snapshot["cinemas"].values()
            for film in entry.get("films", [])
        })
        tmdb.resolve_all(titles)
    except Exception as exc:  # noqa: BLE001
        print(f"[tmdb] warning: poster/rating resolution failed: {type(exc).__name__}: {exc}")

    # Same deal for the Letterboxd watchlist - refresh_cache() already keeps
    # the previous cache on failure, this guards against anything above it.
    try:
        letterboxd.refresh_cache()
    except Exception as exc:  # noqa: BLE001
        print(f"[letterboxd] warning: watchlist refresh failed: {type(exc).__name__}: {exc}")

    total = len(CINEMAS)
    failed = len(snapshot["errors"])
    via_flicks = len(snapshot["fallbacks"])
    print(f"done: {total - failed}/{total} cinemas fetched live "
          f"({via_flicks} via flicks fallback), {failed} kept stale data")
    return 0  # per-cinema failure must never fail the job


if __name__ == "__main__":
    sys.exit(main())
