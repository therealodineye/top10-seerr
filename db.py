"""
SQLite persistence layer for top10-seerr.

Single source of truth for all chart / metadata / package-code caching.
WAL mode so the refresher (writer) and gunicorn workers (readers) never block
each other. Web workers must ONLY read from this module; refresh.py is the
only writer.
"""
import os
import sqlite3
import time
import json
import threading

DATA_DIR = os.environ.get("TOP10_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "top10.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS charts (
    platform        TEXT NOT NULL,
    country         TEXT NOT NULL,   -- country slug, e.g. 'norway'
    media_type      TEXT NOT NULL,   -- 'movie' or 'tv'
    rank            INTEGER NOT NULL,
    tmdb_id         INTEGER,
    title           TEXT,
    source          TEXT NOT NULL,   -- 'netflix-official' | 'justwatch' | 'tmdb-discover'
    is_new_entry    INTEGER,         -- 0/1, NULL if unknown
    weeks_in_top10  INTEGER,         -- NULL if unknown (non-Netflix sources)
    fetched_at      REAL NOT NULL,   -- unix epoch seconds
    PRIMARY KEY (platform, country, media_type, rank)
);
CREATE INDEX IF NOT EXISTS idx_charts_country ON charts(country);
CREATE INDEX IF NOT EXISTS idx_charts_platform_country ON charts(platform, country);
CREATE INDEX IF NOT EXISTS idx_charts_tmdb ON charts(tmdb_id);

CREATE TABLE IF NOT EXISTS titles (
    tmdb_id         INTEGER NOT NULL,
    media_type      TEXT NOT NULL,   -- 'movie' or 'tv'
    title           TEXT,
    overview        TEXT,
    poster_path     TEXT,
    genre           TEXT,
    year            TEXT,
    status          TEXT,            -- 'Available' | 'Processing' | 'Ready'
    is_requestable  INTEGER,
    trailer_key     TEXT,
    watch_providers TEXT,            -- JSON array
    fetched_at      REAL NOT NULL,
    PRIMARY KEY (tmdb_id, media_type)
);

CREATE TABLE IF NOT EXISTS packages (
    country     TEXT NOT NULL,
    platform    TEXT NOT NULL,
    short_code  TEXT,               -- NULL means "confirmed not available in this country"
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (country, platform)
);

CREATE TABLE IF NOT EXISTS tmdb_providers (
    country     TEXT NOT NULL,
    platform    TEXT NOT NULL,
    provider_id INTEGER,            -- NULL means "confirmed not available in this country"
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (country, platform)
);

CREATE TABLE IF NOT EXISTS title_map (
    raw_title   TEXT NOT NULL,      -- title as it appears in the Netflix TSV
    media_type  TEXT NOT NULL,
    tmdb_id     INTEGER,            -- NULL = looked up, not found (still cached to avoid re-querying)
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (raw_title, media_type)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn():
    """Thread-local sqlite connection, WAL mode, dict-like rows."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


def now():
    return time.time()


# ---------------------------------------------------------------------------
# meta helpers
# ---------------------------------------------------------------------------

def set_meta(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------

def upsert_chart_rows(platform, country, media_type, rows, source, fetched_at=None):
    """
    rows: list of dicts with keys rank, tmdb_id, title, is_new_entry, weeks_in_top10
    Replaces the FULL (platform, country, media_type) set atomically, but ONLY
    if rows is non-empty. An empty `rows` list is a no-op (never wipes
    last-known-good data on a failed/empty fetch).
    """
    if not rows:
        return 0
    fetched_at = fetched_at or now()
    conn = get_conn()
    with conn:
        conn.execute(
            "DELETE FROM charts WHERE platform = ? AND country = ? AND media_type = ?",
            (platform, country, media_type),
        )
        conn.executemany(
            """INSERT INTO charts
               (platform, country, media_type, rank, tmdb_id, title, source, is_new_entry, weeks_in_top10, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    platform, country, media_type, r["rank"], r.get("tmdb_id"), r.get("title"),
                    source, r.get("is_new_entry"), r.get("weeks_in_top10"), fetched_at,
                )
                for r in rows
            ],
        )
    return len(rows)


