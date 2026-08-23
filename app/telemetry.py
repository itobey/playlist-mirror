"""An anonymous ping, so there is some idea of how many people run this.

The rules here are the same three that govern `notify.py`, for the same reason:
nothing in this module may raise into the app, delay its startup, or appear
anywhere a user is looking. A telemetry ping is the least important thing this
container does, and it must behave like it.

What is sent is listed in full on the privacy page, and the list is short on
purpose: counts, flags, versions. **No channel, playlist or video identifiers,
no titles, no Google account, no Telegram token or chat id.** The installation
is identified by a random UUID generated on first run — not by a hash of
anything, because a hash of a public identifier can be confirmed by anyone
holding it and a list of candidates, whereas a UUID has no preimage to guess.

No new dependency: one HTTPS POST, which `urllib` has always made.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import threading
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import config, db, notify, quota, youtube
from .version import VERSION

log = logging.getLogger("playlist-mirror.telemetry")

SETTING_INSTANCE_ID = "telemetry_instance_id"
# The last send that actually succeeded. Stored rather than kept in memory so a
# container restarted daily neither pings on every boot nor stops pinging: the
# schedule survives the process.
SETTING_LAST_SENT = "telemetry_last_sent"

# How long the thread sleeps between looks. Short compared to the interval, so
# the check is against a stored timestamp rather than against an elapsed sleep.
_WAKE_SECONDS = 3600
# Not immediate: a slow DNS lookup on the telemetry host must not be the first
# thing a fresh container does. `scheduler.py` uses the same short first wait.
_FIRST_WAIT_SECONDS = 5

# The server accepts at most 32 characters for a version, and a rejected body
# comes back as a 400 that this module swallows — telemetry would simply stop,
# with nothing above debug to say why. Truncating here is the second defence;
# the first is keeping the build suffix short in `version.py`.
_MAX_VERSION = 32


# --- Instance identity -----------------------------------------------------


def instance_id() -> str:
    """This installation's opaque id, created once and reused forever.

    Read-after-write rather than read-then-write: `set_setting` upserts inside a
    single transaction, so if two threads race here they converge on whichever
    value was written first instead of each keeping its own.
    """
    row = db.get_setting(SETTING_INSTANCE_ID)
    if row is not None and (row["value"] or "").strip():
        return row["value"].strip()

    db.set_setting(SETTING_INSTANCE_ID, uuid.uuid4().hex)
    row = db.get_setting(SETTING_INSTANCE_ID)
    return (row["value"] or "").strip() if row is not None else ""


# --- Execution mode --------------------------------------------------------


def execution_mode() -> str:
    """How this instance is being run: KUBERNETES, CONTAINER or SOURCE.

    Every probe is guarded: a filesystem that cannot be read means "not a
    container", never an exception on the way to a ping nobody is waiting for.
    """
    try:
        if os.environ.get("KUBERNETES_SERVICE_HOST") or os.environ.get("KUBERNETES_SERVICE_PORT"):
            return "KUBERNETES"
        if os.environ.get("DOCKER_CONTAINER"):
            return "CONTAINER"
        if Path("/.dockerenv").exists():
            return "CONTAINER"
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
        if "docker" in cgroup or "containerd" in cgroup:
            return "CONTAINER"
    except Exception:  # noqa: BLE001 — an unreadable probe is an answer, not an error
        return "SOURCE"
    return "SOURCE"


# --- The payload -----------------------------------------------------------


def _payload() -> dict[str, Any]:
    counts = db.telemetry_counts()
    return {
        "instanceHash": instance_id(),
        "appVersion": VERSION[:_MAX_VERSION],
        "executionMode": execution_mode(),
        "pythonVersion": platform.python_version()[:_MAX_VERSION],
        "connected": youtube.is_connected(),
        "jobCount": counts["job_count"],
        "videosAdded": counts["videos_added"],
        "unitsCharged": counts["units_charged"],
        "dailyRunCount": counts["daily_run_count"],
        "quotaDailyLimit": quota.daily_limit(),
        "telegramEnabled": notify.configured(),
    }


# --- Sending ---------------------------------------------------------------


def _send(payload: dict[str, Any]) -> None:
    """POST one ping. Raises; `ping_quietly` is what the app calls."""
    # Built by hand rather than with HTTPBasicAuthHandler, which only
    # authenticates after a 401 challenge and would double every request.
    credentials = f"{config.TELEMETRY_USER}:{config.TELEMETRY_TOKEN}".encode("utf-8")
    request = urllib.request.Request(
        f"{config.TELEMETRY_URL}/api/v1/{config.TELEMETRY_USER}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
            # Set explicitly, because urllib's default of `Python-urllib/3.x` is a
            # documented bot signature that Cloudflare answers with a 403 before the
            # request reaches any origin. That 403 lands in the except below and is
            # logged at debug, so the failure mode is a telemetry stream that is
            # simply empty, with nothing anywhere saying why.
            "User-Agent": f"playlist-mirror/{VERSION[:_MAX_VERSION]}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=config.TELEMETRY_TIMEOUT) as response:
        response.read()


def _last_sent() -> Optional[datetime]:
    row = db.get_setting(SETTING_LAST_SENT)
    if row is None:
        return None
    try:
        stamp = datetime.fromisoformat(row["value"])
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _due(now: datetime) -> bool:
    last = _last_sent()
    if last is None:
        return True
    return now - last >= timedelta(hours=config.TELEMETRY_INTERVAL_HOURS)


def ping_quietly() -> None:
    """Send one ping and forget about it.

    Every failure ends here, at debug. An unreachable host, a dead DNS entry, a
    URL deliberately pointed at nothing — all of them are a normal state of this
    application, not an error anyone needs to read about.
    """
    try:
        payload = _payload()
        _send(payload)
    except Exception as error:  # noqa: BLE001 — nothing here may reach the app
        log.debug("Telemetry ping failed: %s: %s", type(error).__name__, error)
        return
    try:
        db.set_setting(SETTING_LAST_SENT, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception as error:  # noqa: BLE001
        log.debug("Could not record the telemetry timestamp: %s", error)


def ping_quietly_async() -> None:
    """Send one ping on a daemon thread without holding up the caller."""
    try:
        threading.Thread(target=ping_quietly, name="telemetry-run", daemon=True).start()
    except RuntimeError as error:
        # Possible only while Python itself is shutting down. A run has already
        # settled at this point, and telemetry must not disturb that outcome.
        log.debug("Could not start telemetry ping thread: %s", error)


# --- The thread ------------------------------------------------------------


class _Pinger:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # The startup ping is unconditional, and it happens here rather than in
        # `lifespan` so a slow DNS lookup on the telemetry host cannot hold up
        # the boot. Every wake after it defers to the stored timestamp, which is
        # what keeps a long-running container on one ping a day.
        first = True
        delay: float = _FIRST_WAIT_SECONDS
        while not self._stop.wait(delay):
            delay = _WAKE_SECONDS
            try:
                if first or _due(datetime.now(timezone.utc)):
                    ping_quietly()
            except Exception as error:  # noqa: BLE001 — a failed tick must not end the loop
                log.debug("Telemetry check failed: %s", error)
            first = False


_pinger = _Pinger()


def start() -> None:
    _pinger.start()


def stop() -> None:
    _pinger.stop()
