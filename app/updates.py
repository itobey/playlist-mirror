"""Discover newer stable Playlist Mirror releases without burdening the app.

The checker makes one small public GitHub API request after startup and then
once a day. It owns its network, parsing and scheduling failures completely: a
release notice is useful, but copying playlists must never depend on one.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from . import config
from .version import VERSION

log = logging.getLogger("playlist-mirror.updates")

_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RELEASE_PATH_PREFIX = "/itobey/playlist-mirror/releases/tag/"


@dataclass(frozen=True)
class AvailableUpdate:
    """The only release data the interface is allowed to consume."""

    version: str
    url: str


_state_lock = threading.Lock()
_available: Optional[AvailableUpdate] = None


def _version_parts(value: str, *, running: bool = False) -> Optional[tuple[int, int, int]]:
    """Return a strict release-workflow version, or ``None`` if it is not one."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    if running:
        # Builds from master report 0.1.3+master.a1b2c3d. They contain newer
        # code than 0.1.3, but their release base is still 0.1.3.
        normalized = normalized.split("+", 1)[0]
    if not _VERSION_RE.fullmatch(normalized):
        return None
    major, minor, patch = (int(part) for part in normalized.split("."))
    return major, minor, patch


def is_newer_version(current: str, candidate: str) -> bool:
    """Whether a stable candidate is newer than the running release base."""
    current_parts = _version_parts(current, running=True)
    candidate_parts = _version_parts(candidate)
    return bool(current_parts and candidate_parts and candidate_parts > current_parts)


def _release_url(value: Any) -> Optional[str]:
    """Accept only this project's HTTPS release pages as browser destinations."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or not parsed.path.startswith(_RELEASE_PATH_PREFIX)
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value.strip()


def _fetch_latest_release() -> dict[str, Any]:
    request = urllib.request.Request(
        config.UPDATE_CHECK_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"playlist-mirror/{VERSION}",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=config.UPDATE_CHECK_TIMEOUT) as response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if status != 200:
            raise RuntimeError(f"GitHub returned HTTP {status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub release response was not an object")
    return payload


def _stable_release(payload: dict[str, Any]) -> Optional[AvailableUpdate]:
    if payload.get("draft") is True or payload.get("prerelease") is True:
        return None
    tag = payload.get("tag_name")
    url = _release_url(payload.get("html_url"))
    if not isinstance(tag, str) or _version_parts(tag) is None or url is None:
        return None
    # Release tags are deliberately limited to a numeric version with an
    # optional leading v, so the browser destination can and should identify
    # the exact same tag the notice names.
    if urlsplit(url).path != f"{_RELEASE_PATH_PREFIX}{tag.strip()}":
        return None
    return AvailableUpdate(version=tag.strip(), url=url)


def check_for_update() -> None:
    """Refresh the cached result once, swallowing every external failure."""
    try:
        release = _stable_release(_fetch_latest_release())
    except Exception as error:  # noqa: BLE001 — this feature may never reach the app
        log.debug("Release check failed: %s: %s", type(error).__name__, error)
        return

    # An unusable payload is not evidence that an already found release stopped
    # existing. Retain the last good result, just as a network failure does.
    if release is None:
        log.debug("GitHub returned no usable stable release")
        return

    global _available
    if is_newer_version(VERSION, release.version):
        with _state_lock:
            _available = release
        log.info("New version %s available: %s", release.version, release.url)
    else:
        with _state_lock:
            _available = None
        log.debug("Playlist Mirror %s is up to date (latest: %s)", VERSION, release.version)


def available_update() -> Optional[AvailableUpdate]:
    """Return the immutable cached notice state; never performs I/O."""
    with _state_lock:
        return _available


class _Checker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="release-check",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Waking an Event.wait is immediate. Keep the join bounded as defence
        # for the rare case where shutdown catches the thread inside urlopen.
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    def _loop(self) -> None:
        delay = config.UPDATE_CHECK_INITIAL_DELAY
        while not self._stop.wait(delay):
            check_for_update()
            delay = config.UPDATE_CHECK_INTERVAL_HOURS * 3600


_checker = _Checker()


def start() -> None:
    _checker.start()


def stop() -> None:
    _checker.stop()
