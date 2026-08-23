"""YouTube Data API access, with every call charged to the local quota ledger.

The channel resolution, uploads enumeration and playlist operations here are the
same ones the retired CLI performed; what is new is that each call books its
published unit cost before returning, so `quota.allocation()` always reflects
what this app has actually spent today.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config, db


class QuotaExceeded(Exception):
    """The API refused a call because the project's daily allocation is gone.

    Authoritative: it overrides the locally projected allocation.
    """


class NotAuthorised(Exception):
    """No usable Google credentials on disk."""


class SourceNotFound(Exception):
    """The channel or playlist the user asked for could not be read."""


# The old name, kept so nothing that catches it silently stops catching.
ChannelNotFound = SourceNotFound


# Playlists the Data API will not serve to a third-party client, whatever scope
# is granted. Watch Later and Liked videos were closed to API reads in 2016;
# RD/UL mixes are generated per viewer and have no stable contents at all. These
# are named up front rather than discovered as a 403 halfway through a playlist mirror.
UNREADABLE_PLAYLISTS = {
    "WL": "Watch Later",
    "LL": "Liked videos",
    "HL": "History",
}

SOURCE_CHANNEL = "channel"
SOURCE_PLAYLIST = "playlist"


def is_quota_error(error: HttpError) -> bool:
    if error.resp.status not in (403, 429):
        return False
    try:
        details = json.loads(error.content.decode("utf-8"))
    except Exception:
        return False
    for item in details.get("error", {}).get("errors", []):
        if item.get("reason") in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            return item.get("reason") != "rateLimitExceeded"
    return False


def http_error_message(error: HttpError) -> str:
    try:
        details = json.loads(error.content.decode("utf-8"))
        return details.get("error", {}).get("message") or str(error)
    except Exception:
        return str(error)


# --- Credentials -----------------------------------------------------------


def has_client_secret() -> bool:
    return config.CLIENT_SECRET_FILE.exists()


def load_credentials() -> Optional[Credentials]:
    if not config.TOKEN_FILE.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_FILE), config.SCOPES)
    except Exception:
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            return None
        save_credentials(creds)
        return creds
    return None


def save_credentials(creds: Credentials) -> None:
    config.ensure_dirs()
    config.TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")


def is_connected() -> bool:
    return load_credentials() is not None


def disconnect() -> None:
    if config.TOKEN_FILE.exists():
        config.TOKEN_FILE.unlink()


def redirect_uri() -> str:
    return f"{config.PUBLIC_BASE_URL}/oauth/callback"


def build_flow(state: Optional[str] = None) -> Flow:
    flow = Flow.from_client_secrets_file(
        str(config.CLIENT_SECRET_FILE),
        scopes=config.SCOPES,
        state=state,
    )
    flow.redirect_uri = redirect_uri()
    return flow


def account_email() -> Optional[str]:
    """Best-effort identity label for the connected account, read from the
    stored token rather than by spending a quota unit."""
    if not config.TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(config.TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data.get("account") or None


# --- API client ------------------------------------------------------------


@dataclass
class Video:
    video_id: str
    title: Optional[str]
    # Set when the source lists an entry that cannot be copied at all — a
    # deleted or private video. Inserting one costs 50 units and fails, so the
    # enumeration marks it here and the run never pays for it.
    unavailable_reason: Optional[str] = None


@dataclass
class Source:
    """What a job copies from: a channel's uploads, or a playlist."""

    kind: str                 # SOURCE_CHANNEL | SOURCE_PLAYLIST
    id: str                   # channel id, or playlist id
    title: Optional[str]
    owner: Optional[str] = None   # for a playlist: the channel that owns it

    @property
    def is_playlist(self) -> bool:
        return self.kind == SOURCE_PLAYLIST


# The tabs YouTube hangs off a channel URL. They are part of the path but name
# nothing: `/@handle/videos` is still the handle's channel.
CHANNEL_TABS = {
    "videos", "shorts", "streams", "live", "playlists", "podcasts", "community",
    "featured", "about", "channels", "search", "store", "releases", "membership",
}


def url_candidate(text: str) -> str:
    """The identifying segment of a YouTube URL.

    Reading the last path segment is wrong: `/@vladimirfitness/videos` ends in a
    tab, not a name. The identifier is either an `@handle`, or the segment
    following a `channel` / `c` / `user` marker.
    """
    path = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    segments = [s for s in path.split("/") if s and ":" not in s]
    for index, segment in enumerate(segments):
        if "youtube.com" in segment or "youtu.be" in segment:
            segments = segments[index + 1:]
            break
    if not segments:
        return ""

    for segment in segments:
        if segment.startswith("@"):
            return segment
    for marker in ("channel", "c", "user"):
        if marker in segments:
            index = segments.index(marker)
            if index + 1 < len(segments):
                return segments[index + 1]
    for segment in reversed(segments):
        if segment.lower() not in CHANNEL_TABS:
            return segment
    return segments[-1]


