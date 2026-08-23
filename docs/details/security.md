# Securing your instance

This app has **no login of its own**. Anyone who can reach it can use your YouTube account —
create playlists, add videos, and see your job history. Treat network access to the app as
equivalent to access to your Google account's YouTube permissions.

Keep it on your home network, behind a VPN, or behind a reverse proxy that requires a
sign-in.

## Hosting it somewhere other than your own computer

If you're running the app somewhere other than `localhost` — a home server, a VPN box, a
Kubernetes cluster — set `PUBLIC_BASE_URL` (or `config.publicBaseUrl` in the
[Helm chart](/details/helm)) to the address you actually reach it at.

Google sends the OAuth sign-in back to `<PUBLIC_BASE_URL>/oauth/callback`, so sign-in fails
until this matches. The app never derives that address from the incoming request — a reverse
proxy or ingress can't fix an unset `PUBLIC_BASE_URL` for you. The app's own **Account** page
shows the exact redirect URI to register.

You also need the right kind of OAuth client. Desktop-app clients accept `http://localhost`
redirects without registration and can't register any other redirect URI at all, so one that
worked locally will fail with `Error 400: redirect_uri_mismatch` once the app moves. For any
address other than localhost, create a **Web application** OAuth client and register the
exact redirect URI shown in the app under **Authorized redirect URIs**. It has to be
`https://` on a real public TLD, though the host only needs to resolve for your browser, not
for Google. See [Getting started](/introduction/getting-started) and
[Troubleshooting](/details/troubleshooting#error-400-redirect-uri-mismatch).

## What's stored, and where

Everything the app persists — the uploaded OAuth client secret, the sign-in token, and the
SQLite job database — lives under `DATA_DIR` (`./data` by default). This is the only
directory that needs protecting and backing up. See [Upgrading and backups](/details/upgrading).

## What leaves the container

Google, and — only if you set them up — Telegram. Plus a small anonymous ping, once at startup,
after every run, and otherwise once a day: counts and flags, no account, channel, playlist or video
identifiers. More information can be seen here: [Privacy and telemetry](/details/telemetry).

## No multi-tenancy

Playlist Mirror is single-user per instance: one YouTube account, no login, no account
system. The OAuth token stored under `DATA_DIR` is the instance's only identity. If you want
to mirror channels for more than one YouTube account, run separate instances with separate
data volumes.
