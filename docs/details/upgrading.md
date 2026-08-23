# Upgrading and backups

## Backups

Everything the app needs to remember — your Google sign-in, your jobs, your settings — lives
in the `DATA_DIR` volume (`./data` by default): the uploaded OAuth client secret
(`client_secret.json`), the token (`token.json`), and the SQLite job database
(`transfers.sqlite3`).

That folder is the only thing you need to back up. Copying it to a new host and pointing
`DATA_DIR` at it restores the instance completely — no re-running the Google Cloud setup, no
losing in-progress jobs.

## Upgrading

```bash
docker compose pull
docker compose up -d
```

Because all state lives outside the image, upgrading is just replacing the container. The
job database schema is migrated automatically on startup if needed.

On Kubernetes, bump `image.tag` (or leave it at the chart's default `appVersion`) and run
`helm upgrade`. `persistence.enabled` must stay `true` — see [Helm](/details/helm).

## Before upgrading

Consider a manual snapshot of `DATA_DIR` before a version bump, especially if a job is
mid-mirror. Playlist Mirror resumes from exactly the state it left off in, so an untouched
data volume is normally all the safety net you need — a backup just covers the improbable
case of a failed migration.
