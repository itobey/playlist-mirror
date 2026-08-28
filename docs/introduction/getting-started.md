# Installation & setup

## Run it

```bash
docker compose up -d
```

Then open <http://localhost:8000>. The app walks you through the Google account setup below
directly in-app — no config files to edit by hand.

Everything the app needs to remember — your sign-in, your jobs, your settings — lives in the
`./data` folder. That's the only thing you need to back up. See [Upgrading and backups](/details/upgrading).

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

## One-time Google Cloud setup

Google requires every app that touches your YouTube account to run inside a Cloud project you
control. Four steps, once, roughly ten minutes.

### 1. Create a project and enable the API

In the [Google Cloud Console](https://console.cloud.google.com), create a project — any name.
Then search the menu for **YouTube Data API v3** and enable it for that project.

::: warning
Don't point another tool at the same project. Playlist Mirror can only count its own calls, so
a second tool's spending would make the remaining quota figure read higher than the truth, and
playlist mirrors would hit the wall earlier than predicted.
:::

A new project is granted **10,000 units a day**, and that's what the app assumes by default
(`QUOTA_DAILY_LIMIT`). Change it in the app's **Account** page only if Google granted this
project more — it takes effect on the next video, with no restart. See [Quota](/details/quota).

### 2. Add yourself as a test user

Under **APIs & Services**, open **OAuth consent screen** (called **Audience** in newer
consoles). In **Test users**, click **+ Add users** and enter the Google address you'll sign
in with.

::: tip Skipping this step
...produces `Error 403: access_denied — has not completed the Google verification process`.
While the app's status is Testing, only listed test users can sign in. There is no approval
wait; access is immediate.
:::

### 3. Create an OAuth client and add its file

In **Credentials**, choose **Create credentials → OAuth client ID**. The application type
depends on how you reach the app:

- **Desktop app** — if you open it at `http://localhost:8000` on the machine it runs on.
- **Web application** — for any other address, including a home server, a VPN box or a
  Kubernetes cluster. Under **Authorized redirect URIs**, add the exact URI shown on the
  app's setup page (`<PUBLIC_BASE_URL>/oauth/callback`), and set `PUBLIC_BASE_URL` to match.

Download the JSON and add it in the app's setup page. It's written to the data volume as
`client_secret.json` and never leaves the machine it runs on.

::: warning A desktop client won't work off localhost
Desktop-app clients accept any `http://localhost` redirect without registration — and only
those. They can't have any other redirect URI registered at all. So a desktop client that
worked fine locally fails with `Error 400: redirect_uri_mismatch` the moment you host the app
somewhere else; you need a separate Web application client for that.
:::

See [Hosting it somewhere other than your own computer](/details/security#hosting-it-somewhere-other-than-your-own-computer).

### 4. Sign in

Google asks for permission to manage your YouTube account. The resulting token is stored in
the data volume, so this is a one-time step.

## When sign-in goes wrong

A handful of failures account for nearly all of them.

**"Error 400: redirect_uri_mismatch"**
The redirect URI the app sent isn't registered on the OAuth client. The app's **Account**
page shows the exact string it sends — it must appear character for character under
**Authorized redirect URIs**. Usually one of two causes: `PUBLIC_BASE_URL` isn't set (so the
app still sends `http://localhost:8000/oauth/callback` from behind your reverse proxy), or
the client is a **Desktop app** one, which can't register anything but `http://localhost`.
See [Troubleshooting](/details/troubleshooting#error-400-redirect-uri-mismatch).

**"Error 403: access_denied"**
The account you signed in with is not a test user on that project. Add it in step 2, and
check you're adding it under the same project that owns the OAuth client. An incognito window
rules out signing in as a different cached account.

**The browser shows "connection refused" after signing in**
Nothing is listening on the redirect address. Confirm the container publishes its port
(`ports: "8000:8000"`) and that `PUBLIC_BASE_URL` matches the address you actually opened the
page at.

**Sign-in loads a stale page from some other app**
A service worker from a previous app on the same port is intercepting the callback. Clear
site data for that origin, use a private window, or move this app to a port nothing else has
ever used.

More failure modes are covered in [Troubleshooting](/details/troubleshooting).

## Next steps

Once signed in, paste a channel URL, `@handle`, channel ID, playlist URL, or playlist ID to
start your first playlist mirror.
