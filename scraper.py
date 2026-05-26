import os
import re
import json
import time
import datetime
from bs4 import BeautifulSoup
from curl_cffi import requests

# TMDB standardized Genre IDs mapping to names (standard fallback cache)
GENRES_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics"
}

PLATFORMS = {
    "netflix": "Netflix",
    "amazon-prime": "Amazon Prime",
    "disney": "Disney+",
    "hbo-max": "HBO Max",
    "apple-tv": "Apple TV+",
    "paramount-plus": "Paramount+"
}

# Resolve paths safely
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COUNTRIES_JSON_PATH = os.path.join(BASE_DIR, "platform_countries.json")

# Load platform country mappings statically compiled
PLATFORM_COUNTRIES = {}
if os.path.exists(COUNTRIES_JSON_PATH):
    try:
        with open(COUNTRIES_JSON_PATH, "r") as f:
            PLATFORM_COUNTRIES = json.load(f)
    except Exception as e:
        print(f"[SCRAPER WARNING] Failed to load platform_countries.json: {e}")

# If empty or loading failed, fallback to standard curated list for robust execution
if not PLATFORM_COUNTRIES:
    standard_fallback = [
        {"name": "Argentina", "slug": "argentina"},
        {"name": "Australia", "slug": "australia"},
        {"name": "Austria", "slug": "austria"},
        {"name": "Belgium", "slug": "belgium"},
        {"name": "Brazil", "slug": "brazil"},
        {"name": "Canada", "slug": "canada"},
        {"name": "Denmark", "slug": "denmark"},
        {"name": "Finland", "slug": "finland"},
        {"name": "France", "slug": "france"},
        {"name": "Germany", "slug": "germany"},
        {"name": "Japan", "slug": "japan"},
        {"name": "Norway", "slug": "norway"},
        {"name": "Spain", "slug": "spain"},
        {"name": "Sweden", "slug": "sweden"},
        {"name": "United Kingdom", "slug": "united-kingdom"},
        {"name": "United States", "slug": "united-states"}
    ]
    PLATFORM_COUNTRIES = {p: list(standard_fallback) for p in PLATFORMS.keys()}

# Prepend the global 'World' option to every platform's countries list dynamically
for p in PLATFORM_COUNTRIES.keys():
    # Make sure we don't duplicate World
    if not any(c["slug"] == "world" for c in PLATFORM_COUNTRIES[p]):
        PLATFORM_COUNTRIES[p].insert(0, {"name": "World (Global)", "slug": "world"})


def slugify_country(country_name):
    """
    Standard slugifier to convert names like 'United States' to 'united-states'
    """
    name = country_name.strip().lower()
    name = name.replace("ü", "u").replace("é", "e").replace("ó", "o").replace("á", "a")
    name = re.sub(r'[^a-z0-9\s-]', '', name)
    name = re.sub(r'[\s-]+', '-', name)
    return name


def get_titles_flixpatrol(platform, country_slug):
    """
    Tries to scrape the Top 10 movies and TV shows from FlixPatrol.
    Implements a sequential fallback look-back up to 5 days.
    """
    base_url = "https://flixpatrol.com/top10"
    today = datetime.date.today()
    
    movies = []
    tv_shows = []
    
    # We will try the current date first, then go back up to 5 days
    for i in range(6): 
        target_date = today - datetime.timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")
        
        # Schema: https://flixpatrol.com/top10/{platform}/{country}/{date}/
        url = f"{base_url}/{platform}/{country_slug}/{date_str}/"
        print(f"[SCRAPER] Fetching: {platform} | Country: {country_slug} | Date: {date_str} (Attempt {i+1}/6)...")
        
        # Inject defensive 3-second delay between separate network attempts (requirement 2)
        if i > 0:
            print("[SCRAPER] Defensively waiting 3 seconds before fallback attempt...")
            time.sleep(3)
            
        try:
            # impersonate="chrome" safely bypasses Cloudflare TLS/JA3 verification (requirement 1)
            res = requests.get(url, impersonate="chrome", timeout=15)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                
                # Check for headings first (defensive exact mapping)
                for heading in soup.find_all(['h2', 'h3', 'div']):
                    text = heading.text.strip().lower()
                    if text == 'top 10 movies':
                        table = heading.find_next('table', class_='card-table')
                        if table:
                            movies = []
                            for row in table.find_all('tr'):
                                link = row.find('a')
                                if link:
                                    movies.append(link.text.strip())
                    elif text == 'top 10 tv shows':
                        table = heading.find_next('table', class_='card-table')
                        if table:
                            tv_shows = []
                            for row in table.find_all('tr'):
                                link = row.find('a')
                                if link:
                                    tv_shows.append(link.text.strip())
                                    
                # Fallback: if headings didn't yield at least some data, grab first two card-tables
                if not movies or not tv_shows:
                    card_tables = soup.find_all('table', class_='card-table')
                    if len(card_tables) >= 2:
                        if not movies:
                            for row in card_tables[0].find_all('tr'):
                                link = row.find('a')
                                if link:
                                    movies.append(link.text.strip())
                        if not tv_shows:
                            for row in card_tables[1].find_all('tr'):
                                link = row.find('a')
                                if link:
                                    tv_shows.append(link.text.strip())
                                    
                # If we successfully scraped at least one of the lists, we return immediately
                if movies or tv_shows:
                    print(f"[SCRAPER] Success on {date_str}! (Movies: {len(movies)}, Shows: {len(tv_shows)})")
                    return movies[:10], tv_shows[:10]
            else:
                print(f"[SCRAPER WARNING] Non-200 response on {date_str}: {res.status_code}")
        except Exception as e:
            print(f"[SCRAPER WARNING] Exception during fetch on {date_str}: {e}")
            
    print(f"[SCRAPER ERROR] Completely failed to retrieve FlixPatrol charts for {platform}/{country_slug} across 5 days look-back.")
    return [], []


def scrape_top_10(platform="netflix", selection="norway"):
    """
    Overhauled to leverage the FlixPatrol scraper.
    """
    platform = platform.lower().strip()
    country_slug = slugify_country(selection)
    
    # Map friendly selection names back to slugs if they are found in list
    if platform in PLATFORM_COUNTRIES:
        for c in PLATFORM_COUNTRIES[platform]:
            if c["name"].lower() == selection.lower():
                country_slug = c["slug"]
                break
                
    return get_titles_flixpatrol(platform, country_slug)


if __name__ == "__main__":
    import sys
    platform = sys.argv[1] if len(sys.argv) > 1 else "netflix"
    country = sys.argv[2] if len(sys.argv) > 2 else "norway"
    
    print(f"Scraping FlixPatrol Top 10 for Platform: {platform} | Country: {country}")
    movies, tv_shows = scrape_top_10(platform, country)
    
    print(f"--- Top 10 Movies ({len(movies)}) ---")
    for i, title in enumerate(movies, 1):
        print(f"{i}. {title}")
        
    print(f"\n--- Top 10 TV Shows ({len(tv_shows)}) ---")
    for i, title in enumerate(tv_shows, 1):
        print(f"{i}. {title}")
