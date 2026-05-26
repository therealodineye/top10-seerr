import os
import re
import requests
import urllib.parse

_CACHED_COOKIE = None

# TMDB standardized Genre IDs mapping to names
GENRES_MAP = {
    # Movie Genres
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Sci-Fi",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
    # TV Genres
    10759: "Action & Adventure",
    10762: "Kids",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics"
}

def clear_cached_cookie():
    """
    Clears the cached Overseerr session cookie. 
    Should be called when credentials change.
    """
    global _CACHED_COOKIE
    _CACHED_COOKIE = None

def get_seerr_config():
    """
    Dynamically loads Overseerr settings from the current environment variables.
    This ensures that any configuration changes written to .env and loaded are 
    immediately active without restarting the Flask process.
    """
    url = os.getenv("SEERR_URL", "").rstrip("/")
    api_key = os.getenv("SEERR_API_KEY", "")
    email = os.getenv("SEERR_EMAIL", "")
    password = os.getenv("SEERR_PASSWORD", "")
    
    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    return url, api_key, email, password, headers

def get_auth_cookie():
    """
    Authenticates with Overseerr using local auth (email/password) to get a session cookie.
    This bypasses the API Key's admin auto-approval behavior.
    """
    global _CACHED_COOKIE
    if _CACHED_COOKIE is not None:
        return _CACHED_COOKIE

    url, api_key, email, password, headers = get_seerr_config()
    
    if not url or not email or not password:
        raise ValueError("Seerr URL, Email, or Password is not configured in .env")

    auth_url = f"{url}/api/v1/auth/local"
    payload = {
        "email": email,
        "password": password
    }
    
    response = requests.post(auth_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
    response.raise_for_status()
    
    # Extract the connect.sid cookie from the response headers
    if "set-cookie" in response.headers:
        cookie_header = response.headers["set-cookie"]
        _CACHED_COOKIE = cookie_header.split(';')[0]
    elif "connect.sid" in response.cookies:
        _CACHED_COOKIE = f"connect.sid={response.cookies['connect.sid']}"
    else:
        raise ValueError("Failed to retrieve session cookie from Overseerr authentication.")
        
    return _CACHED_COOKIE

def search_media(query, media_type="movie"):
    """
    Search for a movie or TV show by title.
    Returns a dictionary containing:
      - tmdbId (int or None)
      - posterUrl (str or None)
      - skip (bool) - True if downloaded, available, pending, or declined
      - status (str) - "Available", "Requested", "Declined", or "Ready"
    or None if no match is found on Overseerr.
    """
    url, api_key, email, password, headers = get_seerr_config()
    if not url or not api_key:
        return None

    # Clean TV show titles by stripping season/series suffixes to allow high-accuracy TMDB matching
    search_query = query
    if media_type == "tv":
        search_query = re.sub(r'(?i)\s*(?::| -)?\s*season\s+\d+\s*$', '', query)
        search_query = re.sub(r'(?i)\s*(?::| -)?\s*limited\s+series\s*$', '', search_query)
        search_query = re.sub(r'(?i)\s*(?::| -)?\s*miniseries\s*$', '', search_query)
        search_query = re.sub(r'(?i)\s*(?::| -)?\s*part\s+\d+\s*$', '', search_query)
        search_query = re.sub(r'[\s:-]+$', '', search_query).strip()

    query_encoded = urllib.parse.quote(search_query)
    search_url = f"{url}/api/v1/search?query={query_encoded}"
    
    try:
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Error searching media '{query}' in Seerr: {e}")
        return None
        
    data = response.json()
    results = data.get("results", [])
    
    for result in results:
        # Check if the result matches the requested media type
        if result.get("mediaType") == media_type:
            tmdb_id = result.get("id")
            poster_path = result.get("posterPath")
            
            poster_url = None
            if poster_path:
                poster_url = f"{url}/api/v1/imageproxy/https://image.tmdb.org/t/p/w300{poster_path}"
                
            skip = False
            status_str = "Ready"
            
            # Check the mediaInfo object to see if we should skip requesting it
            media_info = result.get("mediaInfo")
            if media_info:
                status = media_info.get("status")
                # 2: PENDING, 3: PROCESSING, 4: PARTIALLY_AVAILABLE, 5: AVAILABLE
                if status in [2, 3]:
                    skip = True
                    status_str = "Requested"
                elif status in [4, 5]:
                    skip = True
                    status_str = "Available"
                    
            # If not skipped by basic status, check for declined requests
            if not skip:
                detailed_url = f"{url}/api/v1/{media_type}/{tmdb_id}"
                try:
                    detailed_response = requests.get(detailed_url, headers=headers, timeout=5)
                    if detailed_response.status_code == 200:
                        detailed_media_info = detailed_response.json().get("mediaInfo", {})
                        if detailed_media_info:
                            requests_list = detailed_media_info.get("requests", [])
                            for req in requests_list:
                                # 3: DECLINED
                                if req.get("status") == 3:
                                    skip = True
                                    status_str = "Declined"
                                    break
                except Exception as e:
                    print(f"Error fetching detailed info for TMDB ID {tmdb_id}: {e}")
                    
            # Extract year from releaseDate (movie) or firstAirDate (tv)
            date_str = result.get("releaseDate") if media_type == "movie" else result.get("firstAirDate")
            year = date_str[:4] if date_str and len(date_str) >= 4 else None

            # Extract first matching genre name
            genre_ids = result.get("genreIds", [])
            genre_names = [GENRES_MAP.get(gid) for gid in genre_ids if gid in GENRES_MAP]
            genre = genre_names[0] if genre_names else None

            return {
                "tmdbId": tmdb_id,
                "posterUrl": poster_url,
                "skip": skip,
                "status": status_str,
                "year": year,
                "genre": genre
            }
            
    return None

def get_latest_season(tmdb_id):
    """
    Fetch TV show details to find the highest valid season number.
    Excludes season 0 / specials.
    """
    url, api_key, email, password, headers = get_seerr_config()
    tv_url = f"{url}/api/v1/tv/{tmdb_id}"
    response = requests.get(tv_url, headers=headers, timeout=10)
    response.raise_for_status()
    
    data = response.json()
    seasons = data.get("seasons", [])
    
    latest_season = 0
    for season in seasons:
        season_number = season.get("seasonNumber", 0)
        # We only want valid seasons (exclude 0/specials) and keep track of the max
        if season_number > 0 and season_number > latest_season:
            latest_season = season_number
            
    return latest_season

def request_media(tmdb_id, media_type):
    """
    Request a movie or TV show using the Overseerr API.
    For TV shows, it dynamically fetches and requests only the latest season.
    Uses the authenticated user's session cookie to avoid admin auto-approval.
    """
    url, api_key, email, password, headers = get_seerr_config()
    request_url = f"{url}/api/v1/request"
    
    # Get the session cookie using local auth
    cookie = get_auth_cookie()
    
    # Use ONLY the cookie (and basic content headers) to act exactly as the user
    # Do NOT send the X-Api-Key header, as Overseerr prioritizes the API key over the cookie
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cookie": cookie
    }
    
    payload = {
        "mediaId": int(tmdb_id),
        "mediaType": media_type
    }
    
    if media_type == "tv":
        latest_season = get_latest_season(tmdb_id)
        if latest_season > 0:
            payload["seasons"] = [latest_season]
            
    response = requests.post(request_url, headers=request_headers, json=payload, timeout=10)
    response.raise_for_status()
    
    return response.json()
