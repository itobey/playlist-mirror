"""Runtime configuration, all overridable by environment variable.

Everything the app persists lives under DATA_DIR so a single Docker volume
covers credentials, the token and the job database.
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))

CLIENT_SECRET_FILE = Path(
    os.environ.get("CLIENT_SECRET_FILE", str(DATA_DIR / "client_secret.json"))
)
TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", str(DATA_DIR / "token.json")))
DB_FILE = Path(os.environ.get("DB_FILE", str(DATA_DIR / "transfers.sqlite3")))

APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))

# Where Google sends the user back after sign-in. Must match a redirect URI the
# OAuth client accepts. For a "Desktop app" client any http://localhost:<port>
# and http://127.0.0.1:<port> loopback URI is accepted without registration.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{APP_PORT}").rstrip("/")

# oauthlib refuses to exchange a code that came back over plain http unless this
# is set. The CLI version of this tool never hit it because run_local_server()
# sets the same variable itself. A self-hosted instance is normally reached over
# http on a LAN, so honour that scheme instead of failing the sign-in.
if PUBLIC_BASE_URL.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

SCOPES = ["https://www.googleapis.com/auth/youtube"]

# --- Quota -----------------------------------------------------------------
# The YouTube Data API grants a per-project daily allocation, default 10,000
# units, resetting at midnight Pacific Time. The API exposes no endpoint for
# remaining quota, so this app meters its own spend locally.
# Clamped to at least one insert: a zero or negative limit would make every
# allocation figure a division by zero and take the whole UI down.
QUOTA_DAILY_LIMIT = max(50, int(os.environ.get("QUOTA_DAILY_LIMIT", "10000")))

# Units held back so a run never spends the very last of the allocation on an
# insert it cannot confirm. Set to 0 to spend the allocation to the floor.
QUOTA_RESERVE = int(os.environ.get("QUOTA_RESERVE", "50"))

QUOTA_RESET_TZ = os.environ.get("QUOTA_RESET_TZ", "America/Los_Angeles")

# Published unit costs per YouTube Data API v3 operation.
COST_PLAYLIST_INSERT = 50
COST_PLAYLIST_ITEM_INSERT = 50
COST_LIST = 1
COST_SEARCH = 100

# Pause between inserts, seconds. Gentle on the per-second rate limit.
INSERT_DELAY = float(os.environ.get("INSERT_DELAY", "0.2"))

# --- Daily runs -------------------------------------------------------
# How often the daily-run runner looks for a job that may start itself.
# It is a cheap local check — the database and the local ledger, no API call —
# so a short interval costs nothing. Floored at 15 seconds so a mistyped value
# cannot turn it into a spin loop.
SCHEDULER_INTERVAL = max(15, int(os.environ.get("SCHEDULER_INTERVAL", "60")))

# --- Telegram --------------------------------------------------------------
# Both only seed the stored values, exactly like QUOTA_DAILY_LIMIT: whatever is
# set on the Telegram page is written to the database and governs from then on,
# so a self-hoster never has to edit a compose file to fix a token.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Seconds to wait on api.telegram.org before giving up. A notification that
# cannot be delivered must never hold up the next mirror.
TELEGRAM_TIMEOUT = float(os.environ.get("TELEGRAM_TIMEOUT", "15"))

PLAYLIST_PRIVACY = os.environ.get("PLAYLIST_PRIVACY", "private")

# --- Telemetry -------------------------------------------------------------
# An anonymous ping at startup, after every run, and otherwise once a day, so
# there is some idea of how many people run this and how. What it contains is
# listed in full on the privacy page — no account, channel, playlist or video
# identifiers, and the instance id is a random UUID with nothing behind it to
# guess.
#
# There is no TELEMETRY_ENABLED flag, by the same reasoning as fddb-exporter:
# knowing the install count is the point. The opt-out is to point TELEMETRY_URL
# at an address that goes nowhere, or to block the host in your network. The
# send is swallowed either way, so the app does not care whether it arrived.
TELEMETRY_URL = os.environ.get("TELEMETRY_URL", "https://telemetry.itobey.dev").rstrip("/")

# In the file on purpose, exactly as it is in the public repository. It is not a
# secret and protects nothing confidential: it keeps bots that scan for open
# POST endpoints out of the telemetry service, and being per-app means a token
# lifted from here cannot be used to forge another application's rows.
TELEMETRY_USER = "playlist-mirror"
TELEMETRY_TOKEN = os.environ.get("TELEMETRY_TOKEN", "jjzrAaw77MtAFD5VFsYGkkbNUeF4hK0Z")

# Seconds to wait on the telemetry host. Without a timeout the ping thread can
# hang on a black-holed address for the life of the container.
TELEMETRY_TIMEOUT = float(os.environ.get("TELEMETRY_TIMEOUT", "10"))

TELEMETRY_INTERVAL_HOURS = 24

# --- Release checks -------------------------------------------------------
# One read-only request to GitHub shortly after startup and once a day. Like
# telemetry, release discovery is never load-bearing: an invalid override, a
# timeout or a rate limit is swallowed by app/updates.py and changes nothing in
# the rest of the application.
UPDATE_CHECK_URL = os.environ.get(
    "UPDATE_CHECK_URL",
    "https://api.github.com/repos/itobey/playlist-mirror/releases/latest",
).strip()
UPDATE_CHECK_TIMEOUT = max(0.1, float(os.environ.get("UPDATE_CHECK_TIMEOUT", "10")))
UPDATE_CHECK_INTERVAL_HOURS = max(
    1.0, float(os.environ.get("UPDATE_CHECK_INTERVAL_HOURS", "24"))
)
UPDATE_CHECK_INITIAL_DELAY = max(
    0.0, float(os.environ.get("UPDATE_CHECK_INITIAL_DELAY", "5"))
)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
