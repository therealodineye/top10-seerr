"""
Legacy compatibility shim.

FlixPatrol/FlareSolverr scraping has been REMOVED ENTIRELY. All chart data now
comes from refresh.py (Netflix official TSV + JustWatch GraphQL + TMDB
discover fallback via Overseerr), written to SQLite. This module now only
provides platform/country metadata plus a thin read-only helper for the
legacy main.py CLI script -- it performs NO network access.
"""
import os
import re
import json

import db

PLATFORMS = {
    "netflix": "Netflix",
    "amazon-prime": "Amazon Prime",
    "disney": "Disney+",
    "hbo-max": "HBO Max",
    "apple-tv": "Apple TV+",
    "paramount-plus": "Paramount+"
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COUNTRIES_JSON_PATH = os.path.join(BASE_DIR, "countries.json")

with open(COUNTRIES_JSON_PATH, "r", encoding="utf-8") as f:
    _COUNTRIES = json.load(f)

# Kept for any legacy caller expecting the old per-platform shape.
PLATFORM_COUNTRIES = {p: list(_COUNTRIES) for p in PLATFORMS.keys()}


def slugify_country(country_name):
    """Standard slugifier: 'United States' -> 'united-states'."""
    name = country_name.strip().lower()
    name = name.replace("ü", "u").replace("é", "e").replace("ó", "o").replace("á", "a")
    name = re.sub(r'[^a-z0-9\s-]', '', name)
    name = re.sub(r'[\s-]+', '-', name)
    return name


def scrape_top_10(platform="netflix", selection="norway"):
    """
    Legacy-shaped helper (title-list only) for main.py. Reads the
    already-refreshed SQLite cache -- does NOT hit the network.
    """
    platform = platform.lower().strip()
    country_slug = slugify_country(selection)

    db.init_db()
    movie_rows = db.get_chart(platform, country_slug, "movie", limit=10)
    tv_rows = db.get_chart(platform, country_slug, "tv", limit=10)

    movies = [r["title"] for r in movie_rows if r["title"]]
    tv_shows = [r["title"] for r in tv_rows if r["title"]]
    return movies, tv_shows
