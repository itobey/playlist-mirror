"""SQLite persistence.

Two things must survive a crash, a closed tab or a stopped container: which
videos of a job have already landed in the playlist, and how many quota units
have been spent today. Both are written as they happen, never batched at the
end, so resuming is always exact.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    status              TEXT NOT NULL,
    source_input        TEXT NOT NULL,
    source_kind         TEXT NOT NULL DEFAULT 'channel',
    source_id           TEXT,
    source_title        TEXT,
    source_owner        TEXT,
    playlist_id         TEXT,
    playlist_title      TEXT NOT NULL,
    playlist_description TEXT NOT NULL DEFAULT '',
    total_videos        INTEGER NOT NULL DEFAULT 0,
    units_charged       INTEGER NOT NULL DEFAULT 0,
    -- A ceiling the user set on how many videos one run may add before it
    -- holds. NULL means no ceiling: the run goes until the credits do.
    run_limit           INTEGER,
    -- A daily run: the job may start itself, once per credit day, as soon
    -- as the day's free credits allow it. Off by default, and off is what every
    -- job recorded before this existed keeps.
    scheduled           INTEGER NOT NULL DEFAULT 0,
    -- Whether that automatic run is reported on Telegram. Only meaningful with
    -- a daily run, because nothing else runs unwatched.
    notify              INTEGER NOT NULL DEFAULT 0,
    -- When the daily run last fired. Compared against the start of the
    -- current credit day, this is what holds a daily run to one run per
    -- day: written before the run starts, so a crash mid-run cannot make the
    -- job fire again a minute later.
    last_auto_run_at    TEXT,
    message             TEXT,
    started_at          TEXT,
    finished_at         TEXT
);

CREATE TABLE IF NOT EXISTS job_videos (
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    video_id    TEXT NOT NULL,
    title       TEXT,
    state       TEXT NOT NULL DEFAULT 'pending',
    message     TEXT,
    settled_at  TEXT,
    PRIMARY KEY (job_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_job_videos_job_pos ON job_videos(job_id, position);
CREATE INDEX IF NOT EXISTS idx_job_videos_state ON job_videos(job_id, state);

CREATE TABLE IF NOT EXISTS quota_ledger (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    spent_at TEXT NOT NULL,
    op      TEXT NOT NULL,
    units   INTEGER NOT NULL,
    job_id  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_quota_spent_at ON quota_ledger(spent_at);

-- Instance configuration the operator can change without restarting the
-- container. Environment variables seed these on first use; a stored value
-- governs from then on.
CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    set_at  TEXT NOT NULL
);
"""

# Job status values.
PENDING = "pending"
RUNNING = "running"
HELD_QUOTA = "held_quota"
# Held on the user's own ceiling rather than on the credits. Kept apart from
# HELD_QUOTA and CANCELLED because the answer to "why did it stop, and when can
# I continue?" is different for all three: the wall waits for the reset, a stop
# was pressed, and this one can be resumed immediately.
HELD_LIMIT = "held_limit"
COMPLETE = "complete"
FAILED = "failed"
CANCELLED = "cancelled"

# The word each status is printed under. It lives beside the statuses because
# three surfaces need the same one — the register, the live stream, and the
# Telegram report — and a status whose name differs between them is a status
# the user has to translate.
STATUS_LABELS = {
    PENDING: "SUBMITTED",
    RUNNING: "RUNNING",
    HELD_QUOTA: "PAUSED — NO CREDITS LEFT",
    HELD_LIMIT: "PAUSED — LIMIT REACHED",
    COMPLETE: "COMPLETE",
    FAILED: "FAILED",
    CANCELLED: "STOPPED",
}

TERMINAL_STATUSES = (COMPLETE, FAILED, CANCELLED)
RESUMABLE_STATUSES = (PENDING, HELD_QUOTA, HELD_LIMIT, FAILED, CANCELLED)

# Source kinds.
SOURCE_CHANNEL = "channel"
SOURCE_PLAYLIST = "playlist"

