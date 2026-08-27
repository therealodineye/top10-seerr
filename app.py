import os
import sys
import json
import time
import datetime
import threading
import urllib.parse
from flask import Flask, render_template, jsonify, request, Response
from dotenv import load_dotenv

# Ensure we can load helper modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import db
import seerr

# Load active environment configuration
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

app = Flask(__name__)
app.template_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PLATFORMS = {
    "netflix": "Netflix",
    "amazon-prime": "Amazon Prime Video",
    "disney": "Disney Plus",
    "hbo-max": "HBO Max",
    "apple-tv": "Apple TV",
    "paramount-plus": "Paramount Plus",
}

with open(os.path.join(BASE_DIR, "countries.json"), "r", encoding="utf-8") as f:
    COUNTRIES = json.load(f)
COUNTRY_SLUGS = {c["slug"] for c in COUNTRIES}
DEFAULT_COUNTRY = "norway"

db.init_db()


def get_env_vars():
    from dotenv import dotenv_values
    config = dotenv_values(env_path) if os.path.exists(env_path) else {}
    return {
        "SEERR_URL": config.get("SEERR_URL") or os.getenv("SEERR_URL") or "",
        "SEERR_API_KEY": config.get("SEERR_API_KEY") or os.getenv("SEERR_API_KEY") or "",
        "SEERR_EMAIL": config.get("SEERR_EMAIL") or os.getenv("SEERR_EMAIL") or "",
        "SEERR_PASSWORD": config.get("SEERR_PASSWORD") or os.getenv("SEERR_PASSWORD") or "",
        "AUTOMATION_ENABLED": config.get("AUTOMATION_ENABLED") or os.getenv("AUTOMATION_ENABLED") or "false",
        "AUTOMATION_PLATFORM": config.get("AUTOMATION_PLATFORM") or os.getenv("AUTOMATION_PLATFORM") or "netflix",
        "AUTOMATION_COUNTRY": config.get("AUTOMATION_COUNTRY") or os.getenv("AUTOMATION_COUNTRY") or "norway",
        "AUTOMATION_MEDIA_TYPE": config.get("AUTOMATION_MEDIA_TYPE") or os.getenv("AUTOMATION_MEDIA_TYPE") or "both",
        "AUTOMATION_PIPELINES": config.get("AUTOMATION_PIPELINES") or os.getenv("AUTOMATION_PIPELINES") or "[]",
        "NOTIFICATION_DISCORD_WEBHOOK": config.get("NOTIFICATION_DISCORD_WEBHOOK") or os.getenv("NOTIFICATION_DISCORD_WEBHOOK") or ""
    }


def normalize_country(slug):
    slug = (slug or "").strip().lower()
    if slug in ("", "world"):
        return DEFAULT_COUNTRY
    if slug in COUNTRY_SLUGS:
        return slug
    return DEFAULT_COUNTRY


def build_poster_url(poster_path):
    if not poster_path:
        return None
    raw = f"https://image.tmdb.org/t/p/w300{poster_path}"
    return f"/api/proxy-image?url={urllib.parse.quote(raw)}"


def build_item(chart_row, media_type):
    """Combine a `charts` row with its cached `titles` metadata into a single
    API item. Never raises -- always returns SOMETHING for a chart row, even
    if metadata enrichment hasn't run yet for this tmdbId."""
    tmdb_id = chart_row["tmdb_id"]
    title_row = db.get_title(tmdb_id, media_type) if tmdb_id else None

    watch_providers = []
    if title_row and title_row["watch_providers"]:
        try:
            watch_providers = json.loads(title_row["watch_providers"])
        except Exception:
            watch_providers = []

    item = {
        "rank": chart_row["rank"],
        "title": (title_row["title"] if title_row and title_row["title"] else chart_row["title"]),
        "type": media_type if media_type == "movie" else "tv",
        "tmdbId": tmdb_id,
        "posterUrl": build_poster_url(title_row["poster_path"]) if title_row else None,
        "overview": title_row["overview"] if title_row else None,
        "genre": title_row["genre"] if title_row else None,
        "year": title_row["year"] if title_row else None,
        "status": title_row["status"] if title_row else "Ready",
        "isRequestable": bool(title_row["is_requestable"]) if title_row else True,
        "isNewEntry": bool(chart_row["is_new_entry"]) if chart_row["is_new_entry"] is not None else False,
        "weeksInTop10": chart_row["weeks_in_top10"],
        "trailerKey": title_row["trailer_key"] if title_row else None,
        "watchProviders": watch_providers,
    }
    return item


