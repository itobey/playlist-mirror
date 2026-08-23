# Privacy and telemetry

Playlist Mirror does not collect personal data. Your playlists, the channels you copy, and your
Google sign-in never leave your own container — the OAuth token stays in `DATA_DIR`, and the only
thing that ever sees a playlist or video id is YouTube itself.

To get a rough idea of how many people run this, and how — and therefore how much it is worth
maintaining — the application sends a small anonymous ping. It is sent once on startup, after every
manual or daily run, and otherwise once a day.

## What is sent

Everything, in full. There is nothing in the payload that is not in this table.

| Field                | Example         | Meaning                                                    |
|----------------------|-----------------|------------------------------------------------------------|
| `instanceHash`       | `9f2c…`         | Random id for this installation, so repeat pings count once |
| `appVersion`         | `1.0.0`         | The version you are running                                 |
| `executionMode`      | `CONTAINER`     | `SOURCE`, `CONTAINER` or `KUBERNETES`                       |
| `pythonVersion`      | `3.12.7`        | Which Python the image or your checkout runs on             |
| `connected`          | `true`          | Whether the Google sign-in has been completed               |
| `jobCount`           | `4`             | How many jobs exist, in total                               |
| `videosAdded`        | `812`           | How many videos have been copied, in total                  |
| `unitsCharged`       | `40600`         | Quota units spent over this installation's life             |
| `dailyRunCount`      | `1`             | How many jobs are set to run themselves each day            |
| `quotaDailyLimit`    | `10000`         | The daily allowance in force                                |
| `telegramEnabled`    | `false`         | Whether Telegram notifications are set up                   |

Counts and flags, and nothing else. **No Google account or email, no channel ids, no playlist ids,
no video ids, no titles, no URLs, no Telegram bot token and no chat id.**

`connected` and `quotaDailyLimit` are there for one reason each: the sign-in is the step people get
stuck on, and the raised-quota figure says whether the app should assume 10,000 units or more.

## About that instance id

It is a random UUID, generated once on first run and stored in the SQLite database under
`DATA_DIR`. It is **not** derived from your Google account, your channel, or anything else about
you — there is nothing behind it to guess.

That distinction matters. A hash of an identifier that other people can enumerate — an email
address, a channel id — can be confirmed by anyone holding the hash and a list of candidates. A
random UUID cannot, because there is no preimage to test. This app asks for only the `youtube`
scope, so it never learns your account email in the first place, and it is written that way
deliberately.

Two consequences worth knowing: wiping `DATA_DIR` looks like a brand new installation, and the same
person on two machines counts as two. For "how many people run this", both are arguably the right
answer.

## Turning it off

There is **no `TELEMETRY_ENABLED` flag**, on purpose — the whole value of the ping is knowing the
install count, so I ask you to leave it on. If that is a dealbreaker, there are two ways to stop it:

- **Point it somewhere harmless.** `TELEMETRY_URL` is a normal environment variable. Set it to an
  address that goes nowhere and the send fails, which costs one `debug` log line and nothing else —
  the application does not care whether the ping succeeded, and nothing in the interface changes.

  ```yaml
  services:
    playlist-mirror:
      environment:
        TELEMETRY_URL: http://localhost:1
  ```

- **Block it at the network level.** `telemetry.itobey.dev` in your DNS sinkhole, or an egress rule
  that only lets the container reach Google. Same outcome, enforced outside the application.

The ping runs on its own daemon thread and every failure is swallowed. An air-gapped install, a dead
DNS entry or a deliberately broken URL all boot and run exactly as normal.

If you are comfortable with the anonymous information described above, please consider leaving
telemetry on. It is genuinely useful to me: it shows how many people use Playlist Mirror and helps
me decide whether continuing to maintain it is worth the time and effort.

## Outbound requests, in full

Google, Telegram if you asked for it, and the ping. Nothing else:

| Host                                                       | Why                                                | When                    |
|------------------------------------------------------------|----------------------------------------------------|-------------------------|
| `youtube.googleapis.com`, `www.googleapis.com`             | Reading the source and writing your playlist — the entire point of the app | While a job runs |
| `oauth2.googleapis.com`                                    | Exchanging and refreshing the sign-in token        | At sign-in, then as the token expires |
| `api.telegram.org`                                         | The optional [notifications](/details/notifications) | Only if you set them up |
| `telemetry.itobey.dev`                                     | The anonymous ping described above                 | Startup, after runs, otherwise daily |

The sign-in itself happens in **your browser**, at `accounts.google.com` — the container never sees
your Google password.

## Auditing it

The whole thing is one file, about two hundred lines with the comments:
[`app/telemetry.py`](https://github.com/itobey/playlist-mirror/blob/master/app/telemetry.py). The
payload is built in one function, `_payload()`. If you still have concerns, open an
[issue](https://github.com/itobey/playlist-mirror/issues).
