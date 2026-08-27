# Top 10 → Seerr

Streaming Top 10 charts for six platforms across 94 countries, cross-referenced
against your Overseerr/Jellyseerr library so missing titles can be requested in
one click. Serves a web UI and a JSON API consumed by a companion Android TV app.

Platforms: **Netflix, Amazon Prime Video, Disney+, HBO Max, Apple TV+, Paramount+**

## How it works

**The refresher writes; the web layer only reads.**

A scheduled job fetches every platform/country pair into SQLite. HTTP requests
are pure database reads — a user request never touches the internet and cannot
hang. Stale data is always served in preference to nothing.

```
refresh.py ──> SQLite (/app/data/top10.db) ──> app.py ──> /api/home
   │
   ├── Netflix        official Tudum weekly feed (true ranks)
   ├── Others         JustWatch GraphQL (~0.13s, no auth)
   ├── Fallback       TMDB discover via Overseerr + recency filter
   └── Enrichment     Overseerr by tmdbId → poster, overview, library status
```

A full refresh takes about **70 seconds** and writes ~7000 rows.

### Data sources

| Source | Used for | Notes |
|---|---|---|
| [Netflix Tudum](https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv) | Netflix only | True weekly ranks 1-10, 94 countries, updated Tuesdays. ~32 MB, downloaded once per refresh. `cumulative_weeks_in_top_10 == 1` drives the "new entry" flag. |
| JustWatch GraphQL | All other platforms | `POST https://apis.justwatch.com/graphql`, no auth or API key. Returns `tmdbId` inline. |
| TMDB discover (via Overseerr) | Last-resort fallback | With a 120-day recency filter — raw popularity alone surfaces catalog evergreens and makes a poor Top 10. |

> **JustWatch package codes are per country.** HBO Max is `mxx` in France but can
> differ elsewhere. They are resolved at refresh time via a `packages(country:)`
> query and cached — never hardcode them globally. A platform being unavailable
> in a country is normal and is recorded as `empty`, not an error.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/home?country=<slug>` | **Primary.** Every platform, movies + shows, in one call (~0.18s) |
| `GET /api/countries` | 94 countries, pre-ordered |
| `GET /api/health` | Row counts, per-source breakdown, last refresh time and stats |
| `POST /api/sync` | Request titles into Overseerr |
| `GET /api/history` | Sync history |
| `GET /api/proxy-image?url=` | Poster proxy |
| `POST /api/fetch` | Legacy single-platform endpoint, kept for compatibility |

`/api/home` response:

```json
{
  "country": "france",
  "generatedAt": "2026-08-27T14:00:00Z",
  "platforms": [{
    "platform": "netflix",
    "displayName": "Netflix",
    "source": "netflix-official",
    "fetchedAt": "2026-08-27T02:00:00Z",
    "ageHours": 12.5,
    "movies": [{
      "rank": 1, "title": "The Last House", "type": "movie", "tmdbId": 1284041,
      "posterUrl": "/api/proxy-image?url=...", "overview": "...",
      "genre": "Horror", "year": "2026", "status": "Available",
      "isRequestable": false, "isNewEntry": true, "weeksInTop10": 1,
      "trailerKey": "abc123", "watchProviders": []
    }],
    "shows": []
  }]
}
```

`posterUrl` is relative — prefix it with the base URL. `status` is
`Available` / `Processing` / `Ready`.

**Country order is deliberate:** Norway (default), United Kingdom, United States,
then alphabetical. `/api/countries` returns them already ordered — do not re-sort
in the client.

## Running it

```yaml
services:
  top10-seerr:
    image: ghcr.io/therealodineye/top10-seerr:latest
    container_name: top10-seerr
    ports:
      - "8564:5000"
    volumes:
      - ./config/top10-seerr/.env:/app/.env
      - ./config/top10-seerr/data:/app/data   # SQLite lives here — must persist
    restart: unless-stopped
```

Configure Overseerr in `.env` (see `.env.example`): `SEERR_URL`, `SEERR_API_KEY`,
`SEERR_EMAIL`, `SEERR_PASSWORD`.

Schedule the refresher — every 6 hours is plenty:

```
0 */6 * * * /usr/bin/docker exec top10-seerr python3 /app/refresh.py >> /path/to/logs/refresh.log 2>&1
```

Run it once manually after first start, or the database will be empty:

```
docker exec top10-seerr python3 /app/refresh.py
```

## Notes

* **The `data` volume must persist.** The database is the cache. Without it every
  restart starts empty.
* **Build on `main`.** `.github/workflows/docker-publish.yml` publishes to GHCR
  and tags `latest` from `main`. If that gating is wrong, CI goes green while the
  server keeps pulling a stale image.
* `scraper.py` is a metadata-only compatibility shim and performs **no** network
  access. FlixPatrol scraping and the FlareSolverr dependency were removed
  entirely — see below.
* If Netflix rows lose their posters, check that `seerr.search_media()` still
  returns `posterPath`. `enrich_metadata()` re-fetches any row missing a poster
  even inside its TTL, so the condition self-heals once the source is fixed.

## Why it was rewritten

Charts used to be scraped from FlixPatrol. FlixPatrol added Cloudflare Turnstile,
so every chart fetch had to solve a challenge through FlareSolverr *inside the
user's request*:

* 13-140s per view, against a 60s timeout at both the reverse proxy and the client
* ~18% hard failure rate across a full 504-combination run — worst on Amazon
  Prime and HBO Max
* a full prewarm took 165 minutes, and cached nothing usable: the cache was an
  in-process dict, gunicorn runs two workers, and the prewarm ran in a third
  process that exited

The rewrite removed Cloudflare from the request path entirely. The same view now
resolves in **~0.18s with zero errors**, and a full refresh takes 70 seconds.