# Per-video states.
V_PENDING = "pending"
V_ADDED = "added"
V_FAILED = "failed"
# Listed by the source but not copyable at all — deleted or private. Terminal:
# unlike a failed insert, a resume must not retry it and pay 50 units to be
# refused again.
V_SKIPPED = "skipped"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """One connection per thread; the job runner and the request handlers each
    get their own."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.ensure_dirs()
        conn = sqlite3.connect(config.DB_FILE, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema in place.

    A job used to be able to copy only a channel, so its source columns were
    named for one. They are renamed rather than replaced: every existing job
    keeps its history, its per-video state, and its playlist.
    """

    def columns() -> set[str]:
        return {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}

    renames = (
        ("channel_input", "source_input"),
        ("channel_id", "source_id"),
        ("channel_title", "source_title"),
    )
    for old, new in renames:
        cols = columns()
        if old in cols and new not in cols:
            conn.execute(f"ALTER TABLE jobs RENAME COLUMN {old} TO {new}")

    cols = columns()
    if "source_kind" not in cols:
        # Everything recorded before playlists existed was a channel.
        conn.execute(
            f"ALTER TABLE jobs ADD COLUMN source_kind TEXT NOT NULL DEFAULT '{SOURCE_CHANNEL}'"
        )
    if "source_owner" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN source_owner TEXT")
    if "run_limit" not in cols:
        # Jobs recorded before the ceiling existed simply have none.
        conn.execute("ALTER TABLE jobs ADD COLUMN run_limit INTEGER")
    if "scheduled" not in cols:
        # Nothing on an existing register asked to run unattended, so no job
        # gains a daily run by being migrated into a version that has them.
        conn.execute("ALTER TABLE jobs ADD COLUMN scheduled INTEGER NOT NULL DEFAULT 0")
    if "notify" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN notify INTEGER NOT NULL DEFAULT 0")
    if "last_auto_run_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN last_auto_run_at TEXT")


# --- Jobs ------------------------------------------------------------------


def create_job(
    source_input: str,
    playlist_title: str,
    playlist_description: str = "",
    run_limit: Optional[int] = None,
) -> int:
    now = utcnow()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO jobs (created_at, updated_at, status, source_input,
                                 playlist_title, playlist_description, run_limit)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, now, PENDING, source_input, playlist_title, playlist_description, run_limit),
        )
        return int(cur.lastrowid)


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with tx() as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?",
            (*fields.values(), job_id),
        )


def get_job(job_id: int) -> Optional[sqlite3.Row]:
    return connect().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs(limit: int = 50) -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# What a deleted job leaves behind, so the anonymous ping keeps reporting
# lifetime totals rather than whatever the register happens to hold right now.
# Without these, an instance with "remove completed jobs" switched on reports
# zeroes forever: the rows the counts are read from are the rows that were
# deleted. Kept in `settings` rather than in a table of their own — three
# integers, written once per deletion, read once a day.
REMOVED_JOBS_KEY = "telemetry_removed_jobs"
REMOVED_VIDEOS_ADDED_KEY = "telemetry_removed_videos_added"
REMOVED_UNITS_KEY = "telemetry_removed_units"


def _counter(conn: sqlite3.Connection, key: str) -> int:
    """One banked counter, on a caller-supplied connection.

    Takes the connection rather than calling `get_setting`, because both readers
    of this run inside a transaction and `tx()` is not reentrant.
    """
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _bank(conn: sqlite3.Connection, key: str, amount: int) -> None:
    if amount <= 0:
        return
    conn.execute(
        "INSERT INTO settings (key, value, set_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, set_at = excluded.set_at",
        (key, str(_counter(conn, key) + amount), utcnow()),
    )


