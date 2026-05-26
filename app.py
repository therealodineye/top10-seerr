import os
import sys
import time
import requests
import threading
import urllib.parse
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

# Ensure we can load helper modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import scraper
import seerr

# Load active environment configuration
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

app = Flask(__name__)

# Make sure templates folder exists or Flask knows where it is
app.template_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def get_env_vars():
    """
    Reads active configuration values directly from the .env file if it exists,
    or falls back to active environment variables.
    """
    from dotenv import dotenv_values
    config = dotenv_values(env_path) if os.path.exists(env_path) else {}
    return {
        "SEERR_URL": config.get("SEERR_URL") or os.getenv("SEERR_URL") or "",
        "SEERR_API_KEY": config.get("SEERR_API_KEY") or os.getenv("SEERR_API_KEY") or "",
        "SEERR_EMAIL": config.get("SEERR_EMAIL") or os.getenv("SEERR_EMAIL") or "",
        "SEERR_PASSWORD": config.get("SEERR_PASSWORD") or os.getenv("SEERR_PASSWORD") or "",
        "AUTOMATION_ENABLED": config.get("AUTOMATION_ENABLED") or os.getenv("AUTOMATION_ENABLED") or "false",
        "AUTOMATION_PLATFORM": config.get("AUTOMATION_PLATFORM") or os.getenv("AUTOMATION_PLATFORM") or "netflix",
        "AUTOMATION_COUNTRY": config.get("AUTOMATION_COUNTRY") or os.getenv("AUTOMATION_COUNTRY") or "world",
        "AUTOMATION_MEDIA_TYPE": config.get("AUTOMATION_MEDIA_TYPE") or os.getenv("AUTOMATION_MEDIA_TYPE") or "both",
        "AUTOMATION_PIPELINES": config.get("AUTOMATION_PIPELINES") or os.getenv("AUTOMATION_PIPELINES") or "[]",
        "NOTIFICATION_DISCORD_WEBHOOK": config.get("NOTIFICATION_DISCORD_WEBHOOK") or os.getenv("NOTIFICATION_DISCORD_WEBHOOK") or ""
    }

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")

