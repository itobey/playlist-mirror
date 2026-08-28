# Configuration

All configuration is by environment variable, set on the container (`docker-compose.yml`) or
the process directly. Everything the app persists lives under `DATA_DIR`, so a single Docker
volume covers credentials, the OAuth token, and the job database.

Several settings below only *seed* an initial value: once you change them from the app's own
pages (Account, Telegram), the stored database value wins from then on, and the environment
variable no longer has any effect until the stored value is cleared.

| Variable | Default | Description |
| --- | --- | --- |
| `DATA_DIR` | `data` | Root folder for everything the app persists: OAuth client secret, token, and the job database. |
| `CLIENT_SECRET_FILE` | `$DATA_DIR/client_secret.json` | Path to the uploaded Google OAuth client JSON — a "Desktop app" client for localhost, a "Web application" one for any other address. |
| `TOKEN_FILE` | `$DATA_DIR/token.json` | Path to the stored OAuth token after sign-in. |
| `DB_FILE` | `$DATA_DIR/transfers.sqlite3` | SQLite database holding jobs and settings. |
| `APP_HOST` | `0.0.0.0` | Interface the app binds to. |
| `APP_PORT` | `8000` | Port the app binds to. |
| `PUBLIC_BASE_URL` | `http://localhost:$APP_PORT` | Where Google sends the user back after sign-in. Must match the address you actually open in the browser, and `<PUBLIC_BASE_URL>/oauth/callback` must be registered on the OAuth client. Never derived from the incoming request, so an ingress or reverse proxy cannot fill it in for you — leaving it unset behind one causes `redirect_uri_mismatch`. See [Installation & setup](/introduction/getting-started). |
| `QUOTA_DAILY_LIMIT` | `10000` | The project's daily YouTube Data API quota, in units. Only seeds the figure shown on the Account page — the value set there is stored in the database and wins from then on. Clamped to a minimum of 50. |
| `QUOTA_RESERVE` | `50` | Units held back so a run never spends the last of the allocation on an insert it cannot confirm. Set to `0` to spend the allocation to the floor. |
| `QUOTA_RESET_TZ` | `America/Los_Angeles` | Timezone the daily quota resets in. Google's own reset is midnight Pacific. |
| `PLAYLIST_PRIVACY` | `private` | Privacy of playlists this app creates. One of `private`, `unlisted`, `public`. |
| `SCHEDULER_INTERVAL` | `60` | Seconds between checks of jobs marked "run this job daily, when credits allow". A cheap local check — the database and the app's own ledger, no API call. Floored at 15 seconds. |
| `INSERT_DELAY` | `0.2` | Seconds paused between playlist inserts, gentle on the per-second rate limit. |
| `LOG_LEVEL` | `INFO` | How much the container logs. At `INFO` it logs one line per playlist mirror started and finished (status, videos added, videos failed, videos left, units spent), one per daily run, and one per Telegram message sent or refused. The per-request access log is off at every level — a page that polls its own status would otherwise bury everything else. `WARNING` keeps only problems; `DEBUG` adds the libraries' own chatter. |
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Only seeds the stored value — see [Notifications](/details/notifications). |
| `TELEGRAM_CHAT_ID` | *(empty)* | Only seeds the stored value — see [Notifications](/details/notifications). |
| `TELEGRAM_TIMEOUT` | `15` | Seconds to wait on `api.telegram.org` before giving up. A notification that can't be delivered never holds up the next playlist mirror. |
| `UPDATE_CHECK_URL` | Playlist Mirror's latest-release GitHub API URL | Endpoint checked for the latest stable release. An invalid or unreachable override quietly disables successful checks. |
| `UPDATE_CHECK_TIMEOUT` | `10` | Seconds to wait for GitHub before giving up. A failed check never holds up startup or a page. |
| `UPDATE_CHECK_INTERVAL_HOURS` | `24` | Hours between release checks while the app remains running. Floored at one hour. |
| `UPDATE_CHECK_INITIAL_DELAY` | `5` | Seconds after startup before the first release check, keeping the request off the startup path. |

See [docker-compose.yml](https://github.com/itobey/playlist-mirror/blob/master/docker-compose.yml)
in the repository for a working example, or [Helm](/details/helm) for the Kubernetes
equivalent.