def delete_job(job_id: int) -> None:
    with tx() as conn:
        # Banked before the rows go, in the same transaction as the deletion:
        # either the job is gone and its figures are kept, or neither happened.
        job = conn.execute("SELECT units_charged FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is not None:
            added = conn.execute(
                "SELECT COUNT(*) AS n FROM job_videos WHERE job_id = ? AND state = ?",
                (job_id, V_ADDED),
            ).fetchone()
            _bank(conn, REMOVED_JOBS_KEY, 1)
            _bank(conn, REMOVED_VIDEOS_ADDED_KEY, int(added["n"]))
            _bank(conn, REMOVED_UNITS_KEY, int(job["units_charged"]))
        conn.execute("DELETE FROM job_videos WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


# --- Removing a job when it completes ---------------------------------------
# An operator who mirrors a channel a week does not want a register of finished
# jobs to scroll past, so this takes a completed job off it on their say-so.
#
# Two rules make it safe to do without asking each time:
#
# 1. **Only a completion.** Nothing paused, stopped or failed is ever removed —
#    those are jobs with something left to do, and the register is where the
#    user finds them again.
# 2. **Only a clean one.** A completion carrying failed videos is a job whose
#    operator still has a decision to make (resume and retry them, or accept the
#    gap), so it stays. Skipped videos do not count against this: nothing can
#    copy a video the source has deleted, and a job that is only missing those
#    is finished in every sense that is available to it.
#
# What is removed is the record, never the playlist. The copy on YouTube is the
# thing the user came for and it is not this app's to delete.

AUTO_REMOVE_KEY = "auto_remove_completed"


def auto_remove_completed() -> bool:
    row = get_setting(AUTO_REMOVE_KEY)
    return bool(row and row["value"] == "1")


def set_auto_remove_completed(on: bool) -> None:
    set_setting(AUTO_REMOVE_KEY, "1" if on else "")


def is_removable(job_id: int, status: str) -> bool:
    """Whether this job is one the register may take away by itself."""
    if status != COMPLETE:
        return False
    return video_counts(job_id)[V_FAILED] == 0


def removable_completed() -> list[sqlite3.Row]:
    """Every completed job carrying no failed videos, oldest first.

    Read in one query rather than by filtering `list_jobs`, because switching
    the setting on sweeps the whole register and not just the fifty records the
    page happens to print.
    """
    return connect().execute(
        """SELECT * FROM jobs
           WHERE status = ?
             AND NOT EXISTS (SELECT 1 FROM job_videos v
                             WHERE v.job_id = jobs.id AND v.state = ?)
           ORDER BY id""",
        (COMPLETE, V_FAILED),
    ).fetchall()


# --- Daily runs -------------------------------------------------------
# Statuses a daily run is allowed to start from. Deliberately narrower than
# RESUMABLE_STATUSES, which also holds FAILED: a fault is the one outcome that
# needs a person to look at it, and a job that fails on every attempt would
# otherwise re-fire against the same fault every credit day, spending real units
# to reach the same wall. It stays on the register, ticked, and says it is
# waiting for its operator.
AUTO_STATUSES = (PENDING, HELD_QUOTA, HELD_LIMIT, CANCELLED)


def set_schedule(job_id: int, scheduled: bool, notify: bool) -> None:
    """Notification is stored as the user set it, but never on its own: it
    describes an automatic run, so without a daily run there is nothing
    for it to describe and the form cannot show it either."""
    update_job(
        job_id,
        scheduled=1 if scheduled else 0,
        notify=1 if (scheduled and notify) else 0,
    )


def standing_orders() -> list[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM jobs WHERE scheduled = 1 ORDER BY id"
    ).fetchall()


def due_standing_orders(since_iso: str) -> list[sqlite3.Row]:
    """Daily runs that may fire in the credit day beginning at `since_iso`.

    A job qualifies when it is ticked, is in a state a run can start from, has
    not already fired in this credit day, and actually has something left to
    add. The last clause is checked against the video table rather than the
    job's counters, because a job whose source was never enumerated has counters
    that read zero and nothing an automatic run could do about it.
    """
    placeholders = ",".join("?" * len(AUTO_STATUSES))
    return connect().execute(
        f"""SELECT * FROM jobs
            WHERE scheduled = 1
              AND status IN ({placeholders})
              AND (last_auto_run_at IS NULL OR last_auto_run_at < ?)
              AND EXISTS (SELECT 1 FROM job_videos v
                          WHERE v.job_id = jobs.id AND v.state IN (?, ?))
            ORDER BY id""",
        (*AUTO_STATUSES, since_iso, V_PENDING, V_FAILED),
    ).fetchall()


def mark_auto_run(job_id: int) -> None:
    """Stamp the daily run as fired. Written before the run, not after:
    the point of the stamp is that one credit day yields one automatic run
    whatever happens to that run."""
    update_job(job_id, last_auto_run_at=utcnow())


def active_job_id() -> Optional[int]:
    row = connect().execute(
        "SELECT id FROM jobs WHERE status = ? ORDER BY id DESC LIMIT 1", (RUNNING,)
    ).fetchone()
    return int(row["id"]) if row else None


# --- Job videos ------------------------------------------------------------


_UPSERT_VIDEO = """
INSERT INTO job_videos (job_id, position, video_id, title, state, message, settled_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id, video_id)
DO UPDATE SET position = excluded.position,
              title = COALESCE(excluded.title, job_videos.title)
"""

# The same merge with the stored position left standing. A refresh re-reads a
# source whose order has moved on — a channel lists its newest upload first — so
# taking the new ordering would renumber every record already printed on the
# listing. New arrivals are appended past the last position instead, which is
# also the order they are added in.
_UPSERT_VIDEO_KEEPING_POSITION = """
INSERT INTO job_videos (job_id, position, video_id, title, state, message, settled_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id, video_id)
DO UPDATE SET title = COALESCE(excluded.title, job_videos.title)
"""


def known_video_ids(job_id: int) -> set[str]:
    return {
        row["video_id"]
        for row in connect().execute(
            "SELECT video_id FROM job_videos WHERE job_id = ?", (job_id,)
        )
    }


def set_job_videos(
    job_id: int,
    videos: Iterable[tuple[str, Optional[str], Optional[str]]],
    preserve_positions: bool = False,
) -> int:
    """Record what the source holds, as (video_id, title, unavailable_reason).

    Existing rows keep their state, so re-enumerating a source that has changed
    since never resets progress. An entry the source lists but cannot serve is
    written as skipped with its reason, so the run never pays to be refused.

    `preserve_positions` is for a refresh of a job already under way: stored
    positions stand and anything new is appended after the last of them.
    """
    rows = list(videos)
    statement = _UPSERT_VIDEO_KEEPING_POSITION if preserve_positions else _UPSERT_VIDEO
    with tx() as conn:
        if preserve_positions:
            known = {
                row["video_id"]
                for row in conn.execute(
                    "SELECT video_id FROM job_videos WHERE job_id = ?", (job_id,)
                )
            }
            highest = conn.execute(
                "SELECT COALESCE(MAX(position), 0) AS n FROM job_videos WHERE job_id = ?",
                (job_id,),
            ).fetchone()["n"]
            next_position = int(highest) + 1
        for index, (video_id, title, reason) in enumerate(rows, start=1):
            if not preserve_positions:
                position = index
            elif video_id in known:
                # The row exists, so the INSERT never happens and this value is
                # never stored; the DO UPDATE branch leaves the position alone.
                position = 0
            else:
                position = next_position
                next_position += 1
            conn.execute(
                statement,
                (
                    job_id,
                    position,
                    video_id,
                    title,
                    V_SKIPPED if reason else V_PENDING,
                    reason,
                    utcnow() if reason else None,
                ),
            )
        # Counted off the table rather than off this listing: after a refresh the
        # job also holds videos the source has since dropped, and they are still
        # part of what this job is accounting for.
        conn.execute(
            """UPDATE jobs
               SET total_videos = (SELECT COUNT(*) FROM job_videos WHERE job_id = ?),
                   updated_at = ?
               WHERE id = ?""",
            (job_id, utcnow(), job_id),
        )
    return len(rows)


def mark_video(job_id: int, video_id: str, state: str, message: Optional[str] = None) -> None:
    with tx() as conn:
        conn.execute(
            """UPDATE job_videos SET state = ?, message = ?, settled_at = ?
               WHERE job_id = ? AND video_id = ?""",
            (state, message, utcnow(), job_id, video_id),
        )


def mark_videos_added(job_id: int, video_ids: Iterable[str]) -> int:
    ids = list(video_ids)
    if not ids:
        return 0
    now = utcnow()
    with tx() as conn:
        cur = conn.execute(
            f"""UPDATE job_videos SET state = ?, settled_at = COALESCE(settled_at, ?)
                WHERE job_id = ? AND video_id IN ({",".join("?" * len(ids))})
                  AND state != ?""",
            (V_ADDED, now, job_id, *ids, V_ADDED),
        )
        return cur.rowcount


def mark_missing_from_source(job_id: int, present: Iterable[str], message: str) -> int:
    """Retire the records the source no longer lists.

    Only unsettled records are touched. A video that was already added is left
    exactly as it is: it is in the playlist, it stays in the playlist, and the
    source dropping it afterwards does not undo the insert this job paid for.
    Left pending it would instead be retried on every resume and charged fifty
    units to be refused, which is the same reason a dead entry is skipped.
    """
    listed = set(present)
    unsettled = [
        row["video_id"]
        for row in connect().execute(
            "SELECT video_id FROM job_videos WHERE job_id = ? AND state IN (?, ?)",
            (job_id, V_PENDING, V_FAILED),
        )
    ]
    gone = [video_id for video_id in unsettled if video_id not in listed]
    if not gone:
        return 0
    now = utcnow()
    with tx() as conn:
        # Chunked: a source can hold more ids than SQLite will bind in one
        # statement, and this list is as long as the source is.
        for start in range(0, len(gone), 400):
            chunk = gone[start : start + 400]
            conn.execute(
                f"""UPDATE job_videos SET state = ?, message = ?, settled_at = ?
                    WHERE job_id = ? AND video_id IN ({",".join("?" * len(chunk))})""",
                (V_SKIPPED, message, now, job_id, *chunk),
            )
    return len(gone)


def revive_skipped(job_id: int, servable: Iterable[str]) -> int:
    """The other direction: a skipped record the source is serving again.

    A video is skipped because the source could not serve it — it had been made
    private, deleted, or dropped from the playlist. All three can be undone by
    whoever owns it, and once the source lists it as copyable again the record
    has to go back in the queue, or the job would never copy it however often
    the source is re-read.
    """
    revivable = set(servable)
    skipped = [
        row["video_id"]
        for row in connect().execute(
            "SELECT video_id FROM job_videos WHERE job_id = ? AND state = ?",
            (job_id, V_SKIPPED),
        )
    ]
    back = [video_id for video_id in skipped if video_id in revivable]
    if not back:
        return 0
    with tx() as conn:
        for start in range(0, len(back), 400):
            chunk = back[start : start + 400]
            conn.execute(
                f"""UPDATE job_videos SET state = ?, message = NULL, settled_at = NULL
                    WHERE job_id = ? AND video_id IN ({",".join("?" * len(chunk))})""",
                (V_PENDING, job_id, *chunk),
            )
    return len(back)


def reset_failed_videos(job_id: int) -> int:
    """A resume retries whatever failed last time; only 'added' is final."""
    with tx() as conn:
        cur = conn.execute(
            "UPDATE job_videos SET state = ?, message = NULL WHERE job_id = ? AND state = ?",
            (V_PENDING, job_id, V_FAILED),
        )
        return cur.rowcount


def reset_all_videos(job_id: int) -> None:
    """The playlist this job was filling no longer exists, so nothing it
    recorded as added is true any more. Skipped entries are left alone: a
    deleted or private video at the source is still deleted or private."""
    with tx() as conn:
        conn.execute(
            """UPDATE job_videos SET state = ?, message = NULL, settled_at = NULL
               WHERE job_id = ? AND state != ?""",
            (V_PENDING, job_id, V_SKIPPED),
        )


def pending_videos(job_id: int) -> list[sqlite3.Row]:
    return connect().execute(
        """SELECT video_id, title, position FROM job_videos
           WHERE job_id = ? AND state = ? ORDER BY position""",
        (job_id, V_PENDING),
    ).fetchall()


def video_counts(job_id: int) -> dict[str, int]:
    rows = connect().execute(
        "SELECT state, COUNT(*) AS n FROM job_videos WHERE job_id = ? GROUP BY state",
        (job_id,),
    ).fetchall()
    counts = {V_PENDING: 0, V_ADDED: 0, V_FAILED: 0, V_SKIPPED: 0}
    for row in rows:
        counts[row["state"]] = int(row["n"])
    return counts


def recent_records(job_id: int, limit: int = 60) -> list[sqlite3.Row]:
    """The most recently settled record lines, newest first."""
    return connect().execute(
        """SELECT position, video_id, title, state, message, settled_at
           FROM job_videos
           WHERE job_id = ? AND state != ?
           ORDER BY settled_at DESC, position DESC
           LIMIT ?""",
        (job_id, V_PENDING, limit),
    ).fetchall()


def next_pending_record(job_id: int) -> Optional[sqlite3.Row]:
    return connect().execute(
        """SELECT position, video_id, title FROM job_videos
           WHERE job_id = ? AND state = ? ORDER BY position LIMIT 1""",
        (job_id, V_PENDING),
    ).fetchone()


# --- Quota ledger ----------------------------------------------------------


def charge(op: str, units: int, job_id: Optional[int] = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO quota_ledger (spent_at, op, units, job_id) VALUES (?, ?, ?, ?)",
            (utcnow(), op, units, job_id),
        )
        if job_id is not None:
            conn.execute(
                "UPDATE jobs SET units_charged = units_charged + ?, updated_at = ? WHERE id = ?",
                (units, utcnow(), job_id),
            )


def has_charge_since(op: str, iso_timestamp: str) -> bool:
    """Whether one particular kind of charge has been written since a moment —
    used to ask whether YouTube's own refusal has already pinned today."""
    row = connect().execute(
        "SELECT 1 FROM quota_ledger WHERE op = ? AND spent_at >= ? LIMIT 1",
        (op, iso_timestamp),
    ).fetchone()
    return row is not None


def units_since(iso_timestamp: str) -> int:
    row = connect().execute(
        "SELECT COALESCE(SUM(units), 0) AS n FROM quota_ledger WHERE spent_at >= ?",
        (iso_timestamp,),
    ).fetchone()
    return int(row["n"])


def prune_ledger(before_iso: str) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM quota_ledger WHERE spent_at < ?", (before_iso,))


def telemetry_counts() -> dict[str, int]:
    """The four aggregate figures the anonymous ping reports, in one connection.

    Here rather than in `telemetry.py` because every other module in this app
    reaches the database only through this one. Nothing identifying is counted:
    these are totals over the whole register, never a row of it.

    Three of the four are lifetime figures, so what the register currently holds
    is added to what `delete_job` banked on its way out. Read that way they only
    ever rise, and an instance that removes its completed jobs reports the same
    numbers as one that keeps them.

    `daily_run_count` is the exception, and deliberately: it answers "how many
    jobs are set to run themselves each day", which is a fact about the register
    as it stands. A job that was removed is not running tomorrow, so it must not
    be counted.
    """
    conn = connect()
    jobs_row = conn.execute(
        "SELECT COUNT(*) AS jobs, COALESCE(SUM(units_charged), 0) AS units, "
        "COALESCE(SUM(scheduled), 0) AS daily_runs FROM jobs"
    ).fetchone()
    added_row = conn.execute(
        "SELECT COUNT(*) AS n FROM job_videos WHERE state = ?", (V_ADDED,)
    ).fetchone()
    return {
        "job_count": int(jobs_row["jobs"]) + _counter(conn, REMOVED_JOBS_KEY),
        "videos_added": int(added_row["n"]) + _counter(conn, REMOVED_VIDEOS_ADDED_KEY),
        "units_charged": int(jobs_row["units"]) + _counter(conn, REMOVED_UNITS_KEY),
        "daily_run_count": int(jobs_row["daily_runs"]),
    }


# --- Settings --------------------------------------------------------------


def get_setting(key: str) -> Optional[sqlite3.Row]:
    return connect().execute(
        "SELECT key, value, set_at FROM settings WHERE key = ?", (key,)
    ).fetchone()


def set_setting(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, set_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, set_at = excluded.set_at",
            (key, value, utcnow()),
        )
