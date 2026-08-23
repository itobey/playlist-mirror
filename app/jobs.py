"""The job runner.

One playlist mirror runs at a time — the allocation is a single shared budget, so
running two jobs against it would only make both of them stop sooner and make
the projection useless. Every insert is written to the database the moment it
settles, which is what makes a resume exact rather than approximate.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from googleapiclient.errors import HttpError

from . import config, db, logs, quota, telemetry, youtube

log = logs.get("runs")

# Phases reported while a job is running, in order.
PHASE_RESOLVE = "reading channel"
PHASE_ENUMERATE = "listing uploads"
PHASE_RECONCILE = "reconciling playlist"
PHASE_CREATE = "creating playlist"
PHASE_INSERT = "adding videos"


class JobBusy(Exception):
    pass


@dataclass
class RefreshResult:
    """What re-reading the source actually changed, for the line the operator
    is shown afterwards. All three can be zero: a source that has not moved
    since the job was registered is the ordinary answer, and saying so plainly
    is better than an unqualified 'done'."""

    new_videos: int
    dropped: int
    revived: int
    source_total: int
    units: int


@dataclass
class RunState:
    """In-memory detail about the job running right now. The database holds the
    durable truth; this holds what only matters while the tab is open."""

    job_id: int
    phase: str = PHASE_RESOLVE
    current_position: Optional[int] = None
    current_video_id: Optional[str] = None
    current_title: Optional[str] = None
    added_this_run: int = 0
    # Every record this run has settled either way. The ceiling counts attempts,
    # not successes: a failed insert still cost fifty units, so letting it slip
    # past the ceiling would make the run spend more than the user asked it to.
    settled_this_run: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)

    @property
    def rate_per_minute(self) -> Optional[float]:
        elapsed = time.monotonic() - self.started_monotonic
        if self.added_this_run < 3 or elapsed <= 0:
            return None
        return round(self.added_this_run / elapsed * 60, 1)


# How many farewells to keep. A removed job leaves one sentence behind so the
# page that was watching it has something true to print instead of a 404; past
# that, whoever was watching has long since been told.
REMOVAL_MEMORY = 20


class Runner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self.state: Optional[RunState] = None
        # What the register said as it took a job away, keyed by job id. Held
        # here rather than written to the database, because it exists only to
        # answer the tab that was open on that job at the moment it went.
        self.removals: "OrderedDict[int, dict]" = OrderedDict()

    # -- Lifecycle ----------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def running_job_id(self) -> Optional[int]:
        return self.state.job_id if self.busy and self.state else None

    def start(self, job_id: int, sweep_on_complete: bool = True) -> None:
        """Run a job.

        `sweep_on_complete` is off for the daily-run runner alone: it has to
        read what the job did before the register may take the job away, and a
        removal that happened inside the run would leave its Telegram report
        with nothing to describe. It sweeps for itself once it has looked.
        """
        with self._lock:
            if self.busy:
                # The worker clears self.state in its finally block while the
                # thread is still alive, so running_job_id() may already be None.
                other = self.running_job_id()
                label = f"Job {other:04d} is" if other is not None else "Another playlist mirror is"
                raise JobBusy(
                    f"{label} running. Only one playlist mirror runs at a time, "
                    "because they share the same pool of free daily credits."
                )
            self._cancel.clear()
            self.state = RunState(job_id=job_id)
            self._thread = threading.Thread(
                target=self._run,
                args=(job_id, sweep_on_complete),
                name=f"job-{job_id}",
                daemon=True,
            )
            self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for the running playlist mirror to finish.

        Only the daily-run runner uses this: it starts a job and must know
        the outcome before it decides whether the day's credits still stretch to
        the next one. Nothing on a request path may ever call it — a mirror
        runs for minutes.
        """
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def cancel(self, job_id: int) -> bool:
        if self.running_job_id() != job_id:
            return False
        log.info("Job %04d stopped by hand", job_id)
        self._cancel.set()
        return True

    # -- Reading the source -------------------------------------------------

    def _resolve_source(self, client: youtube.Client, job_id: int, source_input: str) -> None:
        source = client.resolve_source(source_input)
        db.update_job(
            job_id,
            source_kind=source.kind,
            source_id=source.id,
            source_title=source.title,
            source_owner=source.owner,
        )

    def _enumerate(
        self, client: youtube.Client, job, preserve_positions: bool = False
    ) -> list[tuple[str, Optional[str], Optional[str]]]:
        """Read the source's listing into the job, and hand it back so the
        caller can also see what the source no longer holds."""
        source = youtube.Source(
            kind=job["source_kind"],
            id=job["source_id"],
            title=job["source_title"],
            owner=job["source_owner"],
        )
        listing = client.source_playlist_id(source)
        videos = [
            (v.video_id, v.title, v.unavailable_reason)
            for v in client.iter_playlist_videos(listing)
        ]
        db.set_job_videos(job["id"], videos, preserve_positions=preserve_positions)
        return videos

    def preflight(self, job_id: int) -> None:
        """Resolve the source and enumerate its videos so the estimate shown
        before committing is a real count, not a guess. Costs roughly one unit
        per fifty videos — about 0.05% of a day's free credits for a 600-video
        source."""
        job = db.get_job(job_id)
        if job is None:
            raise ValueError(f"No job {job_id}")

        client = youtube.Client(job_id=job_id)
        self._resolve_source(client, job_id, job["source_input"])
        job = db.get_job(job_id)
        assert job is not None
        self._enumerate(client, job)
        db.update_job(job_id, message=None)

    def refresh_source(self, job_id: int) -> RefreshResult:
        """Re-read the source and merge what it holds now into an existing job.

        A source is enumerated once, when the job is registered, and everything
        after that runs off that listing — so a channel that uploads afterwards
        is invisible to a job already on the register. This is the answer to
        that, at one unit per fifty videos: thirteen units for a 600-video
        channel, against the fifty a single duplicate insert would cost. The
        playlist the job has already filled is kept, and every record already
        added stays added.
        """
        job = db.get_job(job_id)
        if job is None:
            raise ValueError(f"No job {job_id}")
        before_units = job["units_charged"]

        client = youtube.Client(job_id=job_id)
        # A job whose registration failed before the source was ever resolved
        # has nothing to re-read yet, so this doubles as its second attempt.
        if not job["source_id"]:
            self._resolve_source(client, job_id, job["source_input"])
            job = db.get_job(job_id)
            assert job is not None

        known = db.known_video_ids(job_id)
        videos = self._enumerate(client, job, preserve_positions=True)

        listed = [video_id for video_id, _, _ in videos]
        servable = [video_id for video_id, _, reason in videos if not reason]
        new_videos = sum(1 for video_id in listed if video_id not in known)
        dropped = db.mark_missing_from_source(
            job_id,
            listed,
            "No longer listed by the source, so there is nothing left to copy.",
        )
        revived = db.revive_skipped(job_id, servable)

        self._note_refresh(job_id, new_videos, dropped, revived)
        after = db.get_job(job_id)
        assert after is not None
        return RefreshResult(
            new_videos=new_videos,
            dropped=dropped,
            revived=revived,
            source_total=len(videos),
            units=after["units_charged"] - before_units,
        )

    def _note_refresh(self, job_id: int, new_videos: int, dropped: int, revived: int) -> None:
        """Record what the re-read found, and reopen a job the source has
        overtaken."""
        job = db.get_job(job_id)
        assert job is not None
        counts = db.video_counts(job_id)
        remaining = counts[db.V_PENDING] + counts[db.V_FAILED]

        parts = []
        if new_videos:
            parts.append(f"{new_videos} new at the source")
        if revived:
            parts.append(f"{revived} copyable again")
        if dropped:
            parts.append(f"{dropped} no longer listed")
        if parts:
            message = (
                "Source re-read: "
                + ", ".join(parts)
                + (
                    f". {remaining:,} still to add — resume to copy them."
                    if remaining
                    else ". Nothing left to add."
                )
            )
        else:
            # Not "nothing has changed": a video already added may well have left
            # the source, and that is deliberately not reported as a change,
            # because it is still in the playlist and there is nothing to do.
            message = "Source re-read: nothing new to copy."

        fields: dict = {"message": message}
        # A finished job that has just gained videos is not finished any more,
        # and only a resumable status puts the Resume control back on the page.
        if remaining and job["status"] == db.COMPLETE:
            fields.update(status=db.PENDING, finished_at=None)
        db.update_job(job_id, **fields)

    # -- The run ------------------------------------------------------------

    def _run(self, job_id: int, sweep: bool = True) -> None:
        before = db.video_counts(job_id)
        job = db.get_job(job_id)
        before_units = int(job["units_charged"]) if job else 0
        log.info(
            "Job %04d started — %s (%d to add)",
            job_id,
            job["playlist_title"] if job else "?",
            before[db.V_PENDING] + before[db.V_FAILED],
        )
        db.update_job(job_id, status=db.RUNNING, started_at=db.utcnow(), message=None)
        try:
            self._execute(job_id)
        except youtube.QuotaExceeded as error:
            self._hold(job_id, str(error), authoritative=True)
        except youtube.NotAuthorised as error:
            self._fail(job_id, str(error))
        except youtube.SourceNotFound as error:
            self._fail(job_id, str(error))
        except HttpError as error:
            self._fail(job_id, youtube.http_error_message(error))
        except Exception as error:  # noqa: BLE001 - surfaced to the user verbatim
            self._fail(job_id, f"{type(error).__name__}: {error}")
        finally:
            self.state = None
            # Read before the sweep: a job the register removes on completion
            # would otherwise be gone before the line that says what it did.
            self._log_outcome(job_id, before, before_units)
            if sweep:
                self.sweep(job_id)
            # Every settled run reports its fresh aggregate counts, whether it
            # was started by hand or by the daily-run scheduler. Keep the POST
            # off this worker so a dead telemetry host cannot delay completion.
            telemetry.ping_quietly_async()

    def _log_outcome(self, job_id: int, before: dict[str, int], before_units: int) -> None:
        """One line for what the run did — the counts, the spend, and where it
        stopped. This is the line to read when a job did not do what was
        expected, so it says the outcome message too."""
        after = db.get_job(job_id)
        if after is None:
            log.info("Job %04d finished — gone from the register", job_id)
            return
        counts = db.video_counts(job_id)
        message = after["message"]
        log.info(
            "Job %04d finished — %s · added %d · failed %d · %d left · %du spent%s",
            job_id,
            after["status"],
            counts[db.V_ADDED] - before[db.V_ADDED],
            counts[db.V_FAILED],
            counts[db.V_PENDING] + counts[db.V_FAILED],
            int(after["units_charged"]) - before_units,
            f" · {message}" if message else "",
        )

    # -- Removing a completed job -------------------------------------------

    def sweep(self, job_id: int) -> Optional[dict]:
        """Take this job off the register if it completed cleanly and the
        operator has asked for that to happen by itself.

        Returns the farewell — the sentence the register prints and the playlist
        it built — or None when the job stays, which is every other outcome.
        """
        if not db.auto_remove_completed():
            return None
        job = db.get_job(job_id)
        if job is None or not db.is_removable(job_id, job["status"]):
            return None

        counts = db.video_counts(job_id)
        added = counts[db.V_ADDED]
        farewell = {
            "ref": f"{job_id:04d}",
            "playlist_title": job["playlist_title"],
            "playlist_url": (
                f"https://www.youtube.com/playlist?list={job['playlist_id']}"
                if job["playlist_id"]
                else ""
            ),
            "added": added,
            "units_charged": int(job["units_charged"]),
            # Said in full rather than as "done": the job is about to stop
            # existing, so this sentence is the last thing that will ever
            # account for it, and it has to carry the count and the promise
            # about the playlist together.
            "notice": (
                f"Job {job_id:04d} finished — {added:,} video"
                f"{'' if added == 1 else 's'} in “{job['playlist_title']}” — and was removed "
                "from this app. Your YouTube playlist is untouched."
            ),
        }
        db.delete_job(job_id)
        self.removals[job_id] = farewell
        while len(self.removals) > REMOVAL_MEMORY:
            self.removals.popitem(last=False)
        return farewell

    def _execute(self, job_id: int) -> None:
        state = self.state
        assert state is not None
        client = youtube.Client(job_id=job_id)
        job = db.get_job(job_id)
        assert job is not None

        # 1. Source, if the preflight did not already settle it.
        if not job["source_id"]:
            state.phase = PHASE_RESOLVE
            self._resolve_source(client, job_id, job["source_input"])
            job = db.get_job(job_id)
            assert job is not None

        # 2. The source's videos, if not already enumerated.
        if not job["total_videos"]:
            state.phase = PHASE_ENUMERATE
            self._enumerate(client, job)
            job = db.get_job(job_id)
            assert job is not None

        if not job["total_videos"]:
            self._fail(
                job_id,
                "That playlist is empty."
                if job["source_kind"] == db.SOURCE_PLAYLIST
                else "That channel has no public uploads to copy.",
            )
            return

        # 3. Playlist: create once, then reuse forever. This is the whole reason
        #    a resume does not produce a second stray playlist.
        if job["playlist_id"]:
            state.phase = PHASE_RECONCILE
            self._reconcile(client, job_id, job["playlist_id"])
            job = db.get_job(job_id)
            assert job is not None

        # Reconciliation clears the id when the playlist was deleted on YouTube,
        # so this runs either on a first start or on a resume with no playlist left.
        if not job["playlist_id"]:
            alloc = quota.allocation()
            if alloc.spendable < config.COST_PLAYLIST_INSERT:
                self._hold(job_id, "Not enough free credits left today to create the playlist.")
                return
            state.phase = PHASE_CREATE
            playlist_id = client.create_playlist(
                job["playlist_title"], job["playlist_description"] or ""
            )
            db.update_job(job_id, playlist_id=playlist_id)
            job = db.get_job(job_id)
            assert job is not None

        # A resume retries what failed last time; only 'added' is final.
        db.reset_failed_videos(job_id)

        # 4. Insert, metering as we go.
        state.phase = PHASE_INSERT
        run_limit = job["run_limit"]
        for record in db.pending_videos(job_id):
            if run_limit and state.settled_this_run >= run_limit:
                self._hold_at_limit(job_id, run_limit)
                return

            if self._cancel.is_set():
                db.update_job(
                    job_id,
                    status=db.CANCELLED,
                    finished_at=db.utcnow(),
                    message="Stopped by you. Resume picks up at the next record.",
                )
                return

            alloc = quota.allocation()
            if alloc.spendable < config.COST_PLAYLIST_ITEM_INSERT:
                self._hold(job_id, None)
                return

            state.current_position = record["position"]
            state.current_video_id = record["video_id"]
            state.current_title = record["title"]

            try:
                client.add_to_playlist(job["playlist_id"], record["video_id"])
            except youtube.QuotaExceeded as error:
                # The API's own verdict, which outranks the local projection.
                self._hold(job_id, str(error), authoritative=True)
                return
            except HttpError as error:
                db.mark_video(
                    job_id, record["video_id"], db.V_FAILED, youtube.http_error_message(error)
                )
            else:
                db.mark_video(job_id, record["video_id"], db.V_ADDED)
                state.added_this_run += 1

            state.settled_this_run += 1
            time.sleep(config.INSERT_DELAY)

        state.current_position = None
        state.current_video_id = None
        state.current_title = None

        counts = db.video_counts(job_id)
        if counts[db.V_PENDING]:
            self._hold(job_id, None)
            return

        # The summary distinguishes the two ways a video can be missing from
        # the copy: skipped ones the source itself cannot serve, which retrying
        # will never fix, and failed ones a resume is worth spending units on.
        parts = [f"{counts[db.V_ADDED]} added"]
        if counts[db.V_SKIPPED]:
            parts.append(
                f"{counts[db.V_SKIPPED]} skipped (deleted or private at the source, "
                "so they cannot be copied by anything)"
            )
        if counts[db.V_FAILED]:
            parts.append(f"{counts[db.V_FAILED]} failed — resume to retry them")

        if len(parts) == 1:
            message = f"All {counts[db.V_ADDED]} videos are in the playlist."
        else:
            message = ", ".join(parts) + "."

        db.update_job(
            job_id,
            status=db.COMPLETE,
            finished_at=db.utcnow(),
            message=message,
        )

    def _reconcile(self, client: youtube.Client, job_id: int, playlist_id: str) -> None:
        """Read what is actually in the playlist before adding anything.

        One unit per fifty items against fifty units per duplicate insert: this
        is both the cheap option and the only one that guarantees no duplicates
        after a crash mid-insert.
        """
        try:
            present = list(client.iter_playlist_video_ids(playlist_id))
        except HttpError as error:
            if error.resp.status == 404:
                # The playlist was deleted on YouTube. Start a fresh one rather
                # than failing every insert against a dead id — and forget the
                # per-video progress, because it described the playlist that is gone.
                db.reset_all_videos(job_id)
                db.update_job(
                    job_id,
                    playlist_id=None,
                    message="The playlist was deleted on YouTube, so this job starts a new one.",
                )
                return
            raise
        db.mark_videos_added(job_id, present)

    # -- Outcomes -----------------------------------------------------------

    def _hold(self, job_id: int, message: Optional[str], authoritative: bool = False) -> None:
        counts = db.video_counts(job_id)
        remaining = counts[db.V_PENDING] + counts[db.V_FAILED]
        if authoritative:
            # YouTube refused, so the real allocation is gone even though the
            # local projection says otherwise — another tool is sharing this
            # Cloud project. Pin the ledger to the limit so the instrument stops
            # promising units that do not exist and Resume stops inviting the
            # user to run straight back into the same wall.
            shortfall = quota.allocation().remaining
            if shortfall > 0:
                db.charge(quota.PIN_OP, shortfall, job_id=None)
        if message is None:
            message = (
                f"Today's free credits are used up. {remaining} videos still to add — "
                f"resume after the reset ({quota.reset_human()})."
            )
        elif authoritative:
            message = (
                f"YouTube refused further calls: the project's daily quota is gone. "
                f"{remaining} videos still to add — resume after the reset "
                f"({quota.reset_human()})."
            )
        db.update_job(job_id, status=db.HELD_QUOTA, finished_at=db.utcnow(), message=message)

    def _hold_at_limit(self, job_id: int, run_limit: int) -> None:
        """The user's own ceiling, reached. Nothing is wrong and nothing is
        waiting on the clock — this job can be resumed the moment they say so."""
        counts = db.video_counts(job_id)
        remaining = counts[db.V_PENDING] + counts[db.V_FAILED]
        next_run = min(run_limit, remaining)
        db.update_job(
            job_id,
            status=db.HELD_LIMIT,
            finished_at=db.utcnow(),
            message=(
                f"Stopped at your limit of {run_limit:,} videos for this run. "
                f"{remaining:,} still to add — resume whenever you like and the next run "
                f"adds {next_run:,}."
            ),
        )

    def _fail(self, job_id: int, message: str) -> None:
        db.update_job(job_id, status=db.FAILED, finished_at=db.utcnow(), message=message)


runner = Runner()


def remove_completed() -> int:
    """Take every cleanly completed job off the register in one go.

    Used when the setting is switched on, so the register the operator is
    looking at becomes the register they just asked for rather than one that
    only starts tidying itself from the next job onwards. Everything the sweep
    leaves behind — paused, stopped, failed, or completed with failures — is a
    job that still has something to say to them.
    """
    rows = db.removable_completed()
    for row in rows:
        db.delete_job(int(row["id"]))
    return len(rows)


def recover_interrupted() -> None:
    """A job left marked running means the app stopped mid-mirror. Its per
    video state is intact, so it is interrupted, not failed — FAILED is reserved
    for jobs the API or this app actually broke."""
    for job in db.list_jobs(limit=200):
        if job["status"] == db.RUNNING:
            db.update_job(
                job["id"],
                status=db.CANCELLED,
                finished_at=db.utcnow(),
                message="Interrupted when the app stopped. Resume continues from the last record.",
            )