def get_chart(platform, country, media_type, limit=10):
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM charts WHERE platform = ? AND country = ? AND media_type = ?
           ORDER BY rank ASC LIMIT ?""",
        (platform, country, media_type, limit),
    ).fetchall()


def get_chart_meta(platform, country):
    """Returns (source, fetched_at) for the OLDEST row of a platform/country
    pair (i.e. the source/age that should be reported for the whole block),
    or (None, None) if there is no data at all."""
    conn = get_conn()
    row = conn.execute(
        """SELECT source, fetched_at FROM charts
           WHERE platform = ? AND country = ?
           ORDER BY fetched_at ASC LIMIT 1""",
        (platform, country),
    ).fetchone()
    if row:
        return row["source"], row["fetched_at"]
    return None, None


def health_counts():
    conn = get_conn()
    rows = conn.execute(
        """SELECT source, COUNT(*) as n, MIN(fetched_at) as oldest, MAX(fetched_at) as newest
           FROM charts GROUP BY source"""
    ).fetchall()
    overall = conn.execute(
        "SELECT MIN(fetched_at) as oldest, MAX(fetched_at) as newest, COUNT(*) as n FROM charts"
    ).fetchone()
    return rows, overall


# ---------------------------------------------------------------------------
# titles (metadata cache)
# ---------------------------------------------------------------------------

def upsert_title(tmdb_id, media_type, data, fetched_at=None):
    fetched_at = fetched_at or now()
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO titles
               (tmdb_id, media_type, title, overview, poster_path, genre, year, status,
                is_requestable, trailer_key, watch_providers, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tmdb_id, media_type) DO UPDATE SET
                 title=excluded.title, overview=excluded.overview, poster_path=excluded.poster_path,
                 genre=excluded.genre, year=excluded.year, status=excluded.status,
                 is_requestable=excluded.is_requestable, trailer_key=excluded.trailer_key,
                 watch_providers=excluded.watch_providers, fetched_at=excluded.fetched_at""",
            (
                tmdb_id, media_type, data.get("title"), data.get("overview"), data.get("poster_path"),
                data.get("genre"), data.get("year"), data.get("status"), int(bool(data.get("is_requestable"))),
                data.get("trailer_key"), json.dumps(data.get("watch_providers") or []), fetched_at,
            ),
        )


def get_title(tmdb_id, media_type):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM titles WHERE tmdb_id = ? AND media_type = ?", (tmdb_id, media_type)
    ).fetchone()
    return row


def get_titles_bulk(pairs):
    """pairs: iterable of (tmdb_id, media_type). Returns dict {(id,type): row}."""
    conn = get_conn()
    out = {}
    for tmdb_id, media_type in pairs:
        row = conn.execute(
            "SELECT * FROM titles WHERE tmdb_id = ? AND media_type = ?", (tmdb_id, media_type)
        ).fetchone()
        if row:
            out[(tmdb_id, media_type)] = row
    return out


# ---------------------------------------------------------------------------
# packages (JustWatch short codes) / tmdb_providers
# ---------------------------------------------------------------------------

def get_package(country, platform):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM packages WHERE country = ? AND platform = ?", (country, platform)
    ).fetchone()


def set_package(country, platform, short_code, fetched_at=None):
    fetched_at = fetched_at or now()
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO packages (country, platform, short_code, fetched_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(country, platform) DO UPDATE SET short_code=excluded.short_code, fetched_at=excluded.fetched_at""",
            (country, platform, short_code, fetched_at),
        )


def get_tmdb_provider(country, platform):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM tmdb_providers WHERE country = ? AND platform = ?", (country, platform)
    ).fetchone()


def set_tmdb_provider(country, platform, provider_id, fetched_at=None):
    fetched_at = fetched_at or now()
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO tmdb_providers (country, platform, provider_id, fetched_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(country, platform) DO UPDATE SET provider_id=excluded.provider_id, fetched_at=excluded.fetched_at""",
            (country, platform, provider_id, fetched_at),
        )


# ---------------------------------------------------------------------------
# title_map (Netflix TSV title -> tmdbId cache)
# ---------------------------------------------------------------------------

def get_title_map(raw_title, media_type):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM title_map WHERE raw_title = ? AND media_type = ?", (raw_title, media_type)
    ).fetchone()


def set_title_map(raw_title, media_type, tmdb_id, fetched_at=None):
    fetched_at = fetched_at or now()
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO title_map (raw_title, media_type, tmdb_id, fetched_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(raw_title, media_type) DO UPDATE SET tmdb_id=excluded.tmdb_id, fetched_at=excluded.fetched_at""",
            (raw_title, media_type, tmdb_id, fetched_at),
        )
