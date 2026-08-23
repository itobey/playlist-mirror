# What is Playlist Mirror?

Playlist Mirror copies every video of a YouTube channel — or of any playlist you can view —
into your own private, editable playlist. Watch through it and delete videos as you go,
something YouTube's own channel pages don't let you do.

It's self-hosted, single user, and runs in one Docker container on your own machine or home
server.

## Why this exists

YouTube limits how much a personal app can do per day. A channel with hundreds of videos can
take several days to fully copy. Most tools that try this just crash partway through and lose
their place.

Playlist Mirror instead treats that daily limit as something to plan around: it tells you up
front how long a copy will take, works within the limit each day, and picks up exactly where
it left off tomorrow — into the same playlist, with nothing duplicated. See [Quota](/details/quota)
for how that accounting works.

## Features

- **Copy a channel or a playlist.** Paste a channel URL, an `@handle`, a channel ID, a
  playlist URL, or a playlist ID. Watch Later and Liked videos can't be copied — YouTube
  doesn't allow any outside app to read them.
- **Know the cost before you start.** The source is read first, so you see exactly how many
  videos there are and how many days it will realistically take, before anything is created.
- **Watch it happen live.** A live view shows each video as it's added, how fast it's going,
  and how much time is left until the job finishes or today's allowance runs out.
- **Stops cleanly, not with a wall of errors.** When the daily allowance is used up, the job
  pauses and tells you exactly when you can pick it back up.
- **Set your own pace.** Cap how many videos a single run adds — say, 50 at a time — and
  resume whenever you like.
- **Can run itself.** Turn on [daily runs](/details/daily-runs) for a job and it
  starts on its own each day as soon as there's room in the daily allowance, up to your run
  limit. Nothing runs unattended unless you switch this on, and a failed job is never retried
  automatically.
- **Clear finished jobs away by itself.** Tick **Remove a job when it finishes** under
  "Every job at once" on the playlists page and each job leaves the register the moment it
  finishes with nothing left to add. A job that finished with failed videos stays put, so you
  can still resume and retry them — and your YouTube playlists are never touched, only the
  app's own record of the job.
- **Optional Telegram summary.** Get a single daily message listing what your automatic jobs
  did. See [Notifications](/details/notifications).
- **Resumes exactly where it stopped.** Every video is tracked as it's added, so picking a
  job back up — after a pause, a crash, or a restart — continues into the same playlist with
  no duplicates and no progress lost.
- **Catches up with new uploads.** Press "Re-check source" on a job to pick up anything the
  channel or playlist has added since you started, and queue it into the same mirror.

## Limits

- One YouTube account per installation — this isn't a multi-user app.
- Nothing runs unattended unless you explicitly turn on "Run this job daily, when credits
  allow" for a job.
- No filtering by date, keyword, or anything else — a source is always copied in full, in its
  own order.
- No automatic, continuous syncing. New uploads are only picked up when you press
  "Re-check source" or when a daily run re-runs a job.
- Videos that have been deleted or made private at the source are skipped and noted as such;
  they're never charged against your daily allowance.

## Next steps

[Get started](/introduction/getting-started) with Docker Compose, or jump straight to
[configuration](/details/configuration) if you're already familiar with the app.
