# Daily runs

You can start any saved job yourself by clicking **Run**. For larger channels or playlists,
however, a job may take several days to finish because of the YouTube API's daily
[quota](/details/quota). Daily Runs handle this for you: once enabled for a job, the app
automatically continues it in the background each day until it is complete.

## The rules

1. **Opt in, per job.** Nothing runs unattended unless its box is ticked.
2. **Once per credit day.** A daily run fires when the day's free credits are there,
   does one run, and doesn't fire again until the next reset. With a run limit it adds that
   many videos and holds; without one it runs to the wall. Either way, one job cannot spend a
   day's credits twice.
3. **Never against a fault.** A job that failed is skipped by the runner and marked
   as waiting for you to look at it. Retrying a broken job every day would spend real units
   just to reach the same wall again.
4. **Never behind your back.** A run started by hand always wins — the runner only ever
   starts a job when nothing else is running, and pressing Stop on a daily run lifts it.
5. **One message.** Everything the day's automatic runs did is reported in a single Telegram
   message (if [notifications](/details/notifications) are set up), sent once the runner has
   finished for the day.

## How jobs are picked

The scheduler works through eligible jobs in the order they were registered, starting with
the oldest. It finishes one job's daily run before moving to the next, and continues for as
long as quota is available.

For example, if two jobs each have a run limit of 50 videos, the scheduler adds up to 50
videos from the first job and then moves to the second. If the quota is exhausted during the
second job, that job pauses at that point. Any unfinished work, including jobs the scheduler
has not reached yet, is picked up on a later daily run after the quota resets.

## How checking works

The scheduler loop is cheap — it only looks at the database and the app's own local ledger,
no API call — so it can afford to check every `SCHEDULER_INTERVAL` seconds (default 60,
floored at 15) and usually find nothing to do.

## Outcomes

| Status | What it means |
| --- | --- |
| Complete | Every video is in the playlist. |
| Paused (run limit) | Stopped at your run limit; runs again after the next reset. |
| Paused (quota) | The day's free credits ran out; runs again after the next reset. |
| Cancelled | Interrupted; runs again after the next reset. |
| Failed | Stopped with a fault — will not start itself again until you look at it. |
| Pending | Did not get started. |
| Running | Still running. |
