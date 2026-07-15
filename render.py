"""Renders data/latest.json (falling back to data/seed.json) into a single
static, self-contained page at docs/index.html.

No build step: inline CSS + vanilla JS, one Jinja2 template. Safe to open
docs/index.html straight off disk or serve it from GitHub Pages.
"""
from __future__ import annotations

import json
from datetime import date, datetime
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


def session_views(sessions: list[dict], today: date) -> tuple[list[str], list[dict]]:
    """Split a film's sessions into (today's times, upcoming grouped by date).

    today_times: sorted list of "6:15pm"-style strings for `today`.
    upcoming: list of {"date": "YYYY-MM-DD", "label": "Thu 16 Jul", "times": [...]}
              sorted by date, for sessions strictly after `today`.
    """
    today_iso = today.isoformat()
    today_times = sorted(s["time"] for s in sessions if s.get("date") == today_iso)

    by_date: dict[str, list[str]] = {}
    for s in sessions:
        d = s.get("date")
        if d and d > today_iso:
            by_date.setdefault(d, []).append(s["time"])

    upcoming = [
        {
            "date": d,
            "label": format_nz_date(date.fromisoformat(d)),
            "times": sorted(times),
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
                "note": film.get("note"),
                "todayTimes": today_times,
                "upcoming": upcoming,
            }
            bucket = film_buckets.setdefault(
                key, {"title": film["title"], "tags": set(), "venues": []}
            )
            bucket["tags"].update(film.get("tags") or [])
            bucket["venues"].append(venue)

    films = []
    for bucket in film_buckets.values():
        tier1_venues = [v for v in bucket["venues"] if v["tier"] == 1]
        tier2_venues = [v for v in bucket["venues"] if v["tier"] != 1]
        films.append({
            "title": bucket["title"],
            "tags": sorted(bucket["tags"]),
            "tier1Venues": tier1_venues,
            "tier2Venues": tier2_venues,
            "venueCount": len(bucket["venues"]),
        })
    films.sort(key=lambda f: (-f["venueCount"], f["title"].lower()))

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
                "note": film.get("note"),
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
        "stale_lines": stale_lines,
        "films": films,
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
