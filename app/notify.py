"""Telegram delivery for daily runs.

Notification exists for one reason: a daily run runs while nobody is
looking at the page, so the only way to learn what it did is to be told. Every
line below follows from that.

- One message, not one per job. A credit day's automatic runs are a single
  event to the person receiving them; three separate pings for three jobs is
  three interruptions reporting one thing.
- Plain text, no parse mode. A playlist title is arbitrary user text that will
  eventually contain an underscore, an asterisk or a `<`, and a message that
  fails to send because of Markdown escaping is worse than a plain one.
- Nothing here may raise into the runner. A playlist mirror that copied 50 videos
  succeeded whether or not Telegram was reachable; the failure is recorded and
  shown on the Telegram page, and the mirror keeps its own outcome.

No new dependency: this is two HTTPS calls, which `urllib` has always made.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, db, logs

log = logs.get("telegram")

API_ROOT = "https://api.telegram.org"

SETTING_BOT_TOKEN = "telegram_bot_token"
SETTING_CHAT_ID = "telegram_chat_id"
# The outcome of the last delivery attempt, as JSON. Kept because a notification
# fails silently by its nature — the whole point is that nobody was watching —
# so the page has to be able to say "the last message did not arrive, and here
# is what Telegram said".
SETTING_LAST_RESULT = "telegram_last_result"


class NotifyError(Exception):
    """A delivery that did not happen, carrying what to do about it."""


# --- Stored settings -------------------------------------------------------


def _stored(key: str, fallback: str) -> str:
    row = db.get_setting(key)
    if row is None:
        return fallback
    return (row["value"] or "").strip()


def bot_token() -> str:
    return _stored(SETTING_BOT_TOKEN, config.TELEGRAM_BOT_TOKEN)


def chat_id() -> str:
    return _stored(SETTING_CHAT_ID, config.TELEGRAM_CHAT_ID)


def token_from_container() -> bool:
    return db.get_setting(SETTING_BOT_TOKEN) is None and bool(config.TELEGRAM_BOT_TOKEN)


def chat_from_container() -> bool:
    return db.get_setting(SETTING_CHAT_ID) is None and bool(config.TELEGRAM_CHAT_ID)


def set_bot_token(value: str) -> None:
    db.set_setting(SETTING_BOT_TOKEN, value.strip())


def set_chat_id(value: str) -> None:
    db.set_setting(SETTING_CHAT_ID, value.strip())


def configured() -> bool:
    return bool(bot_token()) and bool(chat_id())


def masked_token() -> Optional[str]:
    """Enough of the token to recognise it by, and no more.

    The bot id before the colon is not secret — it is in the bot's own username
    — and the last four characters are how a person tells two tokens apart. The
    middle never reaches the page, so a screenshot of this instance's settings
    is not a leak.
    """
    token = bot_token()
    if not token:
        return None
    head, _, tail = token.partition(":")
    if not tail:
        return "…" + token[-4:]
    return f"{head}:{'·' * 8}{tail[-4:]}"


def last_result() -> Optional[dict[str, Any]]:
    row = db.get_setting(SETTING_LAST_RESULT)
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def _record(ok: bool, detail: str) -> None:
    # Every outcome the Telegram page shows is also said here, so the reason a
    # message never arrived is in the container log next to the run that owed it.
    if ok:
        log.info("Telegram: %s", detail)
    else:
        log.warning("Telegram: %s", detail)
    db.set_setting(
        SETTING_LAST_RESULT,
        json.dumps(
            {
                "ok": ok,
                "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        ),
    )


# --- The API ---------------------------------------------------------------


def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    token = bot_token()
    if not token:
        raise NotifyError("No bot token is set, so there is nothing to send with.")
    url = f"{API_ROOT}/bot{token}/{method}"
    body = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.TELEGRAM_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Telegram puts the useful sentence in the body of its 4xx responses,
        # never in the status line, so a bare "400 Bad Request" would throw away
        # the only part worth showing anyone.
        detail = _describe(error)
        raise NotifyError(detail) from error
    except urllib.error.URLError as error:
        raise NotifyError(
            f"Could not reach api.telegram.org ({error.reason}). Check this container's "
            "network access."
        ) from error
    except (TimeoutError, OSError) as error:
        raise NotifyError(f"Could not reach api.telegram.org ({error}).") from error
    except ValueError as error:
        raise NotifyError(f"Telegram sent something that is not JSON ({error}).") from error

    if not payload.get("ok"):
        raise NotifyError(_humanise(str(payload.get("description", "Telegram refused the call."))))
    return payload


def _describe(error: urllib.error.HTTPError) -> str:
    try:
        parsed = json.loads(error.read().decode("utf-8"))
        described = str(parsed.get("description", "")).strip()
    except Exception:  # noqa: BLE001 — an unparseable body is still an error to report
        described = ""
    if not described:
        return f"Telegram answered {error.code}."
    return _humanise(described)


def _humanise(description: str) -> str:
    """Telegram's three common refusals, each with the fix beside it.

    They are the whole of what goes wrong here in practice, and each one has a
    different answer — a wrong token, a bot that has never been spoken to, and
    a chat id that is not this bot's.
    """
    lowered = description.lower()
    if "unauthorized" in lowered:
        return (
            "Telegram rejected the bot token (unauthorized). Copy it again from @BotFather — "
            "it is the whole line, digits, colon and all."
        )
    if "chat not found" in lowered:
        return (
            "Telegram has no chat with that id for this bot. Open the bot in Telegram and send "
            "it any message first, then use Find my chat below."
        )
    if "bot was blocked" in lowered or "bot can't initiate" in lowered:
        return (
            "This bot cannot write to you: it was blocked, or you have never messaged it. Send "
            "the bot a message in Telegram, then try again."
        )
    return f"Telegram refused the message: {description}"


def record_unsent(detail: str) -> None:
    """Note a message that was owed and could not be sent at all.

    Not the same as a delivery that failed at Telegram: nothing was attempted,
    because there was nothing to attempt it with. It still belongs on the record
    — a user waiting for a report they will never receive is the worst outcome
    this page can produce.
    """
    _record(False, detail)


def send(text: str) -> None:
    """Deliver one message, recording the outcome either way."""
    target = chat_id()
    if not target:
        raise NotifyError("No chat is set, so there is nowhere to send to.")
    try:
        _call(
            "sendMessage",
            {
                "chat_id": target,
                "text": text,
                "disable_web_page_preview": "true",
            },
        )
    except NotifyError as error:
        _record(False, str(error))
        raise
    _record(True, "Message delivered.")


def try_send(text: str) -> Optional[str]:
    """`send` for the daily-run runner, which has no one to raise to.

    Returns the failure as a string, or None. The playlist mirror it reports on has
    already happened and is already recorded; an unreachable Telegram must not
    turn a successful copy into an error anywhere.
    """
    try:
        send(text)
    except NotifyError as error:
        return str(error)
    except Exception as error:  # noqa: BLE001 — nothing here may reach the runner
        detail = f"{type(error).__name__}: {error}"
        _record(False, detail)
        return detail
    return None


def discover_chat() -> tuple[Optional[str], Optional[str]]:
    """Read the chat id off the most recent message sent to the bot.

    Telegram never tells you your own chat id; every guide answers it with "add
    a third-party bot", which for a self-hoster means handing a stranger's bot
    the conversation. The bot's own getUpdates already carries it, so this app
    asks for it directly: message your bot, press the button, done.

    Returns (chat id, a human label for it).
    """
    payload = _call("getUpdates", {"limit": "20", "timeout": "0"})
    updates = payload.get("result") or []
    for update in reversed(updates):
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or {}
        )
        chat = message.get("chat") or {}
        if chat.get("id") is None:
            continue
        label = (
            chat.get("username")
            and f"@{chat['username']}"
            or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            )
            or chat.get("title")
            or None
        )
        return str(chat["id"]), label
    return None, None


# --- The message -----------------------------------------------------------


# A glyph carries the status faster than a word does, and a phone notification
# is read at a glance or not at all. The sentence explaining what happens next
# lived here once; it said the same thing every day and pushed the figures off
# the preview line, so the icon replaces it and the page keeps the prose.
STATUS_ICONS = {
    db.COMPLETE: "✅",
    db.HELD_QUOTA: "🪫",
    db.HELD_LIMIT: "⏸",
    db.FAILED: "❌",
    db.CANCELLED: "⏹",
    db.PENDING: "⏳",
    db.RUNNING: "⏳",
}


@dataclass
class RunReport:
    """What one automatic run of one job did, as the message needs it."""

    ref: str
    playlist_title: str
    added: int
    failed: int
    remaining: int
    units: int
    status: str
    playlist_url: Optional[str]


def compose(reports: list[RunReport], credits_line: str) -> str:
    """One message for the whole credit day's automatic runs.

    Ordered so the answer to "did anything happen?" is the first line and the
    answer to "do I need to do anything?" is the last. Terse on purpose: this
    arrives as a phone notification, where everything past the third line is
    only read by someone who already decided to open it.
    """
    count = len(reports)
    total_added = sum(report.added for report in reports)
    blocks = [
        f"🎬 {count} order{'' if count == 1 else 's'} · "
        f"+{total_added:,} video{'' if total_added == 1 else 's'}"
    ]

    for report in reports:
        icon = STATUS_ICONS.get(report.status, "•")
        figures = [f"+{report.added:,}"]
        if report.failed:
            figures.append(f"{report.failed:,} failed")
        if report.remaining:
            figures.append(f"{report.remaining:,} left")
        figures.append(f"{report.units:,}u")
        lines = [
            f"{icon} #{report.ref} {report.playlist_title}",
            "   " + " · ".join(figures),
        ]
        if report.playlist_url:
            lines.append("   " + report.playlist_url)
        blocks.append("\n".join(lines))

    blocks.append(credits_line)
    return "\n\n".join(blocks)


def test_message() -> str:
    return (
        "🎬 Playlist Mirror — test message\n\n"
        "✅ Telegram is set up. Daily runs report here once per credit day, "
        "covering every job that ran."
    )
