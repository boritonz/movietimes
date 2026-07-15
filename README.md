# MovieTimes

A static aggregator for Auckland cinema listings. Every night a GitHub Action
scrapes 11 cinemas across the city and republishes a single mobile-first page
at the GitHub Pages URL for this repo — no app, no server, no database.

## The 11 cinemas

| Cinema | Area | Tier |
| --- | --- | --- |
| Academy Cinemas | CBD | 1 |
| Hollywood Avondale | Avondale | 1 |
| The Capitol | Balmoral | 1 |
| Rialto Newmarket | Newmarket | 1 |
| Silky Otter Ōrākei | Ōrākei | 1 |
| Silky Otter Ponsonby | Ponsonby | 1 |
| Event Queen St | CBD | 1 |
| Event Newmarket | Newmarket | 1 |
| Event St Lukes | St Lukes | 1 |
| Reading Cinemas New Lynn | New Lynn | 1 |
| The Bridgeway | Northcote Pt | 2 ("further afield") |

Tier 1 cinemas are always shown expanded; tier 2 (currently just Bridgeway)
is shown collapsed under a "Further afield" divider in both views, since
it's a longer trip from most of the city.

## Architecture

```
adapters/           one module per booking platform (veezi, silky, evt,
                     reading, flicks) + the cinema registry (CINEMAS) and
                     the adapter contract (adapters/__init__.py)
normalise.py         title de-duplication, tag detection, session date
                     sanity checking - shared by adapters and render.py
scrape.py            orchestrator: calls each cinema's adapter, falls back
                     to Flicks, then to the last good snapshot, then to
                     data/seed.json; writes data/latest.json
render.py            reads data/latest.json (or data/seed.json), builds
                     the by-film and by-cinema views, and writes the fully
                     static docs/index.html via a Jinja2 template
templates/index.html.j2   the page template - inline CSS, vanilla JS,
                     no build step
data/seed.json        a verified, hand-checked snapshot used whenever a
                     cinema has never been scraped successfully
data/latest.json      the live snapshot scrape.py produces (generated,
                     not committed by hand)
docs/index.html       the published page (GitHub Pages serves straight
                     from here)
```

Each adapter module exposes a single `fetch(cinema) -> dict` function; see
the docstring at the top of `adapters/__init__.py` for the exact return
shape. `scrape.py` never imports an adapter module directly - it always
goes through `get_adapter()` / `get_fallback()` so one broken adapter can
never take the others down with it.

## Running locally

```bash
pip install -r requirements.txt

python scrape.py     # writes data/latest.json (falls back per-cinema on failure)
python render.py     # writes docs/index.html from data/latest.json (or data/seed.json)
python -m pytest tests/test_core.py -q   # unit tests, no network
```

Open `docs/index.html` directly in a browser - it's a single self-contained
file with no server required.

## GitHub Pages setup

1. Push this repo to GitHub.
2. Settings → Pages → Build and deployment → **Deploy from a branch**.
3. Branch: `main`, folder: `/docs`. Save.
4. The page will be live at `https://<user>.github.io/<repo>/` within a
   couple of minutes, and updates automatically whenever `docs/index.html`
   changes on `main`.

## Keeping it fresh

`.github/workflows/refresh.yml` runs `scrape.py` then `render.py` every
night (cron `0 19 * * *` UTC, i.e. early morning NZ time) and commits
`data/latest.json` + `docs/index.html` if anything changed.

To refresh on demand from your phone: open the **GitHub mobile app** →
this repo → **Actions** → **refresh listings** → **Run workflow**
(`workflow_dispatch`). It runs the same two scripts and pushes the result
straight to `main`, so the Pages site updates a minute or two later.

## Learnings

- **Veezi can serve stale cached HTML.** We've seen it return a page whose
  session dates were weeks in the past, with no error status - just an old
  cached response. `normalise.sanity_check_dates()` drops any session
  outside a plausible date window and raises `StaleDataError` if *every*
  session on the page falls outside that window, which `scrape.py` treats
  as a failure and falls back accordingly, rather than publishing dates
  nobody should trust.
- **Flicks per-cinema pages SSR inconsistently.** Some Flicks cinema pages
  render fully server-side, others need JS to populate the film list, and
  some return a 404-looking shell for cinemas that do have a working page
  through their own site. Because of that, Flicks is used as the
  **fallback** adapter, not the primary source, for any cinema whose own
  booking platform (Veezi, Silky, Event/EVT, Reading) is working.
- **Film lists churn weekly, session times churn daily.** New Zealand
  cinema programmes turn over on Thursdays, so film titles, notes and tags
  are comparatively stable within a week. Session times are re-published
  daily (sometimes hourly, for last-minute schedule changes), which is why
  the site refreshes nightly rather than weekly, and why the by-film view
  defaults to *today's* sessions with a "Coming up" toggle rather than
  trying to show a full week at once.
