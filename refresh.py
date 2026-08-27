"""
Refresher job for top10-seerr.

Populates SQLite (db.py) for every (platform, country) pair from fast,
reliable sources -- never scraping FlixPatrol/Cloudflare. This is the ONLY
writer to the database; app.py only reads.

Source priority per platform:
  - netflix        -> Netflix official TSV (authoritative)
  - everything else -> JustWatch GraphQL, falling back to TMDB discover
                        (via Overseerr) if JustWatch has no package code
                        for that country, or returns nothing.

Idempotent & resumable: each (platform, country, media_type) triple is
processed independently and committed independently. A failure never
deletes existing last-known-good rows (db.upsert_chart_rows is a no-op
when given an empty list).

Run manually:   python3 refresh.py
Run via cron:   docker exec top10-seerr python3 /app/refresh.py
"""
import os
import sys
import csv
import io
import json
import time
import datetime
import traceback

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

import db
import seerr

JUSTWATCH_URL = "https://apis.justwatch.com/graphql"
JUSTWATCH_UA = "Mozilla/5.0 Chrome/148.0.0.0"
NETFLIX_TSV_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"

PACKAGE_CACHE_TTL = 7 * 86400       # re-check JustWatch/TMDB provider codes weekly
TITLE_META_TTL = 24 * 3600          # re-enrich metadata daily at most

PLATFORMS = {
    "netflix": "Netflix",
    "amazon-prime": "Amazon Prime Video",
    "disney": "Disney Plus",
    "hbo-max": "HBO Max",
    "apple-tv": "Apple TV",
    "paramount-plus": "Paramount Plus",
}

# Matching rules for both JustWatch `packages(country,platform:WEB)` clearNames
# AND TMDB / Overseerr /api/v1/watchproviders/{movies,tv} provider names (same
# naming convention -- TMDB sources this data from JustWatch too).
#
# Some countries expose a platform as several *tiered* / bundled variants
# instead of one plain package, e.g. in the US JustWatch has no plain
# "Paramount Plus" package at all -- only "Paramount Plus Premium" and
# "Paramount Plus Essential". A naive exact-match against "paramount plus"
# misses those entirely and wrongly reports the platform as unavailable.
# So: match by PREFIX (normalized name starts with one of `prefixes`), then
# reject anything containing an `exclude` substring (channel/store bundles,
# which are a different product, e.g. "Apple TV Store" or "HBO Max Amazon
# Channel"), then among the remaining candidates prefer names containing a
# `prefer` keyword (e.g. "premium"), else the shortest (most canonical) name.
MATCH_RULES = {
    "netflix": {"prefixes": ["netflix"], "exclude": ["kids"], "prefer": []},
    "amazon-prime": {"prefixes": ["amazon prime video"], "exclude": ["channel", "store"], "prefer": []},
    "disney": {"prefixes": ["disney plus", "disney+"], "exclude": ["channel", "store", "hotstar"], "prefer": []},
    "hbo-max": {"prefixes": ["hbo max", "max"], "exclude": ["channel", "store"], "prefer": []},
    "apple-tv": {"prefixes": ["apple tv"], "exclude": ["channel", "store", "aggregator"], "prefer": ["plus", "+"]},
    "paramount-plus": {"prefixes": ["paramount plus", "paramount+"], "exclude": ["channel", "store"], "prefer": ["premium"]},
}


def match_provider_name(platform, names_with_ids):
    """
    names_with_ids: list of (name, id_or_shortcode) tuples.
    Returns the best-matching id_or_shortcode, or None.
    """
    rules = MATCH_RULES.get(platform)
    if not rules:
        return None
    candidates = []
    for name, ident in names_with_ids:
        norm = (name or "").strip().lower()
        if not any(norm.startswith(p) for p in rules["prefixes"]):
            continue
        if any(ex in norm for ex in rules["exclude"]):
            continue
        candidates.append((name, ident))
    if not candidates:
        return None
    for prefer_kw in rules["prefer"]:
        preferred = [c for c in candidates if prefer_kw in c[0].lower()]
        if preferred:
            candidates = preferred
            break
    candidates.sort(key=lambda c: len(c[0]))
    return candidates[0][1]

session = requests.Session()

STATS = {"ok": 0, "fallback": 0, "empty": 0, "error": 0}
DETAIL_LOG = []


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Country list
# ---------------------------------------------------------------------------