@app.route("/api/countries", methods=["GET"])
def get_countries():
    return jsonify(scraper.PLATFORM_COUNTRIES)

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
    
    # Pinned automation variables
    auto_enabled = str(data.get("AUTOMATION_ENABLED", "false")).strip().lower().replace("\n", "").replace("\r", "")
    auto_platform = data.get("AUTOMATION_PLATFORM", "netflix").strip().lower().replace("\n", "").replace("\r", "")
    auto_country = data.get("AUTOMATION_COUNTRY", "world").strip().lower().replace("\n", "").replace("\r", "")
    auto_media_type = data.get("AUTOMATION_MEDIA_TYPE", "both").strip().lower().replace("\n", "").replace("\r", "")
    auto_pipelines = str(data.get("AUTOMATION_PIPELINES", "[]")).strip().replace("\n", "").replace("\r", "")
    
    # Premium features variables
    discord_webhook = data.get("NOTIFICATION_DISCORD_WEBHOOK", "").strip().replace("\n", "").replace("\r", "")

    try:
        # Save to local .env
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

        # Reload environment variables in Python context immediately
        load_dotenv(env_path, override=True)
        # Clear the Overseerr auth session cookie
        seerr.clear_cached_cookie()
        
        return jsonify({"success": True, "message": "Configuration successfully saved and reloaded."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/fetch", methods=["POST"])
def fetch_list():
    data = request.json or {}
    platform = data.get("platform", "netflix").strip().lower()
    selection = data.get("selection", "world").strip().lower()
    fetch_movies = data.get("movies", True)
    fetch_shows = data.get("shows", True)

    # Soft check: If config is missing, return friendly notice
    config = get_env_vars()
    if not config["SEERR_URL"] or not config["SEERR_API_KEY"]:
        return jsonify({
            "error": "Overseerr is not configured. Please open Settings via the Gear Icon to configure SEERR_URL and SEERR_API_KEY."
        }), 400

    try:
        movies_scraped, shows_scraped = scraper.scrape_top_10(platform, selection)
    except Exception as e:
        return jsonify({"error": f"Failed to scrape Netflix lists: {str(e)}"}), 500

    movies_results = []
    shows_results = []

    # Process movies
    if fetch_movies:
        for idx, title in enumerate(movies_scraped, 1):
            item = {
                "rank": idx,
                "title": title,
                "tmdbId": None,
                "posterUrl": None,
                "status": "Ready",
                "isRequestable": True
            }
            # Search in Seerr
            seerr_res = seerr.search_media(title, "movie")
            if seerr_res:
                item["tmdbId"] = seerr_res.get("tmdbId")
                raw_url = seerr_res.get("posterUrl")
                if raw_url:
                    item["posterUrl"] = f"/api/proxy-image?url={urllib.parse.quote(raw_url)}"
                item["status"] = seerr_res.get("status")
                item["isRequestable"] = not seerr_res.get("skip")
                item["year"] = seerr_res.get("year")
                item["genre"] = seerr_res.get("genre")
                item["overview"] = seerr_res.get("overview")
                item["watchProviders"] = seerr_res.get("watchProviders", [])
                item["trailerKey"] = seerr_res.get("trailerKey")
            movies_results.append(item)

    # Process TV shows
    if fetch_shows:
        for idx, title in enumerate(shows_scraped, 1):
            item = {
                "rank": idx,
                "title": title,
                "tmdbId": None,
                "posterUrl": None,
                "status": "Ready",
                "isRequestable": True
            }
            # Search in Seerr
            seerr_res = seerr.search_media(title, "tv")
            if seerr_res:
                item["tmdbId"] = seerr_res.get("tmdbId")
                raw_url = seerr_res.get("posterUrl")
                if raw_url:
                    item["posterUrl"] = f"/api/proxy-image?url={urllib.parse.quote(raw_url)}"
                item["status"] = seerr_res.get("status")
                item["isRequestable"] = not seerr_res.get("skip")
                item["year"] = seerr_res.get("year")
                item["genre"] = seerr_res.get("genre")
                item["overview"] = seerr_res.get("overview")
                item["watchProviders"] = seerr_res.get("watchProviders", [])
                item["trailerKey"] = seerr_res.get("trailerKey")
            shows_results.append(item)

    return jsonify({
        "selection": selection,
        "movies": movies_results,
        "shows": shows_results
    })

def log_sync_event(platform, country, total_scraped, requested_items, status, details=None):
    """
    Appends a structured sync event log to sync_history.json.
    """
    import datetime
    import json
    
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
            
    logs.insert(0, log_entry) # Most recent first
    logs = logs[:100] # Keep last 100 entries to prevent file bloat
    
    try:
        with open(log_file, "w") as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        print(f"[LOG ERROR] Failed to write sync log: {e}")

def send_discord_notification(platform, country, synced_items):
    """
    Dispatches a rich embed discord notification card summarizing a sync execution.
    """
    config = get_env_vars()
    webhook_url = config.get("NOTIFICATION_DISCORD_WEBHOOK")
    if not webhook_url:
        return
        
    import json
    import requests
    
    title_text = "Top10 to Seerr Sync Status"
    color_code = 3066993 # Green for normal
    
    items_list_str = ""
    if synced_items:
        for item in synced_items:
            success_mark = "🟢 Requested" if item.get("success") else f"🔴 Failed ({item.get('error', 'Unknown Error')})"
            items_list_str += f"- **{item.get('title')}** ({item.get('type', 'movie').upper()}): {success_mark}\n"
    else:
        items_list_str = "_No new titles required sync (all already requested/available)_"
        
    embed = {
        "title": title_text,
        "color": color_code,
        "description": f"A synchronization pipeline has executed successfully.\n\n**Pipeline Parameters:**\n- **Platform:** {platform.title()}\n- **Country/Region:** {country.title()}\n\n**Sync Actions Summary:**\n{items_list_str}",
        "footer": {
            "text": "Top10 to Seerr Sync System Manager"
        },
        "timestamp": requests.utils.default_headers().get("Date") or ""
    }
    
    payload = {
        "embeds": [embed]
    }
    
    try:
        requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=5)
    except Exception as e:
        print(f"[WEBHOOK ERROR] Failed to dispatch Discord notification: {e}")

@app.route("/api/history", methods=["GET"])
def get_sync_history():
    import json
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
        "flixpatrol_status": "Not Tested",
        "flixpatrol_latency_ms": None,
        "overall_status": "Healthy"
    }
    
    if url:
        start_time = time.time()
        try:
            res = requests.get(f"{url}/api/v1/status", timeout=5)
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
        from curl_cffi import requests as cffi_requests
        res = cffi_requests.get("https://flixpatrol.com", impersonate="chrome", timeout=5)
        latency = int((time.time() - start_time) * 1000)
        results["flixpatrol_latency_ms"] = latency
        if res.status_code == 200:
            results["flixpatrol_status"] = f"Reachable ({res.status_code})"
        else:
            results["flixpatrol_status"] = f"Warning Status ({res.status_code})"
            results["overall_status"] = "Degraded"
    except Exception as e:
        results["flixpatrol_status"] = f"Failed to connect: {str(e)}"
        results["overall_status"] = "Degraded"
        
    return jsonify(results)

@app.route("/api/sync", methods=["POST"])
def sync_selected():
    data = request.json or {}
    items = data.get("requests", [])
    platform = data.get("platform", "netflix")
    country = data.get("country", "world")

    # Check configuration
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
        media_type = item.get("type") # "movie" or "tv"
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

    # 1. Log event persistently
    log_status = "Partial" if fail_count > 0 else "Success"
    log_sync_event(platform, country, len(items), success_count, log_status, results)
    
    # 2. Dispatch webhook alert
    send_discord_notification(platform, country, results)

    return jsonify({
        "success": True,
        "successCount": success_count,
        "failCount": fail_count,
        "details": results
    })

