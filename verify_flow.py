import sys
import os
import time
import requests
import subprocess

def test_workflow():
    print("=== Starting Programmatic API Verification ===")
    
    # 1. Start Flask app in background
    print("Launching local Flask server on http://127.0.0.1:5000...")
    proc = subprocess.Popen([sys.executable, "app.py"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2.5) # Give it time to bind to the port
    
    base_url = "http://127.0.0.1:5000"
    
    try:
        # 2. Test GET /api/config (should be empty initially)
        print("\nTesting GET /api/config (initial config)...")
        res = requests.get(f"{base_url}/api/config")
        res.raise_for_status()
        config_data = res.json()
        print("Initial config:", config_data)
        
        # 3. Test POST /api/config with credentials dynamically read from local .env
        from dotenv import dotenv_values
        env = dotenv_values(".env")
        payload = {
            "SEERR_URL": env.get("SEERR_URL") or "https://placeholder-seerr-url.com/",
            "SEERR_API_KEY": env.get("SEERR_API_KEY") or "placeholder_api_key",
            "SEERR_EMAIL": env.get("SEERR_EMAIL") or "placeholder_email",
            "SEERR_PASSWORD": env.get("SEERR_PASSWORD") or "placeholder_password",
            "AUTOMATION_ENABLED": env.get("AUTOMATION_ENABLED") or "false",
            "AUTOMATION_PLATFORM": env.get("AUTOMATION_PLATFORM") or "netflix",
            "AUTOMATION_COUNTRY": env.get("AUTOMATION_COUNTRY") or "world",
            "AUTOMATION_MEDIA_TYPE": env.get("AUTOMATION_MEDIA_TYPE") or "both"
        }
        
        print("\nTesting POST /api/config (overwriting with credentials)...")
        res = requests.post(f"{base_url}/api/config", json=payload)
        res.raise_for_status()
        print("Save response:", res.json())
        
        # 4. Verify GET /api/config returns the saved values
        print("\nVerifying GET /api/config (updated config)...")
        res = requests.get(f"{base_url}/api/config")
        res.raise_for_status()
        updated_config = res.json()
        print("Updated config in app:", updated_config)
        assert updated_config["SEERR_URL"].rstrip("/") == payload["SEERR_URL"].rstrip("/")
        assert updated_config["SEERR_EMAIL"] == payload["SEERR_EMAIL"]
        
        # 5. Verify the actual .env file contents on disk
        print("\nVerifying local .env file on disk...")
        with open(".env", "r") as f:
            env_content = f.read()
        print(".env contents:\n" + env_content.strip())
        
        # 6. Test POST /api/fetch for Netflix (Norway)
        print("\nTesting POST /api/fetch for Netflix Norway (live scraper & status sync)...")
        res = requests.post(f"{base_url}/api/fetch", json={"platform": "netflix", "selection": "Norway", "movies": True, "shows": True})
        res.raise_for_status()
        fetch_data = res.json()
        
        print("\n--- Fetched Movies Seerr Statuses ---")
        for m in fetch_data.get("movies", []):
            print(f"Rank #{m['rank']}: {m['title']} | TMDB: {m['tmdbId']} | Status: {m['status']} | Requestable: {m['isRequestable']}")
            if m['posterUrl']:
                print(f"   Poster URL: {m['posterUrl'][:100]}...")
            
        print("\n--- Fetched TV Shows Seerr Statuses ---")
        for s in fetch_data.get("shows", []):
            print(f"Rank #{s['rank']}: {s['title']} | TMDB: {s['tmdbId']} | Status: {s['status']} | Requestable: {s['isRequestable']}")
            if s['posterUrl']:
                print(f"   Poster URL: {s['posterUrl'][:100]}...")
            
        print("\n=======================================================")
        print("PROGRAMMATIC API INTEGRATION CHECKS PASSED SUCCESSFULLY!")
        print("=======================================================")
        
    except Exception as e:
        print(f"\nVERIFICATION FAILED: {e}")
        try:
            stdout, stderr = proc.communicate(timeout=2)
            print("Server stdout:\n", stdout.decode())
            print("Server stderr:\n", stderr.decode())
        except Exception as comm_err:
            print("Failed to retrieve server logs:", comm_err)
        sys.exit(1)
    finally:
        print("\nShutting down test Flask server...")
        proc.terminate()
        proc.wait()

if __name__ == "__main__":
    test_workflow()
