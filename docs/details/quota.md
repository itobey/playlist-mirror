# Quota

YouTube has no built-in way to copy a playlist or create one from all the videos on a channel,
so Playlist Mirror uses the YouTube Data API to provide that missing feature. Access to the API
comes with a daily quota: a channel with hundreds of videos can take several days to copy, and
a naive tool has no memory of where it got to. This page explains how the app accounts for it.

## The numbers

- The YouTube Data API v3 grants a per-project daily allocation, **10,000 units** by default,
  resetting at **midnight Pacific Time**.
- `playlists.insert` (creating the playlist) and each `playlistItems.insert` (adding one
  video) each cost **50 units**.
- `playlists.list` / `playlistItems.list` calls cost **1 unit**; a `search.list` call costs
  **100 units**.

At the default 10,000-unit allocation, a playlist mirror adds roughly **~198 videos a day**. A
600-video channel needs about 3 days. The app computes this up front, before creating
anything, so you know what you're starting.

## It's an estimate, and the app says so

The YouTube Data API publishes no "remaining quota" endpoint. Every figure the app shows is a
local projection: units it has charged itself since the last reset, subtracted from the
configured daily limit (`QUOTA_DAILY_LIMIT`, or whatever you've set on the Account page). The
authoritative signal is always the API's own `403 quotaExceeded` response, which overrides
anything computed locally — if YouTube refuses a call, that day's credits are gone whatever
the local tally says.

## Reserve

`QUOTA_RESERVE` (default 50 units) is held back so a run never spends the last of the day's
allocation on an insert it can't confirm succeeded. Set it to `0` to spend the allocation
down to the floor.

## Reset timezone

`QUOTA_RESET_TZ` (default `America/Los_Angeles`) controls when the app considers a new day to
have started. Google's own reset is midnight Pacific — change this only if you have reason to
believe it drifts from that.

If the printed reset clock ever disagrees with Google's real one — for example because the
container has no tzdata, or the timezone is misconfigured — the app lets you manually rule
today's count off early with **Reset the count**, rather than waiting for a possibly-wrong
computed midnight.

## Multiple tools, one project

Don't point another tool at the same Google Cloud project as Playlist Mirror. The app can
only count its own calls — a second tool's spending makes the remaining-quota figure read
higher than the truth, and playlist mirrors hit the wall earlier than predicted.

## Raising the limit

If Google has granted your project more than 10,000 units a day, set the real figure on the
app's **Account** page (or seed it via `QUOTA_DAILY_LIMIT`). It takes effect on the next
video, no restart required.
