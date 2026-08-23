"""Daily runs: the jobs that start themselves.

This is the one place the app spends units without being asked at the moment of
spending, so its rules are deliberately narrow and every one of them is a
promise the interface repeats:

1. **Opt in, per job.** Nothing runs unattended unless its box is ticked.
2. **Once per credit day.** A daily run fires when the day's free credits
   are there, does one run, and does not fire again until the next reset. With
   a run limit it adds that many and holds; without one it runs to the wall.
   Either way one job cannot spend a day's credits twice.
3. **Never against a fault.** A job that failed is skipped by the runner and
   says on the register that it is waiting for its operator. Retrying a broken
   job every day would spend real units to reach the same wall.
4. **Never behind a person's back.** A run started by hand always wins: the
   runner only ever starts a job when nothing else is running, and pressing
   Stop on a daily run lifts it.
5. **One message.** Everything the day's automatic runs did is reported in a
   single Telegram message, sent once the runner has finished for the day.

The loop itself is cheap — the database and the local ledger, no API call — so
it can afford to look every minute and find nothing, which is the usual answer.
"""

from __future__ import annotations

import threading
from typing import Optional

from . import config, db, jobs, logs, notify, quota

log = logs.get("daily")


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Held only while a tick is deciding or running, so the state the pages
        # read is the state the runner is acting on.
        self._lock = threading.Lock()

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="daily-runs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # A short first wait rather than a full interval: a container restarted
        # after the reset should not sit idle for a minute with credits waiting.
        delay: float = 5
        while not self._stop.wait(delay):
            delay = config.SCHEDULER_INTERVAL
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — a failed tick must not end the loop
                log.exception("Daily-run check failed; will try again")

    # -- Eligibility --------------------------------------------------------

    def due(self) -> list:
        """Daily runs that may fire in the credit day now under way."""
        return db.due_standing_orders(quota.last_reset().isoformat(timespec="seconds"))

    def blocked(self) -> list:
        """Ticked jobs a daily run will not touch: the ones that failed.

        Surfaced rather than silently skipped — a job that quietly stops running
        itself is exactly the kind of thing this app must not do.
        """
        return [job for job in db.standing_orders() if job["status"] == db.FAILED]

    # -- The tick -----------------------------------------------------------

    def tick(self) -> int:
        """Run every daily run the day's credits reach, then report once.

        Returns how many jobs it ran, which is almost always zero.
        """
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            reports: list[notify.RunReport] = []
            ran = 0
            while not self._stop.is_set():
                if jobs.runner.busy:
                    # Someone pressed Start. A run they are watching outranks
                    # one they are not; the daily runs wait for the next
                    # look, and the day's stamp means they have not lost a turn.
                    break
                alloc = quota.allocation()
                if alloc.exhausted:
                    break
                job = next(iter(self.due()), None)
                if job is None:
                    break

                report = self._run_one(job)
                ran += 1
                if report is not None:
                    reports.append(report)

            if reports:
                self._report(reports)
            return ran
        finally:
            self._lock.release()

    def _run_one(self, job) -> Optional[notify.RunReport]:
        job_id = int(job["id"])
        log.info("Daily run starting job %04d", job_id)
        before = db.video_counts(job_id)
        before_units = int(job["units_charged"])

        # Stamped before the run, not after: one credit day yields one automatic
        # run whatever becomes of it, and a crash halfway through must not read
        # as a turn the job never took.
        db.mark_auto_run(job_id)
        try:
            # The register may be set to remove a job the moment it completes.
            # It must not do that until this has read what the run did, or the
            # day's report would be missing the one job that actually finished —
            # so the sweep is held back and run here, after the reading.
            jobs.runner.start(job_id, sweep_on_complete=False)
        except jobs.JobBusy:
            log.info("Daily run for job %04d gave way to a run started by hand", job_id)
            return None
        jobs.runner.join()

        after = db.get_job(job_id)
        if after is None:
            return None
        counts = db.video_counts(job_id)
        status = after["status"]
        jobs.runner.sweep(job_id)
        if not job["notify"]:
            return None

        return notify.RunReport(
            ref=f"{job_id:04d}",
            playlist_title=after["playlist_title"],
            added=counts[db.V_ADDED] - before[db.V_ADDED],
            failed=counts[db.V_FAILED],
            remaining=counts[db.V_PENDING] + counts[db.V_FAILED],
            units=int(after["units_charged"]) - before_units,
            status=status,
            playlist_url=(
                f"https://www.youtube.com/playlist?list={after['playlist_id']}"
                if after["playlist_id"]
                else None
            ),
        )

    def _report(self, reports: list[notify.RunReport]) -> None:
        if not notify.configured():
            # Ticked for notification with nothing to notify through. Recorded
            # so the Telegram page can say a message was owed and not sent,
            # rather than the user waiting for one that never comes.
            notify.record_unsent(
                "A daily run ran with notification switched on, but no bot token and "
                "chat were set, so the report could not be sent."
            )
            log.warning("Daily runs ran with notification on, but Telegram is not set up")
            return
        alloc = quota.allocation()
        # One line, three facts: what today has cost, the ceiling, and when the
        # next automatic run becomes possible. The paragraph that used to
        # explain the estimate lives on the page, where there is room for it.
        credits_line = f"⚡ {alloc.charged:,}/{alloc.limit:,}u · resets {quota.reset_human()}"
        failure = notify.try_send(notify.compose(reports, credits_line))
        if failure:
            log.warning("Daily-run report could not be delivered: %s", failure)
        else:
            log.info("Daily-run report sent for %d job(s)", len(reports))


scheduler = Scheduler()