def get_platform_block(platform, country_slug):
    movie_rows = db.get_chart(platform, country_slug, "movie", limit=10)
    tv_rows = db.get_chart(platform, country_slug, "tv", limit=10)

    movies = [build_item(r, "movie") for r in movie_rows]
    shows = [build_item(r, "tv") for r in tv_rows]

    source, fetched_at = db.get_chart_meta(platform, country_slug)
    if fetched_at is None:
        # fall back to whichever of movie/tv rows actually exist
        candidates = list(movie_rows) + list(tv_rows)
        fetched_at = min((r["fetched_at"] for r in candidates), default=None)
        source = candidates[0]["source"] if candidates else None

    age_hours = round((time.time() - fetched_at) / 3600, 1) if fetched_at else None
    fetched_iso = (
        datetime.datetime.utcfromtimestamp(fetched_at).strftime("%Y-%m-%dT%H:%M:%SZ")
        if fetched_at else None
    )

    return {
        "platform": platform,
        "displayName": PLATFORMS.get(platform, platform.title()),
        "source": source,
        "fetchedAt": fetched_iso,
        "ageHours": age_hours,
        "movies": movies,
        "shows": shows,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.route("/api/countries", methods=["GET"])
def get_countries():
    return jsonify(COUNTRIES)


@app.route("/api/home", methods=["GET"])
def api_home():
    """
    Single-call replacement for the 6 concurrent per-platform requests the
    Android app used to make. Pure SQLite read -- never touches the network.
    """
    country_slug = normalize_country(request.args.get("country"))
    platforms = [get_platform_block(p, country_slug) for p in PLATFORMS]

    return jsonify({
        "country": country_slug,
        "generatedAt": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    })


@app.route("/api/health", methods=["GET"])
def api_health():
    rows, overall = db.health_counts()
    now = time.time()

    def fmt_age(ts):
        return round((now - ts) / 3600, 1) if ts else None

    by_source = [
        {
            "source": r["source"],
            "rows": r["n"],
            "oldestAgeHours": fmt_age(r["oldest"]),
            "newestAgeHours": fmt_age(r["newest"]),
        }
        for r in rows
    ]

    last_started = db.get_meta("last_refresh_started_at")
    last_completed = db.get_meta("last_refresh_completed_at")
    last_stats = db.get_meta("last_refresh_stats")

    return jsonify({
        "totalChartRows": overall["n"] if overall else 0,
        "oldestAgeHours": fmt_age(overall["oldest"]) if overall else None,
        "newestAgeHours": fmt_age(overall["newest"]) if overall else None,
        "bySource": by_source,
        "lastRefreshStartedAt": last_started,
        "lastRefreshCompletedAt": last_completed,
        "lastRefreshStats": json.loads(last_stats) if last_stats else None,
    })


@app.route("/api/fetch", methods=["POST"])
def fetch_list():
    """Backwards-compatible endpoint -- same request/response shape as
    before, now a pure SQLite read instead of a live FlixPatrol scrape."""
    data = request.json or {}
    platform = data.get("platform", "netflix").strip().lower()
    selection = normalize_country(data.get("selection", "norway"))
    fetch_movies = data.get("movies", True)
    fetch_shows = data.get("shows", True)

    if platform not in PLATFORMS:
        return jsonify({"error": f"Unknown platform '{platform}'"}), 400

    movies_results = [build_item(r, "movie") for r in db.get_chart(platform, selection, "movie", 10)] if fetch_movies else []
    shows_results = [build_item(r, "tv") for r in db.get_chart(platform, selection, "tv", 10)] if fetch_shows else []

    return jsonify({
        "selection": selection,
        "movies": movies_results,
        "shows": shows_results,
    })


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(get_env_vars())


@app.route("/api/config", methods=["POST"])
def save_config():
    data = request.json or {}
    url = data.get("SEERR_URL", "").strip().replace("\n", "").replace("\r", "")
    api_key = data.get("SEERR_API_KEY", "").strip().replace("\n", "").replace("\r", "")
    email = data.get("SEERR_EMAIL", "").strip().replace("\n", "").replace("\r", "")
    password = data.get("SEERR_PASSWORD", "").strip().replace("\n", "").replace("\r", "")

    auto_enabled = str(data.get("AUTOMATION_ENABLED", "false")).strip().lower().replace("\n", "").replace("\r", "")
    auto_platform = data.get("AUTOMATION_PLATFORM", "netflix").strip().lower().replace("\n", "").replace("\r", "")
    auto_country = data.get("AUTOMATION_COUNTRY", "norway").strip().lower().replace("\n", "").replace("\r", "")
    auto_media_type = data.get("AUTOMATION_MEDIA_TYPE", "both").strip().lower().replace("\n", "").replace("\r", "")
    auto_pipelines = str(data.get("AUTOMATION_PIPELINES", "[]")).strip().replace("\n", "").replace("\r", "")

    discord_webhook = data.get("NOTIFICATION_DISCORD_WEBHOOK", "").strip().replace("\n", "").replace("\r", "")

    try:
        with open(env_path, "w") as f:
            f.write(f"SEERR_URL={url}\n")
            f.write(f"SEERR_API_KEY={api_key}\n")
            f.write(f"SEERR_EMAIL={email}\n")
            f.write(f"SEERR_PASSWORD={password}\n")
            f.write(f"AUTOMATION_ENABLED={auto_enabled}\n")
            f.write(f"AUTOMATION_PLATFORM={auto_platform}\n")
            f.write(f"AUTOMATION_COUNTRY={auto_country}\n")
            f.write(f"AUTOMATION_MEDIA_TYPE={auto_media_type}\n")
            f.write(f"AUTOMATION_PIPELINES={auto_pipelines}\n")
            f.write(f"NOTIFICATION_DISCORD_WEBHOOK={discord_webhook}\n")

        load_dotenv(env_path, override=True)
        seerr.clear_cached_cookie()

        return jsonify({"success": True, "message": "Configuration successfully saved and reloaded."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def log_sync_event(platform, country, total_scraped, requested_items, status, details=None):
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_history.json")
    log_entry = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "platform": platform,
        "country": country,
        "totalScraped": total_scraped,
        "requestedItems": requested_items,
        "status": status,
        "details": details or []
    }

    logs = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                logs = json.load(f)
                if not isinstance(logs, list):
                    logs = []
        except Exception:
            logs = []

    logs.insert(0, log_entry)
    logs = logs[:100]

    try:
        with open(log_file, "w") as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        print(f"[LOG ERROR] Failed to write sync log: {e}")


def send_discord_notification(platform, country, synced_items):
    config = get_env_vars()
    webhook_url = config.get("NOTIFICATION_DISCORD_WEBHOOK")
    if not webhook_url:
        return

    import requests

    items_list_str = ""
    if synced_items:
        for item in synced_items:
            success_mark = "\U0001F7E2 Requested" if item.get("success") else f"\U0001F534 Failed ({item.get('error', 'Unknown Error')})"
            items_list_str += f"- **{item.get('title')}** ({item.get('type', 'movie').upper()}): {success_mark}\n"
    else:
        items_list_str = "_No new titles required sync (all already requested/available)_"

    embed = {
        "title": "Top10 to Seerr Sync Status",
        "color": 3066993,
        "description": f"A synchronization pipeline has executed successfully.\n\n**Pipeline Parameters:**\n- **Platform:** {platform.title()}\n- **Country/Region:** {country.title()}\n\n**Sync Actions Summary:**\n{items_list_str}",
        "footer": {"text": "Top10 to Seerr Sync System Manager"},
    }

    payload = {"embeds": [embed]}

    try:
        requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=5)
    except Exception as e:
        print(f"[WEBHOOK ERROR] Failed to dispatch Discord notification: {e}")


@app.route("/api/history", methods=["GET"])
def get_sync_history():
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_history.json")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                logs = json.load(f)
                return jsonify(logs)
        except Exception as e:
            return jsonify({"error": f"Failed to read logs: {str(e)}"}), 500
    return jsonify([])


@app.route("/api/diagnostics", methods=["GET"])
def get_diagnostics():
    import requests as _requests
    config = get_env_vars()
    url = config.get("SEERR_URL", "").rstrip("/")
    api_key = config.get("SEERR_API_KEY", "")
    email = config.get("SEERR_EMAIL", "")
    password = config.get("SEERR_PASSWORD", "")

    results = {
        "overseerr_url_configured": bool(url),
        "overseerr_api_key_configured": bool(api_key),
        "overseerr_email_configured": bool(email),
        "overseerr_password_configured": bool(password),
        "overseerr_ping_latency_ms": None,
        "overseerr_ping_status": "Not Tested",
        "overseerr_auth_status": "Not Tested",
        "justwatch_status": "Not Tested",
        "justwatch_latency_ms": None,
        "overall_status": "Healthy"
    }

    if url:
        start_time = time.time()
        try:
            res = _requests.get(f"{url}/api/v1/status", timeout=5)
            latency = int((time.time() - start_time) * 1000)
            results["overseerr_ping_latency_ms"] = latency
            if res.status_code == 200:
                results["overseerr_ping_status"] = f"Online ({res.status_code})"
            else:
                results["overseerr_ping_status"] = f"Warning Status ({res.status_code})"
                results["overall_status"] = "Degraded"
        except Exception as e:
            results["overseerr_ping_status"] = f"Failed: {str(e)}"
            results["overall_status"] = "Critical"

    if email and password and url:
        try:
            seerr.clear_cached_cookie()
            cookie = seerr.get_auth_cookie()
            if cookie:
                results["overseerr_auth_status"] = "Authenticated Successfully"
            else:
                results["overseerr_auth_status"] = "Failed to retrieve session cookie"
                results["overall_status"] = "Degraded"
        except Exception as e:
            results["overseerr_auth_status"] = f"Failed: {str(e)}"
            results["overall_status"] = "Degraded"

    start_time = time.time()
    try:
        res = _requests.post(
            "https://apis.justwatch.com/graphql",
            headers={"User-Agent": "Mozilla/5.0 Chrome/148.0.0.0", "Content-Type": "application/json"},
            json={"query": "query($c:Country!){packages(country:$c,platform:WEB){shortName}}", "variables": {"c": "NO"}},
            timeout=5,
        )
        latency = int((time.time() - start_time) * 1000)
        results["justwatch_latency_ms"] = latency
        if res.status_code == 200:
            results["justwatch_status"] = f"Reachable ({res.status_code})"
        else:
            results["justwatch_status"] = f"Warning Status ({res.status_code})"
            results["overall_status"] = "Degraded"
    except Exception as e:
        results["justwatch_status"] = f"Failed to connect: {str(e)}"
        results["overall_status"] = "Degraded"

    return jsonify(results)


@app.route("/api/sync", methods=["POST"])
def sync_selected():
    data = request.json or {}
    items = data.get("requests", [])
    platform = data.get("platform", "netflix")
    country = data.get("country", "norway")

    config = get_env_vars()
    if not config["SEERR_URL"] or not config["SEERR_EMAIL"] or not config["SEERR_PASSWORD"]:
        return jsonify({
            "error": "Local auth credentials missing. Please open Settings via the Gear Icon to configure SEERR_EMAIL and SEERR_PASSWORD."
        }), 400

    results = []
    success_count = 0
    fail_count = 0

    for item in items:
        tmdb_id = item.get("tmdbId")
        media_type = item.get("type")
        title = item.get("title", f"TMDB ID {tmdb_id}")

        if not tmdb_id:
            results.append({"title": title, "type": media_type, "success": False, "error": "Missing TMDB ID"})
            fail_count += 1
            continue

        try:
            seerr.request_media(tmdb_id, media_type)
            results.append({"title": title, "type": media_type, "success": True})
            success_count += 1
        except Exception as e:
            results.append({"title": title, "type": media_type, "success": False, "error": str(e)})
            fail_count += 1

    log_status = "Partial" if fail_count > 0 else "Success"
    log_sync_event(platform, country, len(items), success_count, log_status, results)
    send_discord_notification(platform, country, results)

    return jsonify({
        "success": True,
        "successCount": success_count,
        "failCount": fail_count,
        "details": results
    })


@app.route("/api/proxy-image", methods=["GET"])
def proxy_image():
    import requests
    target_url = request.args.get("url")
    if not target_url:
        return "Missing url parameter", 400

    import re
    tmdb_match = re.search(r'(https?://image\.tmdb\.org/t/p/.*)$', target_url)
    tmdb_url = tmdb_match.group(1) if tmdb_match else target_url

    try:
        fallback_res = requests.get(tmdb_url, timeout=5, stream=True)
        fallback_res.raise_for_status()
        return Response(
            fallback_res.raw.read(),
            mimetype=fallback_res.headers.get("Content-Type", "image/jpeg")
        )
    except Exception as e:
        print(f"[IMAGE PROXY WARNING] Direct TMDB CDN fetch failed ({e}). Falling back to Seerr local API...")

        config = get_env_vars()
        headers = {"X-Api-Key": config["SEERR_API_KEY"]}
        try:
            img_res = requests.get(target_url, headers=headers, timeout=5, stream=True)
            img_res.raise_for_status()
            return Response(
                img_res.raw.read(),
                mimetype=img_res.headers.get("Content-Type", "image/jpeg")
            )
        except Exception as seerr_err:
            print(f"[IMAGE PROXY ERROR] Seerr backup fetch also failed: {seerr_err}")
            return "Failed to load image", 500


def run_automation_worker():
    """
    Background worker that wakes up every 48 hours to sync pinned lists to
    Overseerr. Reads exclusively from SQLite (populated by the external
    refresh.py cron job) -- never fetches from the internet itself.
    """
    print("[AUTOMATION] Background worker thread started successfully.")
    INTERVAL = 172800
    time.sleep(10)

    while True:
        try:
            from dotenv import dotenv_values
            env = dotenv_values(env_path)

            enabled = env.get("AUTOMATION_ENABLED", "false").strip().lower() == "true"

            pipelines = []
            pipelines_json = env.get("AUTOMATION_PIPELINES", "").strip()
            if pipelines_json:
                try:
                    pipelines = json.loads(pipelines_json)
                except Exception as parse_err:
                    print(f"[AUTOMATION ERROR] Failed to parse AUTOMATION_PIPELINES: {parse_err}")

            if not pipelines:
                platform = env.get("AUTOMATION_PLATFORM", "netflix").strip().lower()
                country = env.get("AUTOMATION_COUNTRY", "norway").strip().lower()
                media_type = env.get("AUTOMATION_MEDIA_TYPE", "both").strip().lower()
                pipelines = [{"platform": platform, "country": country, "media": media_type}]

            if enabled:
                print(f"[AUTOMATION] Pinned Sync starting for {len(pipelines)} pipelines...")
                url = env.get("SEERR_URL", "").rstrip("/")
                api_key = env.get("SEERR_API_KEY", "")
                email = env.get("SEERR_EMAIL", "")
                password = env.get("SEERR_PASSWORD", "")

                if url and api_key and email and password:
                    for pipe in pipelines:
                        p_platform = pipe.get("platform", "netflix").strip().lower()
                        p_country = normalize_country(pipe.get("country", "norway"))
                        p_media_type = pipe.get("media", "both").strip().lower()

                        print(f"[AUTOMATION] Executing pipeline: Platform={p_platform}, Country={p_country}, Media={p_media_type}")
                        try:
                            items_to_sync = []
                            if p_media_type in ["both", "movie"]:
                                for r in db.get_chart(p_platform, p_country, "movie", 10):
                                    item = build_item(r, "movie")
                                    if item["tmdbId"] and item["isRequestable"]:
                                        items_to_sync.append({"tmdbId": item["tmdbId"], "type": "movie", "title": item["title"]})
                            if p_media_type in ["both", "tv"]:
                                for r in db.get_chart(p_platform, p_country, "tv", 10):
                                    item = build_item(r, "tv")
                                    if item["tmdbId"] and item["isRequestable"]:
                                        items_to_sync.append({"tmdbId": item["tmdbId"], "type": "tv", "title": item["title"]})

                            if items_to_sync:
                                print(f"[AUTOMATION] Syncing {len(items_to_sync)} items via Overseerr bot auth...")
                                success_count = 0
                                results = []
                                for item in items_to_sync:
                                    try:
                                        seerr.request_media(item["tmdbId"], item["type"])
                                        results.append({"title": item["title"], "type": item["type"], "success": True})
                                        success_count += 1
                                        time.sleep(1)
                                    except Exception as req_err:
                                        results.append({"title": item["title"], "type": item["type"], "success": False, "error": str(req_err)})
                                        print(f"[AUTOMATION ERROR] Failed to request {item['title']}: {req_err}")

                                log_status = "Partial" if any(not r["success"] for r in results) else "Success"
                                log_sync_event(p_platform, p_country, len(items_to_sync), success_count, log_status, results)
                                send_discord_notification(p_platform, p_country, results)

                                print(f"[AUTOMATION] Pipeline finished: requested {success_count}/{len(items_to_sync)} items successfully.")
                            else:
                                print("[AUTOMATION] No items require sync in this pipeline.")
                        except Exception as pipe_err:
                            print(f"[AUTOMATION ERROR] Pipeline Platform={p_platform}, Country={p_country} failed: {pipe_err}")
                else:
                    print("[AUTOMATION WARNING] Overseerr credentials missing. Skipping background sync.")
            else:
                print("[AUTOMATION] Pinned background sync is disabled.")
        except Exception as e:
            print(f"[AUTOMATION CRASH] Pinned background worker crashed: {e}")

        print("[AUTOMATION] Sleeping for 48 hours...")
        time.sleep(INTERVAL)


if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    threading.Thread(target=run_automation_worker, daemon=True).start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
