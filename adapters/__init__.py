"""Adapter registry: cinemaId -> callable.

Adapter contract
----------------
Each adapter module exposes  fetch(cinema: dict) -> dict  where `cinema` is an
entry from CINEMAS below, and the return value is:

    {
      "cinemaId": str,
      "films": [
        {
          "title": str,
          "sessions": [{"date": "YYYY-MM-DD", "time": "6:15pm"}],  # may be []
          "note": str | None,        # optional free text ("opens Thu 16 Jul")
          "tags": ["kids"|"retro"|"event", ...],  # optional, may be []
        },
        ...
      ],
      "fetchedAt": str,   # ISO-8601 with offset, Pacific/Auckland
      "sourceUrl": str,   # page a human should click through to
    }

Adapters raise AdapterError (or any Exception) on failure; scrape.py catches
per cinema and keeps the previous good entry. Adapters must run their session
dates through normalise.sanity_check_dates() before returning — Veezi in
particular can serve stale cached HTML (see README learnings).
"""


class AdapterError(Exception):
    """Raised when an adapter cannot produce trustworthy listings."""


CINEMAS = [
    # tier 1 = close by, expanded; tier 2 = "further afield", collapsed
    {"id": "academy", "name": "Academy Cinemas", "area": "CBD", "tier": 1,
     "url": "https://www.academycinemas.co.nz",
     "flicksSlug": "academy-cinemas"},
    {"id": "hollywood", "name": "Hollywood Avondale", "area": "Avondale", "tier": 1,
     "url": "https://www.hollywoodavondale.nz",
     "veeziToken": "fpnccxy3ma159g7z8a3e95asy8",
     "flicksSlug": "the-hollywood-cinema"},
    {"id": "capitol", "name": "The Capitol", "area": "Balmoral", "tier": 1,
     "url": "https://www.thecapitol.co.nz",
     "veeziToken": None,  # discover from booking links on thecapitol.co.nz
     "flicksSlug": "capitol-cinema"},
    {"id": "rialto", "name": "Rialto Newmarket", "area": "Newmarket", "tier": 1,
     "url": "https://www.rialto.co.nz",
     "flicksSlug": "rialto-cinemas-newmarket"},
    {"id": "silky-orakei", "name": "Silky Otter Ōrākei", "area": "Ōrākei", "tier": 1,
     "url": "https://www.silkyotter.co.nz",
     "flicksSlug": "silky-otter-cinemas-orakei"},
    {"id": "silky-ponsonby", "name": "Silky Otter Ponsonby", "area": "Ponsonby", "tier": 1,
     "url": "https://www.silkyotter.co.nz",
     "flicksSlug": "silky-otter-cinemas-ponsonby"},
    {"id": "event-queen", "name": "Event Queen St", "area": "CBD", "tier": 1,
     "url": "https://www.eventcinemas.co.nz/cinemas/queenstreet",
     "flicksSlug": "event-cinemas-queen-street"},
    {"id": "event-newmarket", "name": "Event Newmarket", "area": "Newmarket", "tier": 1,
     "url": "https://www.eventcinemas.co.nz/cinemas/newmarket",
     "flicksSlug": "event-cinemas-newmarket"},
    {"id": "event-stlukes", "name": "Event St Lukes", "area": "St Lukes", "tier": 1,
     "url": "https://www.eventcinemas.co.nz/cinemas/stlukes",
     "flicksSlug": "event-cinemas-st-lukes"},
    {"id": "reading-newlynn", "name": "Reading Cinemas New Lynn", "area": "New Lynn", "tier": 1,
     "url": "https://www.readingcinemas.co.nz",
     "flicksSlug": "reading-cinemas-lynnmall"},
    {"id": "bridgeway", "name": "The Bridgeway", "area": "Northcote Pt", "tier": 2,
     "url": "https://www.bridgeway.co.nz",
     "veeziToken": None,  # discover from booking links on bridgeway.co.nz
     "flicksSlug": "bridgeway-cinema"},
]

CINEMAS_BY_ID = {c["id"]: c for c in CINEMAS}

# cinemaId -> (module name, so one broken module never takes down the rest)
_ROUTES = {
    "academy": "flicks",
    "hollywood": "veezi",
    "capitol": "veezi",
    "rialto": "evt",
    "silky-orakei": "silky",
    "silky-ponsonby": "silky",
    "event-queen": "evt",
    "event-newmarket": "evt",
    "event-stlukes": "evt",
    "reading-newlynn": "reading",
    "bridgeway": "veezi",
}


def get_adapter(cinema_id):
    """Return the fetch callable for a cinema. Imports lazily so a syntax
    error in one adapter module only breaks its own cinemas."""
    import importlib
    module = importlib.import_module(f"adapters.{_ROUTES[cinema_id]}")
    return module.fetch


def get_fallback(cinema_id):
    """Flicks fallback for any cinema with a flicksSlug (except those already
    routed to flicks)."""
    if _ROUTES[cinema_id] == "flicks":
        return None
    if not CINEMAS_BY_ID[cinema_id].get("flicksSlug"):
        return None
    import importlib
    module = importlib.import_module("adapters.flicks")
    return module.fetch
