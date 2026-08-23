# Troubleshooting

## "Error 400: redirect_uri_mismatch"

Google was handed a redirect URI that the OAuth client doesn't accept. The app builds it as
`<PUBLIC_BASE_URL>/oauth/callback` and shows the finished string on its **Account** page;
that string must appear, character for character, under **Authorized redirect URIs** on the
client in **APIs & Services → Credentials**. Two causes cover nearly every case:

**`PUBLIC_BASE_URL` isn't set.** It defaults to `http://localhost:$APP_PORT`, and the app
never derives the URI from the incoming request — so behind an ingress or reverse proxy it
keeps sending `http://localhost:8000/oauth/callback` no matter what address you opened. Set
it (or `config.publicBaseUrl` in the [Helm chart](/details/helm)) to the address you actually
reach the app at, scheme included:

```yaml
PUBLIC_BASE_URL: https://playlist.example.com
```

**The OAuth client is a "Desktop app" one.** Those accept any `http://localhost` redirect
without registration, and no other redirect URI can be registered on them at all — which is
why a client that worked locally fails as soon as the app moves to a real hostname. Create a
new **OAuth client ID → Web application** in the same project, add the redirect URI there,
download the JSON and upload it on the app's setup page to replace `client_secret.json`.

A couple of rules Google applies to the URI itself: anything other than a loopback address
must be `https://`, and the host must sit on a real public TLD. A LAN-only name like
`https://playlist.mydomain.dev` is fine even though it only resolves on your own network —
Google never connects to it, your browser does the redirect. A private TLD such as `.local`
or `.internal` is rejected outright.

## "Error 403: access_denied"

The account you signed in with is not a test user on the Google Cloud project that owns the
OAuth client. Under **APIs & Services → OAuth consent screen** (or **Audience**), add your
Google address in **Test users**. Access is immediate — there's no approval wait. Use an
incognito window to rule out signing in as a different cached account.

## The browser shows "connection refused" after signing in

Nothing is listening on the redirect address. Confirm the container publishes its port
(`ports: "8000:8000"` in `docker-compose.yml`) and that `PUBLIC_BASE_URL` matches the address
you actually opened the app at. See [Configuration](/details/configuration).

## Sign-in loads a stale page from some other app

A browser service worker from a previous app on the same port is intercepting the OAuth
callback. Clear site data for that origin, use a private window, or move this app to a port
nothing else has ever used.

## "quotaExceeded" errors while mirroring

Expected behavior, not a bug — the day's API allocation is spent. The app pauses the job and
tells you when it can resume (see [Quota](/details/quota)). If this happens far earlier than
the estimate predicted, check whether another tool is using the same Google Cloud project;
Playlist Mirror can only count the calls it makes itself.

## A daily run stopped running itself

Check the job's status. A **failed** job is never retried automatically — it needs your
attention before it will run again. See [Daily runs](/details/daily-runs) for what
each status means.

## Telegram messages aren't arriving

Check the app's Telegram page for the last delivery result — a failed notification is
recorded there, and never blocks or retries a playlist mirror. Common causes: an incorrect bot
token/chat ID, or the container can't reach `api.telegram.org`.

## Still stuck?

Open an issue on [GitHub](https://github.com/itobey/playlist-mirror/issues).
