# Docker

The published image runs as a single container with all state under one volume.

## docker-compose.yml

```yaml
services:
  playlist-mirror:
    build: .
    container_name: playlist-mirror
    restart: unless-stopped
    environment:
      DATA_DIR: /app/data
      # Must match the address you actually open in the browser — Google sends
      # the sign-in back to PUBLIC_BASE_URL/oauth/callback.
      PUBLIC_BASE_URL: http://localhost:8000
      QUOTA_DAILY_LIMIT: "10000"
      QUOTA_RESERVE: "50"
      QUOTA_RESET_TZ: America/Los_Angeles
      PLAYLIST_PRIVACY: private
      SCHEDULER_INTERVAL: "60"
      # Optional Telegram reporting — see the Notifications page. A token does
      # not belong in a file you might commit; set it from the app instead.
      # TELEGRAM_BOT_TOKEN: ""
      # TELEGRAM_CHAT_ID: ""
    volumes:
      - ./data:/app/data
    ports:
      - "8000:8000"
```

```bash
docker compose up -d
```

## What's in the image

The `Dockerfile` is a plain `python:3.12-slim` base: no external database, no extra services.
Everything the app needs to run is `app/`, `templates/`, and `static/`, copied in at build
time.

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## The `./data` volume

Everything the app persists lives under `DATA_DIR`: the uploaded OAuth client secret, the
sign-in token, and the SQLite job database. Mount it as a volume, or state — including your
Google sign-in — is lost every time the container is recreated.

## Without Docker

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```
