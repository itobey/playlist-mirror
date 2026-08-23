# Notifications

Telegram notifications exist for one reason: a [daily run](/details/daily-runs)
runs while nobody is looking at the page, so the only way to learn what it did is to be told.
They're entirely optional and off by default.

## What you get

- **One summary after the day's automatic runs.** The app waits until all eligible Daily Runs
  have finished, then sends a single Telegram message with their results. For example, if
  three jobs run that day, you receive one summary covering all three instead of three
  separate messages. If no Daily Run takes place, no message is sent.
- **Plain text.** A playlist title is arbitrary text that will eventually contain an
  underscore, an asterisk, or a `<` — a message that fails to send because of Markdown
  escaping is worse than a plain one.
- **Never interrupts playlist mirroring.** A notification failure is recorded and shown on
  the Telegram page, but the job's outcome doesn't depend on whether Telegram was reachable.

## Setting it up

Set the bot token and chat ID from the app's own **Telegram** page — this is the recommended
path, since the values are then stored in the database and don't need to live in a config
file you might commit.

Alternatively, seed them via environment variables (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
These only seed the initial value; once set from the app's Telegram page, the stored value
wins from then on. See [Configuration](/details/configuration).

On Kubernetes, the [Helm chart](/details/helm) intentionally does not accept a plaintext bot
token in `values.yaml` — point `telegram.secretRef.name` at an existing secret instead, or
skip it and configure Telegram from the app itself.

`TELEGRAM_TIMEOUT` (default 15 seconds) bounds how long the app waits on `api.telegram.org`
before giving up on a delivery attempt.
