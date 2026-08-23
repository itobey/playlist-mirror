"""Application logging.

Two decisions live here, both about signal:

1. **No access log.** A page that polls its own status turns uvicorn's access
   log into a wall of `GET / 200 OK` that buries everything worth reading. The
   app is a single-user tool behind a reverse proxy that keeps its own access
   log if anyone wants one, so uvicorn's is switched off outright.

2. **Events, not chatter.** What is left is what a person would want to see
   while working out why the app did something: a playlist mirror starting and what it
   did when it stopped, a daily run firing, a Telegram message going out or
   failing. One line each, at INFO.

Set LOG_LEVEL=DEBUG for more, or WARNING for less.
"""

from __future__ import annotations

import logging
import os

LOGGER_NAME = "playlist-mirror"

_configured = False


def setup() -> None:
    """Install the app's handler and silence uvicorn's access log.

    Safe to call twice: uvicorn configures its own logging before the app
    starts, so this runs after it and deliberately has the last word.
    """
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))

    app_log = logging.getLogger(LOGGER_NAME)
    app_log.handlers = [handler]
    app_log.setLevel(level)
    # uvicorn puts a handler on the root logger; without this every line is
    # printed twice.
    app_log.propagate = False

    # The one line per request. `disabled` rather than a level, because uvicorn
    # logs access at INFO and the rest of uvicorn is worth keeping at INFO.
    logging.getLogger("uvicorn.access").disabled = True


def get(name: str) -> logging.Logger:
    """A child of the app logger — `logs.get("runs")` → `playlist-mirror.runs`."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
