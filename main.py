import sys
import scraper
import seerr

def process_media_list(media_list, media_type):
    """
    Iterates over a list of titles, searches them in Overseerr, 
    and sends a request if they aren't already available or pending.
    """
    for title in media_list:
        print(f"Processing {media_type}: '{title}'...")
        try:
            search_result = seerr.search_media(title, media_type)
            
            if not search_result:
                print(f"  -> [WARNING] Could not find {media_type} '{title}' in Overseerr. Skipping.")
                continue
                
            if search_result.get("skip"):
                print(f"  -> [INFO] '{title}' is already requested or available. Skipping.")
                continue
            
            tmdb_id = search_result["tmdbId"]
            print(f"  -> [ACTION] Requesting '{title}' (TMDB ID: {tmdb_id})...")
            
            seerr.request_media(tmdb_id, media_type)
            print(f"  -> [SUCCESS] Successfully requested '{title}'.")
            
        except Exception as e:
            print(f"  -> [ERROR] Failed to process '{title}': {e}")

def main():
    print("Scraping Netflix Top 10 (Norway)...")
    try:
        movies, tv_shows = scraper.scrape_top_10()
    except Exception as e:
        print(f"Failed to scrape Netflix: {e}")
        sys.exit(1)
        
    print(f"\nFound {len(movies)} movies and {len(tv_shows)} TV shows.\n")
    
    print("--- Processing Movies ---")
    process_media_list(movies, "movie")
    
    print("\n--- Processing TV Shows ---")
    process_media_list(tv_shows, "tv")
    
    print("\nDone syncing Netflix Top 10 with Overseerr!")

if __name__ == "__main__":
    main()
