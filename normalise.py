"""Title normalisation, tag detection, and date sanity checks.

Shared by all adapters and by render.py's by-film aggregation.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from adapters import AdapterError

NZ = ZoneInfo("Pacific/Auckland")

# How far ahead a session date may plausibly be. NZ programmes are published
# roughly a week out; NZIFF/advance screenings can be ~a month out.
DATE_WINDOW_DAYS = 45


class StaleDataError(AdapterError):
    """All scraped session dates fall outside the plausible window —
    the source served stale cached content (see README learnings: Veezi
    once returned a months-old page)."""


def today_nz() -> date:
    return datetime.now(NZ).date()


def now_nz_iso() -> str:
    return datetime.now(NZ).isoformat(timespec="seconds")


def dedupe_key(title: str) -> str:
    """Case/punctuation-insensitive key for cross-cinema title matching."""
    key = title.lower()
    key = re.sub(r"\((?:19|20)\d{2}\)", " ", key)  # "(2026)" re-release years
    key = re.sub(r"[^a-z0-9 ]", "", key)
    return re.sub(r"\s+", " ", key).strip()


_TAG_PATTERNS = {
    "kids": re.compile(r"kids club|school holiday|kids['’]? session", re.I),
    "retro": re.compile(r"\bretro\b|classic screening|throwback", re.I),
    "event": re.compile(
        r"q\s?&\s?a|event screening|concert|listening session|marathon|"
        r"advance screening|nziff|sing.?along|film festival", re.I),
}


def detect_tags(title: str, note: str | None = None) -> list[str]:
    haystack = f"{title} {note or ''}"
    return [tag for tag, pat in _TAG_PATTERNS.items() if pat.search(haystack)]


def sanity_check_dates(films: list[dict], today: date | None = None) -> list[dict]:
    """Drop sessions outside [today, today + DATE_WINDOW_DAYS].

    Returns the films list with out-of-window sessions removed. If the scrape
    contained sessions but *every* date was out of window, the source is
    serving stale content — raise StaleDataError so scrape.py keeps the
    previous good entry instead.
    """
    today = today or today_nz()
    horizon = today + timedelta(days=DATE_WINDOW_DAYS)
    had_sessions = False
    kept_any = False
    for f in films:
        kept = []
        for s in f.get("sessions", []):
            had_sessions = True
            try:
                d = date.fromisoformat(s["date"])
            except (KeyError, ValueError):
                continue
            if today <= d <= horizon:
                kept.append(s)
        if kept:
            kept_any = True
        f["sessions"] = kept
    if had_sessions and not kept_any:
        raise StaleDataError(
            f"all session dates outside {today}..{horizon} — source looks stale")
    return films