def load_countries():
    path = os.path.join(BASE_DIR, "countries.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Netflix official TSV
# ---------------------------------------------------------------------------

TSV_CACHE_PATH = os.path.join(db.DATA_DIR, "netflix_top10_cache.tsv")


def fetch_netflix_tsv():
    """
    Downloads the Netflix official TSV. Netflix's CDN has been observed to
    rate-limit/black-hole a source IP after a handful of requests in quick
    succession (during development, both direct `curl` and `requests` from
    this server started timing out entirely after ~4-5 fetches). To stay
    resilient to that in production (and to any transient outage), a
    successful download is cached to disk, and a failed download falls back
    to that last-good on-disk copy rather than skipping Netflix entirely for
    the cycle.
    """
    log("[NETFLIX] Downloading all-weeks-countries.tsv ...")
    start = time.time()
    last_err = None
    text = None
    for attempt in range(1, 4):
        try:
            res = session.get(NETFLIX_TSV_URL, timeout=60, allow_redirects=True)
            res.raise_for_status()
            text = res.text
            last_err = None
            break
        except Exception as e:
            last_err = e
            log(f"[NETFLIX] download attempt {attempt}/3 failed: {e}")
            time.sleep(3)

    if text is not None:
        log(f"[NETFLIX] Downloaded {len(text)} chars in {time.time()-start:.1f}s")
        try:
            with open(TSV_CACHE_PATH, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            log(f"[NETFLIX] warning: failed to write local TSV cache: {e}")
    else:
        log(f"[NETFLIX] live download failed after 3 attempts ({last_err}); trying on-disk cache...")
        if os.path.exists(TSV_CACHE_PATH):
            with open(TSV_CACHE_PATH, "r", encoding="utf-8") as f:
                text = f.read()
            age_h = (time.time() - os.path.getmtime(TSV_CACHE_PATH)) / 3600
            log(f"[NETFLIX] using cached TSV from disk, age={age_h:.1f}h")
        else:
            raise last_err

    reader = csv.reader(io.StringIO(text), delimiter="\t")
    rows = list(reader)
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}

    # Find the latest week per country (usually a single global latest week,
    # but computed defensively per-country in case of partial lag).
    latest_week = {}
    for r in rows[1:]:
        if len(r) <= idx["week"]:
            continue
        iso2 = r[idx["country_iso2"]]
        week = r[idx["week"]]
        if week > latest_week.get(iso2, ""):
            latest_week[iso2] = week

    by_country = {}
    for r in rows[1:]:
        if len(r) <= idx["cumulative_weeks_in_top_10"]:
            continue
        iso2 = r[idx["country_iso2"]]
        week = r[idx["week"]]
        if week != latest_week.get(iso2):
            continue
        category = r[idx["category"]]  # 'Films' or 'TV'
        try:
            rank = int(r[idx["weekly_rank"]])
        except ValueError:
            continue
        title = r[idx["show_title"]]
        try:
            weeks = int(r[idx["cumulative_weeks_in_top_10"]])
        except ValueError:
            weeks = None

        media_type = "movie" if category == "Films" else "tv"
        by_country.setdefault(iso2, {"movie": [], "tv": []})
        by_country[iso2][media_type].append(
            {"rank": rank, "title": title, "weeks_in_top10": weeks, "is_new_entry": 1 if weeks == 1 else 0}
        )

    for iso2 in by_country:
        for mt in ("movie", "tv"):
            by_country[iso2][mt].sort(key=lambda x: x["rank"])
            by_country[iso2][mt] = by_country[iso2][mt][:10]

    log(f"[NETFLIX] Parsed {len(by_country)} countries at week {max(latest_week.values()) if latest_week else 'n/a'}")
    return by_country


def resolve_netflix_tmdb(title, media_type):
    """Resolve a Netflix TSV title to a tmdbId, using the title_map cache so
    each unique title is only looked up once (ever) via Overseerr search."""
    cached = db.get_title_map(title, media_type)
    if cached is not None:
        return cached["tmdb_id"]

    tmdb_id = None
    try:
        res = seerr.search_media(title, media_type)
        if res and res.get("tmdbId"):
            tmdb_id = res["tmdbId"]
            # search_media already gives us full metadata -- cache it directly
            # so the enrichment pass doesn't need a second network round trip.
            db.upsert_title(tmdb_id, media_type, {
                "title": title,
                "overview": res.get("overview"),
                "poster_path": None,
                "genre": res.get("genre"),
                "year": res.get("year"),
                "status": res.get("status"),
                "is_requestable": not res.get("skip"),
                "trailer_key": res.get("trailerKey"),
                "watch_providers": res.get("watchProviders", []),
            })
    except Exception as e:
        log(f"[NETFLIX] title lookup failed for '{title}': {e}")

    db.set_title_map(title, media_type, tmdb_id)
    return tmdb_id


def process_netflix(country_slug, iso2, nf_data):
    entry = nf_data.get(iso2)
    if not entry:
        DETAIL_LOG.append(f"netflix/{country_slug}: no Netflix-official data for {iso2}")
        STATS["empty"] += 1
        return

    for media_type in ("movie", "tv"):
        chart_rows = []
        for item in entry[media_type]:
            tmdb_id = resolve_netflix_tmdb(item["title"], media_type)
            chart_rows.append({
                "rank": item["rank"],
                "tmdb_id": tmdb_id,
                "title": item["title"],
                "is_new_entry": item["is_new_entry"],
                "weeks_in_top10": item["weeks_in_top10"],
            })
        n = db.upsert_chart_rows("netflix", country_slug, media_type, chart_rows, source="netflix-official")
        if n:
            STATS["ok"] += 1
        else:
            STATS["empty"] += 1


# ---------------------------------------------------------------------------
# JustWatch
# ---------------------------------------------------------------------------

def jw_query(query, variables):
    payload = {"query": query, "variables": variables}
    headers = {"User-Agent": JUSTWATCH_UA, "Content-Type": "application/json"}
    res = session.post(JUSTWATCH_URL, json=payload, headers=headers, timeout=15)
    res.raise_for_status()
    data = res.json()
    if "errors" in data:
        raise RuntimeError(str(data["errors"]))
    return data["data"]


PACKAGES_QUERY = "query($c:Country!){packages(country:$c,platform:WEB){shortName clearName}}"
POPULAR_QUERY = (
    "query($c:Country!,$f:Int!,$p:[String!]){"
    "popularTitles(country:$c,first:$f,filter:{packages:$p}){"
    "edges{node{objectType content(country:$c,language:\"en\"){title externalIds{tmdbId}}}}}}"
)


def get_justwatch_code(iso2, platform):
    cached = db.get_package(iso2, platform)
    if cached and (time.time() - cached["fetched_at"]) < PACKAGE_CACHE_TTL:
        return cached["short_code"]

    try:
        data = jw_query(PACKAGES_QUERY, {"c": iso2})
        names_with_ids = [
            (pkg.get("clearName") or "", pkg.get("shortName"))
            for pkg in data.get("packages", [])
        ]
        code = match_provider_name(platform, names_with_ids)
        db.set_package(iso2, platform, code)
        return code
    except Exception as e:
        log(f"[JUSTWATCH] package lookup failed for {iso2}: {e}")
        # keep stale cached value (even if expired) rather than treating as missing
        return cached["short_code"] if cached else None


def jw_popular(iso2, short_code):
    data = jw_query(POPULAR_QUERY, {"c": iso2, "f": 40, "p": [short_code]})
    movies, shows = [], []
    for edge in data.get("popularTitles", {}).get("edges", []):
        node = edge.get("node", {})
        obj_type = node.get("objectType")
        content = node.get("content") or {}
        title = content.get("title")
        tmdb_raw = (content.get("externalIds") or {}).get("tmdbId")
        tmdb_id = int(tmdb_raw) if tmdb_raw else None
        target = movies if obj_type == "MOVIE" else shows
        if len(target) < 10:
            target.append({"title": title, "tmdb_id": tmdb_id})
    return movies, shows


# ---------------------------------------------------------------------------
# TMDB discover fallback (via Overseerr)
# ---------------------------------------------------------------------------

def get_tmdb_provider_id(iso2, platform):
    cached = db.get_tmdb_provider(iso2, platform)
    if cached and (time.time() - cached["fetched_at"]) < PACKAGE_CACHE_TTL:
        return cached["provider_id"]

    url, api_key, email, password, headers = seerr.get_seerr_config()
    if not url or not api_key:
        return cached["provider_id"] if cached else None

    try:
        res = session.get(f"{url}/api/v1/watchproviders/movies", params={"watchRegion": iso2}, headers=headers, timeout=15)
        res.raise_for_status()
        providers = res.json()
        names_with_ids = [(p.get("name") or "", p.get("id")) for p in providers]
        provider_id = match_provider_name(platform, names_with_ids)
        db.set_tmdb_provider(iso2, platform, provider_id)
        return provider_id
    except Exception as e:
        log(f"[TMDB-DISCOVER] provider lookup failed for {iso2}/{platform}: {e}")
        return cached["provider_id"] if cached else None


def tmdb_discover(media_type, iso2, provider_id):
    url, api_key, email, password, headers = seerr.get_seerr_config()
    if not url or not api_key:
        return []
    endpoint = f"{url}/api/v1/discover/{'movies' if media_type == 'movie' else 'tv'}"
    params = {
        "watchRegion": iso2,
        "watchProviders": provider_id,
        "sortBy": "popularity.desc",
    }
    cutoff = (datetime.date.today() - datetime.timedelta(days=120)).isoformat()
    if media_type == "movie":
        params["primaryReleaseDateGte"] = cutoff
    else:
        params["firstAirDateGte"] = cutoff

    res = session.get(endpoint, params=params, headers=headers, timeout=15)
    res.raise_for_status()
    data = res.json()
    out = []
    for r in data.get("results", [])[:10]:
        title = r.get("title") if media_type == "movie" else r.get("name")
        out.append({"title": title, "tmdb_id": r.get("id")})
    return out


def process_platform(country_slug, iso2, platform):
    try:
        code = get_justwatch_code(iso2, platform)
    except Exception as e:
        log(f"[{platform}/{country_slug}] package lookup crashed: {e}")
        code = None

    movies, shows = [], []
    source = None

    if code:
        try:
            movies, shows = jw_popular(iso2, code)
            if movies or shows:
                source = "justwatch"
        except Exception as e:
            log(f"[{platform}/{country_slug}] JustWatch popularTitles failed: {e}")

    if not movies and not shows:
        try:
            provider_id = get_tmdb_provider_id(iso2, platform)
            if provider_id:
                movies = tmdb_discover("movie", iso2, provider_id)
                shows = tmdb_discover("tv", iso2, provider_id)
                if movies or shows:
                    source = "tmdb-discover"
        except Exception as e:
            log(f"[{platform}/{country_slug}] TMDB discover fallback failed: {e}")

    if not source:
        DETAIL_LOG.append(f"{platform}/{country_slug}: NO DATA from any source (jw_code={code})")
        STATS["empty"] += 1
        return

    if source == "tmdb-discover":
        STATS["fallback"] += 1
    else:
        STATS["ok"] += 1

    for media_type, items in (("movie", movies), ("tv", shows)):
        rows = [
            {"rank": i + 1, "tmdb_id": it["tmdb_id"], "title": it["title"]}
            for i, it in enumerate(items)
        ]
        db.upsert_chart_rows(platform, country_slug, media_type, rows, source=source)


# ---------------------------------------------------------------------------
# Metadata enrichment
# ---------------------------------------------------------------------------

def enrich_metadata():
    conn = db.get_conn()
    pairs = conn.execute(
        "SELECT DISTINCT tmdb_id, media_type FROM charts WHERE tmdb_id IS NOT NULL"
    ).fetchall()
    log(f"[ENRICH] {len(pairs)} distinct (tmdbId, mediaType) pairs referenced in charts")

    done = 0
    skipped = 0
    failed = 0
    for row in pairs:
        tmdb_id, media_type = row["tmdb_id"], row["media_type"]
        existing = db.get_title(tmdb_id, media_type)
        if existing and (time.time() - existing["fetched_at"]) < TITLE_META_TTL:
            skipped += 1
            continue
        try:
            details = seerr.get_media_details(tmdb_id, media_type)
            if details:
                db.upsert_title(tmdb_id, media_type, {
                    "title": details.get("title"),
                    "overview": details.get("overview"),
                    "poster_path": details.get("posterPath"),
                    "genre": details.get("genre"),
                    "year": details.get("year"),
                    "status": details.get("status"),
                    "is_requestable": details.get("isRequestable"),
                    "trailer_key": details.get("trailerKey"),
                    "watch_providers": details.get("watchProviders", []),
                })
                done += 1
            else:
                failed += 1
        except Exception as e:
            log(f"[ENRICH] failed for {media_type}/{tmdb_id}: {e}")
            failed += 1
    log(f"[ENRICH] done={done} skipped(fresh)={skipped} failed={failed}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    start = time.time()
    db.init_db()
    db.set_meta("last_refresh_started_at", time.time())

    countries = load_countries()
    log(f"[REFRESH] {len(countries)} countries x {len(PLATFORMS)} platforms")

    try:
        nf_data = fetch_netflix_tsv()
    except Exception as e:
        log(f"[NETFLIX] FATAL: could not download/parse TSV this cycle: {e}")
        nf_data = {}

    for c in countries:
        slug, iso2 = c["slug"], c["iso2"]
        try:
            process_netflix(slug, iso2, nf_data)
        except Exception as e:
            log(f"[netflix/{slug}] crashed: {e}")
            traceback.print_exc()
            STATS["error"] += 1

        for platform in PLATFORMS:
            if platform == "netflix":
                continue
            try:
                process_platform(slug, iso2, platform)
            except Exception as e:
                log(f"[{platform}/{slug}] crashed: {e}")
                traceback.print_exc()
                STATS["error"] += 1

    enrich_metadata()

    db.set_meta("last_refresh_completed_at", time.time())
    db.set_meta("last_refresh_stats", json.dumps(STATS))

    elapsed = time.time() - start
    log(f"[REFRESH] Completed in {elapsed/60:.1f} minutes. Stats: {STATS}")
    if DETAIL_LOG:
        log(f"[REFRESH] {len(DETAIL_LOG)} empty/failed combos:")
        for line in DETAIL_LOG:
            log(f"  - {line}")


if __name__ == "__main__":
    main()