@app.route("/api/proxy-image", methods=["GET"])
def proxy_image():
    target_url = request.args.get("url")
    if not target_url:
        return "Missing url parameter", 400

    # 1. Self-healing/NAT-Bypassing: Extract the public TMDB CDN URL and fetch it directly first.
    # This prevents Docker loopback/NAT Hairpin connection timeouts on the server when trying to fetch via Seerr's local URL.
    import re
    tmdb_match = re.search(r'(https?://image\.tmdb\.org/t/p/.*)$', target_url)
    if tmdb_match:
        tmdb_url = tmdb_match.group(1)
    else:
        tmdb_url = target_url

    try:
        fallback_res = requests.get(tmdb_url, timeout=5, stream=True)
        fallback_res.raise_for_status()
        
        from flask import Response
        return Response(
            fallback_res.raw.read(),
            mimetype=fallback_res.headers.get("Content-Type", "image/jpeg")
        )
    except Exception as e:
        print(f"[IMAGE PROXY WARNING] Direct TMDB CDN fetch failed ({e}). Falling back to Seerr local API...")
        
        # 2. Secondary backup: Fetch via Seerr API if TMDB CDN fails
        config = get_env_vars()
        headers = {
            "X-Api-Key": config["SEERR_API_KEY"]
        }
        try:
            img_res = requests.get(target_url, headers=headers, timeout=5, stream=True)
            img_res.raise_for_status()
            
            from flask import Response
            return Response(
                img_res.raw.read(),
                mimetype=img_res.headers.get("Content-Type", "image/jpeg")
            )
        except Exception as seerr_err:
            print(f"[IMAGE PROXY ERROR] Seerr backup fetch also failed: {seerr_err}")
            return "Failed to load image", 500


def run_automation_worker():
    """
    Background worker that wakes up precisely every 48 hours to sync the pinned lists.
    """
    print("[AUTOMATION] Background worker thread started successfully.")
    # 48 hours in seconds: 48 * 3600 = 172800 seconds
    INTERVAL = 172800 
    
    # Wait 10 seconds on boot before starting first sync
    time.sleep(10)
    
    while True:
        try:
            from dotenv import dotenv_values
            import json
            env = dotenv_values(env_path)
            
            enabled = env.get("AUTOMATION_ENABLED", "false").strip().lower() == "true"
            
            pipelines = []
            pipelines_json = env.get("AUTOMATION_PIPELINES", "").strip()
            if pipelines_json:
                try:
                    pipelines = json.loads(pipelines_json)
                except Exception as parse_err:
                    print(f"[AUTOMATION ERROR] Failed to parse AUTOMATION_PIPELINES: {parse_err}")
            
            # Fallback to legacy single scalar settings if pipelines is empty
            if not pipelines:
                platform = env.get("AUTOMATION_PLATFORM", "netflix").strip().lower()
                country = env.get("AUTOMATION_COUNTRY", "world").strip().lower()
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
                        p_country = pipe.get("country", "world").strip().lower()
                        p_media_type = pipe.get("media", "both").strip().lower()
                        
                        print(f"[AUTOMATION] Executing pipeline: Platform={p_platform}, Country={p_country}, Media={p_media_type}")
                        try:
                            movies_scraped, shows_scraped = scraper.scrape_top_10(p_platform, p_country)
                            items_to_sync = []
                            
                            # Movies
                            if p_media_type in ["both", "movie"] and movies_scraped:
                                for title in movies_scraped:
                                    res = seerr.search_media(title, "movie")
                                    if res and not res.get("skip") and res.get("tmdbId"):
                                        items_to_sync.append({"tmdbId": res["tmdbId"], "type": "movie", "title": title})
                                        
                            # TV Shows
                            if p_media_type in ["both", "tv"] and shows_scraped:
                                for title in shows_scraped:
                                    res = seerr.search_media(title, "tv")
                                    if res and not res.get("skip") and res.get("tmdbId"):
                                        items_to_sync.append({"tmdbId": res["tmdbId"], "type": "tv", "title": title})
                                        
                            if items_to_sync:
                                print(f"[AUTOMATION] Syncing {len(items_to_sync)} items via Overseerr bot auth...")
                                success_count = 0
                                results = []
                                for item in items_to_sync:
                                    try:
                                        seerr.request_media(item["tmdbId"], item["type"])
                                        results.append({"title": item["title"], "type": item["type"], "success": True})
                                        success_count += 1
                                        time.sleep(1) # Gentle on Seerr
                                    except Exception as req_err:
                                        results.append({"title": item["title"], "type": item["type"], "success": False, "error": str(req_err)})
                                        print(f"[AUTOMATION ERROR] Failed to request {item['title']}: {req_err}")
                                            
                                # 1. Log event persistently
                                log_status = "Partial" if any(not r["success"] for r in results) else "Success"
                                log_sync_event(p_platform, p_country, len(items_to_sync), success_count, log_status, results)
                                
                                # 2. Dispatch webhook alert
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
            
        print(f"[AUTOMATION] Sleeping for 48 hours...")
        time.sleep(INTERVAL)


# Launch background worker once on startup inside main reloader thread
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    threading.Thread(target=run_automation_worker, daemon=True).start()


if __name__ == "__main__":
    # Start on local dev server
    app.run(host="127.0.0.1", port=5000, debug=True)