def _flatten(name: Optional[str]) -> str:
    """A name reduced to what a handle can carry: lowercase, no @, no spaces or
    punctuation. 'Papa Meat' and '@papameat' flatten to the same thing."""
    if not name:
        return ""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _names_match(asked: str, custom_url: Optional[str], title: Optional[str]) -> bool:
    wanted = _flatten(asked)
    if not wanted:
        return False
    return wanted in (_flatten(custom_url), _flatten(title))


def parse_source_input(raw: str) -> tuple[str, str]:
    """Read the user's input as either a playlist or a channel, without
    spending a unit. Returns (kind, candidate).

    A playlist is recognised by a `list=` parameter anywhere in a URL or by a
    bare playlist id — both the playlist page and a watch URL opened from
    inside a playlist carry it.
    """
    text = raw.strip()

    if "list=" in text:
        tail = text.split("list=", 1)[1]
        for separator in ("&", "#", "?", "/"):
            tail = tail.split(separator)[0]
        if tail:
            return SOURCE_PLAYLIST, tail

    candidate = text
    if "youtube.com" in candidate or "youtu.be" in candidate:
        candidate = url_candidate(candidate)

    # A channel id also starts with UC, so it is tested first: only the
    # 24-character form is a channel, while UU… of any length is the uploads
    # playlist of one.
    if candidate.startswith("UC") and len(candidate) == 24:
        return SOURCE_CHANNEL, candidate
    if candidate[:2] in UNREADABLE_PLAYLISTS or candidate.startswith(("PL", "UU", "OL", "RD", "FL", "UL")):
        return SOURCE_PLAYLIST, candidate

    return SOURCE_CHANNEL, candidate


