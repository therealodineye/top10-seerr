import os
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

SEERR_URL = os.getenv("SEERR_URL")
SEERR_API_KEY = os.getenv("SEERR_API_KEY")
SEERR_EMAIL = os.getenv("SEERR_EMAIL")
SEERR_PASSWORD = os.getenv("SEERR_PASSWORD")

# Soft warning instead of hard crash at startup
missing = []
if not SEERR_URL:
    missing.append("SEERR_URL")
if not SEERR_API_KEY:
    missing.append("SEERR_API_KEY")
if not SEERR_EMAIL:
    missing.append("SEERR_EMAIL")
if not SEERR_PASSWORD:
    missing.append("SEERR_PASSWORD")

if missing:
    print(f"[WARNING] The following environment variables are missing from .env: {', '.join(missing)}. "
          f"Please configure them in the app settings drawer.", file=sys.stderr)

