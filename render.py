"""Renders data/latest.json (falling back to data/seed.json) into a single
static, self-contained page at docs/index.html.

No build step: inline CSS + vanilla JS, one Jinja2 template. Safe to open
docs/index.html straight off disk or serve it from GitHub Pages.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from adapters import CINEMAS
from normalise import dedupe_key, today_nz

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
SEED_PATH = DATA_DIR / "seed.json"
TEMPLATES_DIR = ROOT / "templates"
DOCS_DIR = ROOT / "docs"

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_nz_date(d: date) -> str:
    """'Thu 16 Jul' - no locale dependency, always English."""
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def format_nz_date_long(d: date) -> str:
    """'16 Jul 2026'."""
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def format_nz_datetime(iso_str: str | None) -> str:
    """'Wed 15 Jul 2026, 12:30pm'."""
    if not iso_str:
        return "unknown"
    dt = datetime.fromisoformat(iso_str)
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{format_nz_date(dt.date())} {dt.year}, {hour12}:{dt.minute:02d}{ampm}"


def parse_time_to_mins(t: str) -> int:
    """Parse a '6:15pm'-style time string into minutes since midnight.

    Used to emit machine-readable data-mins attributes so the page can hide
    past sessions client-side (the page itself is only rendered once daily).

    '12:00pm' (noon) == 720. '12:15am' (just after midnight) == 15.
    """
    s = t.strip().lower()
    ampm = s[-2:]
    if ampm not in ("am", "pm"):
        raise ValueError(f"unrecognised time format: {t!r}")
    hour_str, minute_str = s[:-2].split(":")
    hour = int(hour_str)
    minute = int(minute_str)
    if ampm == "am":
        if hour == 12:
            hour = 0
    else:
        if hour != 12:
            hour += 12
    return hour * 60 + minute


NOTE_MAX_CHARS = 60


def truncate_note(note: str | None, limit: int = NOTE_MAX_CHARS) -> str | None:
    """Notes render only up to `limit` chars — this is a decision tool, not a
    listings database. Longer notes get truncated with an ellipsis."""
    if not note:
        return None
    if len(note) <= limit:
        return note
    return note[:limit].rstrip() + "…"


# How far ahead "Coming up" renders. The full scraped horizon still lives in
# latest.json (see normalise.DATE_WINDOW_DAYS) - this just caps what's worth
# putting on a decision-grade page.
COMING_UP_WINDOW_DAYS = 2


def _time_entry(t: str) -> dict:
    return {"display": t, "mins": parse_time_to_mins(t)}


def session_views(sessions: list[dict], today: date) -> tuple[list[dict], list[dict]]:
    """Split a film's sessions into (today's times, upcoming grouped by date).

    today_times: sorted list of {"display": "6:15pm", "mins": 1095} for `today`.
    upcoming: list of {"date": "YYYY-MM-DD", "label": "Thu 16 Jul", "times": [...]}
              sorted by date, for sessions strictly after `today` and within
              COMING_UP_WINDOW_DAYS (decision-grade cut - see render.py docstring
              item 5; the rest of the scraped horizon stays in latest.json only).
    """
    today_iso = today.isoformat()
    horizon_iso = (today + timedelta(days=COMING_UP_WINDOW_DAYS)).isoformat()

    today_times = sorted(
        (_time_entry(s["time"]) for s in sessions if s.get("date") == today_iso),
        key=lambda t: t["mins"],
    )

    by_date: dict[str, list[dict]] = {}
    for s in sessions:
        d = s.get("date")
        if d and today_iso < d <= horizon_iso:
            by_date.setdefault(d, []).append(_time_entry(s["time"]))

    upcoming = [
        {
            "date": d,
            "label": format_nz_date(date.fromisoformat(d)),
            "times": sorted(times, key=lambda t: t["mins"]),
        }
        for d, times in sorted(by_date.items())
    ]
    return today_times, upcoming


def load_data(latest_path: Path = LATEST_PATH, seed_path: Path = SEED_PATH) -> dict:
    if latest_path.exists():
        with open(latest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    with open(seed_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_context(data: dict, today: date, cinemas: list[dict] = CINEMAS) -> dict:
    cinemas_data = data.get("cinemas", {})
    errors = data.get("errors", {})

    # --- header staleness lines ---------------------------------------
    stale_lines = []
    for cinema in cinemas:
        if cinema["id"] not in errors:
            continue
        entry = cinemas_data.get(cinema["id"])
        stale_date = "an earlier date"
        if entry and entry.get("fetchedAt"):
            try:
                stale_date = format_nz_date_long(datetime.fromisoformat(entry["fetchedAt"]).date())
            except ValueError:
                pass
        stale_lines.append(f"{cinema['name']}: showing older listings from {stale_date}")

    fetched_dt = None
    if data.get("fetchedAt"):
        try:
            fetched_dt = datetime.fromisoformat(data["fetchedAt"])
        except ValueError:
            fetched_dt = None
    fetched_date_short = format_nz_date(fetched_dt.date()) if fetched_dt else "an earlier date"

    # --- by-film aggregation -------------------------------------------
    film_buckets: dict[str, dict] = {}
    any_upcoming = False

    for cinema in cinemas:
        entry = cinemas_data.get(cinema["id"])
        if not entry:
            continue
        source_url = entry.get("sourceUrl") or cinema["url"]
        for film in entry.get("films", []):
            key = dedupe_key(film["title"])
            today_times, upcoming = session_views(film.get("sessions") or [], today)
            if upcoming:
                any_upcoming = True
            venue = {
                "cinemaId": cinema["id"],
                "cinemaName": cinema["name"],
                "sourceUrl": source_url,
                "tier": cinema["tier"],
                "note": truncate_note(film.get("note")),
                "todayTimes": today_times,
                "upcoming": upcoming,
            }
            bucket = film_buckets.setdefault(
                key, {"title": film["title"], "tags": set(), "venues": []}
            )
            bucket["tags"].update(film.get("tags") or [])
            bucket["venues"].append(venue)

    # Films with zero rendered sessions anywhere (titles-only cinemas like
    # Academy/Silky, or films whose only sessions fall outside the
    # COMING_UP_WINDOW_DAYS cut) don't earn a full card - demote them to a
    # compact link list (item 4/5: decision tool, not a listings database).
    films = []
    also_showing = []
    for bucket in film_buckets.values():
        tier1_venues = [v for v in bucket["venues"] if v["tier"] == 1]
        tier2_venues = [v for v in bucket["venues"] if v["tier"] != 1]
        venue_count = len(bucket["venues"])

        today_entries = [t for v in bucket["venues"] for t in v["todayTimes"]]
        upcoming_entries = [
            (u["date"], t)
            for v in bucket["venues"]
            for u in v["upcoming"]
            for t in u["times"]
        ]

        if not today_entries and not upcoming_entries:
            venue_links = sorted(
                {(v["cinemaName"], v["sourceUrl"]) for v in bucket["venues"]},
                key=lambda x: x[0].lower(),
            )
            also_showing.append({
                "title": bucket["title"],
                "venues": venue_links,
                "kids": "kids" in bucket["tags"],
            })
            continue

        plural = "s" if venue_count != 1 else ""
        if today_entries:
            earliest = min(today_entries, key=lambda t: t["mins"])
            summary = f"{venue_count} cinema{plural} · from {earliest['display']}"
            sort_key = (0, earliest["mins"], -venue_count, bucket["title"].lower())
        else:
            earliest_date, earliest_t = min(upcoming_entries, key=lambda p: (p[0], p[1]["mins"]))
            label = format_nz_date(date.fromisoformat(earliest_date))
            summary = f"{venue_count} cinema{plural} · from {label}"
            sort_key = (1, earliest_date, earliest_t["mins"], -venue_count, bucket["title"].lower())

        films.append({
            "title": bucket["title"],
            "tags": sorted(bucket["tags"]),
            "tier1Venues": tier1_venues,
            "tier2Venues": tier2_venues,
            "venueCount": venue_count,
            "hasToday": bool(today_entries),
            "summary": summary,
            "_sortKey": sort_key,
        })

    films.sort(key=lambda f: f["_sortKey"])
    for f in films:
        del f["_sortKey"]
    also_showing.sort(key=lambda f: f["title"].lower())

    # --- by-cinema view --------------------------------------------------
    cinema_entries: dict[str, dict] = {}
    for cinema in cinemas:
        entry = cinemas_data.get(cinema["id"])
        if not entry:
            continue
        films_out = []
        for film in entry.get("films", []):
            today_times, upcoming = session_views(film.get("sessions") or [], today)
            if upcoming:
                any_upcoming = True
            films_out.append({
                "title": film["title"],
                "note": truncate_note(film.get("note")),
                "tags": film.get("tags") or [],
                "todayTimes": today_times,
                "upcoming": upcoming,
            })
        cinema_entries[cinema["id"]] = {
            "sourceUrl": entry.get("sourceUrl") or cinema["url"],
            "info": entry.get("info"),
            "films": films_out,
        }

    tier1_cinemas = [c for c in cinemas if c["tier"] == 1 and c["id"] in cinema_entries]
    tier2_cinemas = [c for c in cinemas if c["tier"] != 1 and c["id"] in cinema_entries]

    return {
        "fetched_at_display": format_nz_datetime(data.get("fetchedAt")),
        "fetched_date_short": fetched_date_short,
        "today_iso": today.isoformat(),
        "stale_lines": stale_lines,
        "films": films,
        "also_showing": also_showing,
        "any_upcoming": any_upcoming,
        "cinema_entries": cinema_entries,
        "tier1_cinemas": tier1_cinemas,
        "tier2_cinemas": tier2_cinemas,
    }


def render_html(context: dict, templates_dir: Path = TEMPLATES_DIR) -> str:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("index.html.j2")
    return template.render(**context)


def main(
    latest_path: Path = LATEST_PATH,
    seed_path: Path = SEED_PATH,
    docs_dir: Path = DOCS_DIR,
    templates_dir: Path = TEMPLATES_DIR,
) -> int:
    data = load_data(latest_path, seed_path)
    context = build_context(data, today_nz())
    html = render_html(context, templates_dir)

    docs_dir.mkdir(parents=True, exist_ok=True)
    out_path = docs_dir / "index.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