class Client:
    """A thin YouTube client that books every call to the quota ledger."""

    def __init__(self, job_id: Optional[int] = None) -> None:
        creds = load_credentials()
        if creds is None:
            raise NotAuthorised(
                "No valid Google credentials. Connect an account on the setup page."
            )
        self.job_id = job_id
        self.api = build("youtube", "v3", credentials=creds, cache_discovery=False)

    def _charge(self, op: str, units: int) -> None:
        db.charge(op, units, self.job_id)

    def _execute(self, request, op: str, units: int):
        """Charge first, then call: a call that fails still consumed the units,
        and undercounting the allocation is the dangerous direction to be wrong."""
        self._charge(op, units)
        try:
            return request.execute()
        except HttpError as error:
            if is_quota_error(error):
                raise QuotaExceeded(http_error_message(error)) from error
            raise

    # -- Source -------------------------------------------------------------

    def resolve_source(self, source_input: str) -> Source:
        """Resolve whatever the user pasted into the thing a job copies from.

        One unit either way: `playlists.list` for a playlist, `channels.list`
        for a channel — the same price as the old channel-only path.
        """
        kind, candidate = parse_source_input(source_input)

        if kind == SOURCE_CHANNEL:
            channel_id, title = self.resolve_channel(source_input)
            return Source(kind=SOURCE_CHANNEL, id=channel_id, title=title)

        prefix = candidate[:2]
        if prefix in UNREADABLE_PLAYLISTS:
            name = UNREADABLE_PLAYLISTS[prefix]
            raise SourceNotFound(
                f"{name} cannot be copied. YouTube's API does not serve it to any "
                "third-party app, including this one. Copy it into a normal playlist "
                "on YouTube first, then paste that playlist here."
            )
        if candidate.startswith(("RD", "UL")):
            raise SourceNotFound(
                "That is an auto-generated mix, not a playlist. YouTube builds it per "
                "viewer, so it has no fixed contents to copy."
            )

        response = self._execute(
            self.api.playlists().list(part="snippet,contentDetails", id=candidate),
            "playlists.list",
            config.COST_LIST,
        )
        items = response.get("items", [])
        if not items:
            raise SourceNotFound(
                f"No playlist '{candidate}'. A private playlist owned by someone else "
                "cannot be read; check the link, or ask its owner to set it to unlisted."
            )
        snippet = items[0]["snippet"]
        return Source(
            kind=SOURCE_PLAYLIST,
            id=items[0]["id"],
            title=snippet.get("title"),
            owner=snippet.get("channelTitle"),
        )

    # -- Channel ------------------------------------------------------------

    def resolve_channel(self, channel_input: str) -> tuple[str, Optional[str]]:
        """Accepts a channel ID, an @handle, or any channel URL.
        Returns (channel_id, channel_title)."""
        raw = channel_input.strip()
        candidate = raw

        if "youtube.com" in candidate or "youtu.be" in candidate:
            candidate = url_candidate(candidate)

        if candidate.startswith("UC") and len(candidate) == 24:
            response = self._execute(
                self.api.channels().list(part="snippet", id=candidate),
                "channels.list",
                config.COST_LIST,
            )
            items = response.get("items", [])
            title = items[0]["snippet"]["title"] if items else None
            if not items:
                raise SourceNotFound(f"No channel with ID '{candidate}'.")
            return candidate, title

        handle = candidate.lstrip("@")
        if not handle:
            raise SourceNotFound(f"Could not read a channel from '{raw}'.")

        response = self._execute(
            self.api.channels().list(part="id,snippet", forHandle=handle),
            "channels.list",
            config.COST_LIST,
        )
        items = response.get("items", [])
        if items:
            return items[0]["id"], items[0]["snippet"]["title"]

        # search.list costs 100 units — 2x an insert — so it is the last resort.
        search = self._execute(
            self.api.search().list(part="snippet", q=handle, type="channel", maxResults=1),
            "search.list",
            config.COST_SEARCH,
        )
        search_items = search.get("items", [])
        not_found = SourceNotFound(
            f"No channel found for '{raw}'. Check the handle, or open the channel "
            "on YouTube and paste the URL from the address bar."
        )
        if not search_items:
            raise not_found

        # search.list ranks by relevance, not identity: a handle it does not know
        # still returns the best keyword match, which is how a job silently binds
        # itself to a stranger's channel. Confirm the hit actually is the one
        # asked for before handing it back.
        snippet = search_items[0]["snippet"]
        channel_id = snippet["channelId"]
        confirm = self._execute(
            self.api.channels().list(part="snippet", id=channel_id),
            "channels.list",
            config.COST_LIST,
        )
        confirm_items = confirm.get("items", [])
        if not confirm_items:
            raise not_found
        confirmed = confirm_items[0]["snippet"]
        if not _names_match(handle, confirmed.get("customUrl"), confirmed.get("title")):
            raise not_found
        return channel_id, confirmed.get("title")

    def uploads_playlist_id(self, channel_id: str) -> str:
        response = self._execute(
            self.api.channels().list(part="contentDetails", id=channel_id),
            "channels.list",
            config.COST_LIST,
        )
        items = response.get("items", [])
        if not items:
            raise SourceNotFound(f"Channel '{channel_id}' not found.")
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def source_playlist_id(self, source: Source) -> str:
        """The playlist to read the videos out of. For a channel that is its
        uploads playlist — one extra unit; for a playlist it is free."""
        if source.is_playlist:
            return source.id
        return self.uploads_playlist_id(source.id)

    def iter_playlist_videos(self, playlist_id: str) -> Iterator[Video]:
        """Every entry of a playlist, in the playlist's own order.

        A curated playlist routinely contains entries that no longer resolve to
        a copyable video — the uploader deleted it, or made it private. YouTube
        still lists them, and `playlistItems.insert` still charges 50 units to
        refuse them, so they are flagged here off a part that costs nothing
        extra.
        """
        page_token = None
        while True:
            response = self._execute(
                self.api.playlistItems().list(
                    part="contentDetails,snippet,status",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ),
                "playlistItems.list",
                config.COST_LIST,
            )
            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                title = snippet.get("title")
                privacy = item.get("status", {}).get("privacyStatus")
                reason = None
                if privacy == "private" or title == "Private video":
                    reason = "Private on YouTube — cannot be copied."
                elif privacy in ("privacyStatusUnspecified", None) and title == "Deleted video":
                    reason = "Deleted from YouTube — cannot be copied."
                elif title == "Deleted video":
                    reason = "Deleted from YouTube — cannot be copied."
                yield Video(
                    video_id=item["contentDetails"]["videoId"],
                    title=title,
                    unavailable_reason=reason,
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    # The old name: a channel's uploads are a playlist like any other.
    iter_uploads = iter_playlist_videos

    # -- Playlist -----------------------------------------------------------

    def create_playlist(self, title: str, description: str = "") -> str:
        response = self._execute(
            self.api.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": title, "description": description},
                    "status": {"privacyStatus": config.PLAYLIST_PRIVACY},
                },
            ),
            "playlists.insert",
            config.COST_PLAYLIST_INSERT,
        )
        return response["id"]

    def playlist_exists(self, playlist_id: str) -> bool:
        response = self._execute(
            self.api.playlists().list(part="id", id=playlist_id),
            "playlists.list",
            config.COST_LIST,
        )
        return bool(response.get("items"))

    def iter_playlist_video_ids(self, playlist_id: str) -> Iterator[str]:
        """Reconciliation before a resume: 1 unit per 50 items, versus 50 units
        for a duplicate insert. Always worth it."""
        page_token = None
        while True:
            response = self._execute(
                self.api.playlistItems().list(
                    part="contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ),
                "playlistItems.list",
                config.COST_LIST,
            )
            for item in response.get("items", []):
                yield item["contentDetails"]["videoId"]
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        self._execute(
            self.api.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ),
            "playlistItems.insert",
            config.COST_PLAYLIST_ITEM_INSERT,
        )
