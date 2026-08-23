"""FastAPI application: routes, live event stream, and OAuth handling."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, db, jobs, logs, notify, quota, scheduler, telemetry, updates, youtube
from .version import VERSION

log = logs.get("app")

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# A global rather than a render() key: the 500 page deliberately bypasses
# render(), and the footer has to survive on that page too.
templates.env.globals["app_version"] = VERSION

@asynccontextmanager
async def lifespan(_: FastAPI):
    # First, so everything below reports through the app's own handler and the
    # per-request access log is off before the first request arrives.
    logs.setup()
    log.info("Playlist Mirror %s starting", VERSION)
    config.ensure_dirs()
    db.init()
    jobs.recover_interrupted()
    # The ledger only ever needs today's charges; anything older than two days
    # is dead weight that would otherwise grow by one row per API call forever.
    db.prune_ledger((quota.last_reset() - timedelta(days=2)).isoformat(timespec="seconds"))
    # Started after recovery, so a job left marked running by a stopped container
    # is retired before anything looks at whether it may start itself.
    scheduler.scheduler.start()
    # Last, and on their own daemon threads: nothing about starting up may wait
    # on an optional remote host, and neither host may fail a boot.
    telemetry.start()
    updates.start()
    yield
    updates.stop()
    scheduler.scheduler.stop()
    telemetry.stop()


app = FastAPI(title="Playlist Mirror", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Short-lived OAuth state, held in memory: single user, single sign-in at a time.
# Maps the state token to that sign-in's PKCE code verifier. The verifier has to
# survive from /oauth/start to /oauth/callback because the two requests build
# separate Flow objects, and Google will not exchange the code without it.
_oauth_state: dict[str, str] = {}

STATUS_LABELS = db.STATUS_LABELS


# --- Shared view data ------------------------------------------------------


def allocation_view() -> dict[str, Any]:
    alloc = quota.allocation()
    return {
        "limit": alloc.limit,
        "charged": alloc.charged,
        "remaining": alloc.remaining,
        "spendable": alloc.spendable,
        "reserve": alloc.reserve,
        "percent_charged": alloc.percent_charged,
        "videos_affordable": alloc.videos_affordable,
        "videos_per_day": alloc.videos_per_day,
        "exhausted": alloc.exhausted,
        # Google's own verdict rather than this app's projection, and the band
        # says which of the two it is showing: one is worth overriding by hand,
        # the other is not.
        "pinned": quota.pinned_today(),
        "resets_in": quota.reset_human(),
        "reset_tz": config.QUOTA_RESET_TZ,
    }


def allowance_view() -> dict[str, Any]:
    """The setup page's view of the allowance: the figure, where it came from,
    and whether today is already spoken for."""
    stored = db.get_setting(quota.SETTING_DAILY_LIMIT)
    alloc = quota.allocation()
    return {
        "limit": alloc.limit,
        "videos_per_day": alloc.videos_per_day,
        "from_container": stored is None,
        "container_limit": config.QUOTA_DAILY_LIMIT,
        "ceiling": quota.LIMIT_CEILING,
        "floor": config.COST_PLAYLIST_ITEM_INSERT,
        "pinned_today": quota.pinned_today(),
        "resets_in": quota.reset_human(),
    }


def limit_line(view: dict[str, Any]) -> str:
    """The one sentence printed under the run ceiling, saying what it will do.

    Written by the server rather than by the page, because the register and the
    job page both print it and a ceiling described two ways is a ceiling nobody
    trusts. Empty when none is set: there is nothing to describe until there is.
    """
    if not view["run_limit"]:
        return ""
    if view["is_running"]:
        return f"This run holds at {view['run_limit']:,} videos."
    if not (view["pending"] + view["failed"]):
        return "Nothing left to add, so there is nothing for it to hold at."
    # Once fewer videos are left than the ceiling, the ceiling is no longer the
    # thing that stops the run, and printing it as though it were would be a lie
    # by omission.
    return f"The next run adds up to {view['run_stops_after']:,} videos, then holds."


def schedule_line(job, view: dict[str, Any]) -> str:
    """The one sentence printed under a daily run, saying what will happen.

    Written per job rather than once for the page, because the answer differs by
    job: a failed job will not start itself however it is ticked, and a job with
    nothing left has nothing to start. Empty when no order stands — the sentence
    describes a decision that has been made, and there is nothing to describe
    until it is.
    """
    if not view["scheduled"]:
        return ""
    if job["status"] == db.FAILED:
        return (
            "This job stopped with a fault, so it will not start itself. Resume it once by "
            "hand and the daily run takes over again."
        )
    if not (view["pending"] + view["failed"]):
        return "Nothing left to add, so there is nothing for it to run."

    if view["auto_ran_today"]:
        when = (
            "It has already run on today's credits, so the next run is after the reset "
            f"({quota.reset_human()})."
        )
    elif quota.allocation().exhausted:
        when = f"Today's credits are used up, so it starts after the reset ({quota.reset_human()})."
    else:
        when = "Today's credits are there, so it starts within the minute."

    # How much one run adds is the ceiling's sentence, printed directly above
    # this one, so it is said here only where there is no ceiling to say it —
    # two lines of the same record telling the operator the same figure is how a
    # record stops being read at all.
    batch = (
        ""
        if job["run_limit"]
        else " With no run limit set, each run goes until the day's credits are spent."
    )
    told = (
        " You get one Telegram message once every daily run has run."
        if view["notify"]
        else ""
    )
    return f"{when}{batch}{told}"


def remove_line(on: bool, removable: int) -> str:
    """The one sentence printed under the auto-removal box.

    Written here rather than in the page for the same reason the ceiling's and
    the daily run's sentences are: it has to say what pressing this will do to
    *this* register, and the answer changes with what is on it. The promise
    about the playlist is repeated in every branch on purpose — it is the only
    question this control raises, and an answer that appears in two states out
    of three is not an answer.
    """
    if on:
        return (
            "A job comes off the register as soon as it finishes with nothing left to add. "
            "One that finished with failed videos stays, so you can resume and retry them. "
            "Your YouTube playlists are never touched."
        )
    if removable:
        jobs_word = f"{removable} finished job{'' if removable == 1 else 's'}"
        return (
            f"Turning this on removes the {jobs_word} already on the register, and every job "
            "that finishes from then on. Your YouTube playlists are never touched."
        )
    return (
        "Nothing on the register has finished yet, so turning this on changes nothing today — "
        "it takes each job off as it finishes. Your YouTube playlists are never touched."
    )


def standing_view() -> dict[str, Any]:
    """What the register says about the jobs that run themselves.

    Answers exactly two questions, because they are the only two a standing
    order raises: how many are on the books, and when does the next one go.
    """
    orders = db.standing_orders()
    due = scheduler.scheduler.due()
    alloc = quota.allocation()
    notifying = sum(1 for job in orders if job["notify"])
    return {
        "count": len(orders),
        "due": len(due),
        "blocked": sum(1 for job in orders if job["status"] == db.FAILED),
        "notifying": notifying,
        "telegram_ready": notify.configured(),
        # Ticked to report, with nowhere to report to. Worth its own line: the
        # user has asked to be told and would otherwise wait for a message that
        # can never arrive.
        "notify_unset": notifying > 0 and not notify.configured(),
        "starts_soon": bool(due) and not alloc.exhausted and not jobs.runner.busy,
        "waiting_for_credits": bool(due) and alloc.exhausted,
        "resets_in": quota.reset_human(),
        "interval": config.SCHEDULER_INTERVAL,
    }


def telegram_view() -> dict[str, Any]:
    orders = db.standing_orders()
    return {
        "configured": notify.configured(),
        "token": notify.masked_token(),
        "chat": notify.chat_id() or None,
        "token_from_container": notify.token_from_container(),
        "chat_from_container": notify.chat_from_container(),
        "last": notify.last_result(),
        "notifying": sum(1 for job in orders if job["notify"]),
        "standing": len(orders),
    }


def job_view(job) -> dict[str, Any]:
    counts = db.video_counts(job["id"])
    total = job["total_videos"] or 0
    added = counts[db.V_ADDED]
    # A job that never got as far as resolving its source still has to be
    # labelled honestly, so the kind is read back off what the user typed
    # rather than left at the column default.
    kind = (
        job["source_kind"]
        if job["source_id"]
        else youtube.parse_source_input(job["source_input"])[0]
    )
    view = {
        "id": job["id"],
        "ref": f"{job['id']:04d}",
        "status": job["status"],
        "status_label": STATUS_LABELS.get(job["status"], job["status"].upper()),
        "source_input": job["source_input"],
        "source_kind": kind,
        "source_is_playlist": kind == db.SOURCE_PLAYLIST,
        "source_label": "Playlist" if kind == db.SOURCE_PLAYLIST else "Channel",
        "source_id": job["source_id"],
        "source_title": job["source_title"] or job["source_input"],
        "source_owner": job["source_owner"],
        "playlist_id": job["playlist_id"],
        "playlist_title": job["playlist_title"],
        "playlist_description": job["playlist_description"],
        "playlist_url": (
            f"https://www.youtube.com/playlist?list={job['playlist_id']}"
            if job["playlist_id"]
            else None
        ),
        "total": total,
        "added": added,
        "failed": counts[db.V_FAILED],
        "skipped": counts[db.V_SKIPPED],
        "pending": counts[db.V_PENDING],
        # What the copy can ever contain: the source's own dead entries are not
        # a shortfall this job can make up, so they are excluded from the target.
        "copyable": total - counts[db.V_SKIPPED],
        "units_charged": job["units_charged"],
        "units_required": quota.estimate_units(
            total - counts[db.V_SKIPPED], needs_playlist=not job["playlist_id"]
        ),
        "units_remaining_for_job": (counts[db.V_PENDING] + counts[db.V_FAILED])
        * config.COST_PLAYLIST_ITEM_INSERT,
        # Measured against what is copyable, so a playlist carrying dead entries
        # still reaches 100% when everything that can be copied has been.
        "percent": (
            round(added / (total - counts[db.V_SKIPPED]) * 100, 1)
            if total - counts[db.V_SKIPPED] > 0
            else (100.0 if total else 0.0)
        ),
        "message": job["message"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "resumable": (
            job["status"] in db.RESUMABLE_STATUSES
            and total > 0
            and (counts[db.V_PENDING] + counts[db.V_FAILED]) > 0
        ),
        "is_running": job["status"] == db.RUNNING,
        "needs_preflight": total == 0,
        "run_limit": job["run_limit"],
        "scheduled": bool(job["scheduled"]),
        "notify": bool(job["notify"]),
        # A ticked job the runner will not touch. Printed rather than skipped in
        # silence: a daily run that has quietly stopped running is the one
        # thing this control must never become.
        "auto_blocked": bool(job["scheduled"]) and job["status"] == db.FAILED,
        "auto_ran_today": bool(
            job["scheduled"]
            and job["last_auto_run_at"]
            and job["last_auto_run_at"] >= quota.last_reset().isoformat(timespec="seconds")
        ),
    }
    # What the ceiling actually costs this run: once fewer videos are left than
    # the ceiling, the ceiling is no longer the thing that stops the job, and
    # printing it as though it were would be a lie by omission.
    view["run_stops_after"] = (
        min(job["run_limit"], counts[db.V_PENDING] + counts[db.V_FAILED])
        if job["run_limit"]
        else None
    )
    view["days_required"] = (
        quota.days_required(counts[db.V_PENDING] + counts[db.V_FAILED], not job["playlist_id"])
        if view["pending"] or view["failed"]
        else 0
    )
    # Both read the ceiling's effective figure above them.
    view["limit_line"] = limit_line(view)
    view["schedule_line"] = schedule_line(job, view)
    return view


def live_view(job_id: int) -> dict[str, Any]:
    state = jobs.runner.state
    if state is None or state.job_id != job_id:
        return {
            "phase": None,
            "position": None,
            "video_id": None,
            "title": None,
            "rate": None,
            "settled_this_run": 0,
        }
    return {
        "phase": state.phase,
        "position": state.current_position,
        "video_id": state.current_video_id,
        "title": state.current_title,
        "rate": state.rate_per_minute,
        # The ceiling counts attempts, so this is the figure the ceiling is
        # actually measured against — printed beside it while a run is going,
        # because a limit you cannot see is a limit you cannot trust.
        "settled_this_run": state.settled_this_run,
    }


def eta(view: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Two different questions: when does this job finish, and when does today's
    allocation run out? For a job larger than one day they have very different
    answers, and showing only the smaller one reads as a lie."""
    rate = live.get("rate")
    remaining = view["pending"] + view["failed"]
    if not rate or remaining <= 0:
        return {"seconds": None, "bound": None, "text": ""}
    affordable = quota.allocation().videos_affordable
    if affordable < remaining:
        seconds = int(max(affordable, 0) / rate * 60)
        return {"seconds": seconds, "bound": "allocation",
                "text": f"free credits run out {_span(seconds)}"}
    seconds = int(remaining / rate * 60)
    return {"seconds": seconds, "bound": "job", "text": f"finishes {_span(seconds)}"}


def _span(seconds: int) -> str:
    if seconds < 60:
        return "in under a minute"
    hours, remainder = divmod(seconds, 3600)
    minutes = round(remainder / 60)
    if hours:
        return f"in about {hours}h {minutes:02d}m"
    return f"in about {minutes} min"


def record_view(row) -> dict[str, Any]:
    return {
        "position": row["position"],
        "video_id": row["video_id"],
        "title": row["title"] or row["video_id"],
        "state": row["state"],
        "message": row["message"],
        "settled_at": row["settled_at"],
    }


def snapshot(job_id: int) -> dict[str, Any]:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    view = job_view(job)
    live = live_view(job_id)
    upcoming = db.next_pending_record(job_id)
    return {
        "job": view,
        "live": live,
        "allocation": allocation_view(),
        "eta": eta(view, live),
        "records": [record_view(r) for r in db.recent_records(job_id, limit=40)],
        "next_record": (
            {
                "position": upcoming["position"],
                "video_id": upcoming["video_id"],
                "title": upcoming["title"] or upcoming["video_id"],
            }
            if upcoming
            else None
        ),
    }


# --- The register, live ----------------------------------------------------
# A daily run spends credits while nobody is watching the job page. If the
# register is the page that happens to be open, it has to say so as it happens —
# a figure that only moves on reload is a figure the operator has to distrust.


# The register prints far less of a job than its own page does, and the stream
# is allowed to restrike only what is printed. Everything else on a record — the
# controls, the plates, whether a resume is offered at all — is server-rendered,
# and a run starting or ending is the one transition worth a reload.
ROW_FIELDS = (
    "id", "ref", "status", "status_label", "total", "added", "copyable", "failed",
    "skipped", "units_charged", "percent", "message", "run_limit", "run_stops_after",
    "scheduled", "notify", "auto_blocked", "is_running", "resumable",
    "limit_line", "schedule_line",
)


def run_settled() -> int:
    """How many records the run now going has settled against its ceiling.

    The ceiling counts attempts rather than successes, so this is the figure it
    is actually measured against — and the register prints it beside the ceiling
    for the same reason the job page does.
    """
    state = jobs.runner.state
    return state.settled_this_run if jobs.runner.busy and state else 0


def register_row(job) -> dict[str, Any]:
    view = job_view(job)
    return {key: view[key] for key in ROW_FIELDS}


def bulk_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The two controls that act on every job at once.

    Each is one plate that swings both ways rather than a pair of opposites: the
    honest label is the one naming the thing that is not yet true of every job,
    and once it is true of all of them the only remaining action is to lift it.

    A daily run is offered where it could actually run something — a
    finished job has nothing to start — and reporting is offered only over the
    orders that exist, because a report describes a run.
    """
    eligible = [row for row in rows if row["resumable"] or row["scheduled"]]
    standing = [row for row in rows if row["scheduled"]]
    # Counted over the whole table rather than the fifty records the page
    # prints: switching the box on sweeps the register, not the page.
    auto_remove = db.auto_remove_completed()
    removable = 0 if auto_remove else len(db.removable_completed())
    return {
        "auto_remove": auto_remove,
        "removable": removable,
        "remove_line": remove_line(auto_remove, removable),
        # One job is not "all jobs"; a bulk control over a single record is
        # furniture beside the record's own control.
        "shown": len(rows) > 1,
        "eligible": len(eligible),
        "standing": len(standing),
        "notifying": sum(1 for row in standing if row["notify"]),
        # Set on all while any eligible job is without one; lift once all have
        # it. With nothing to act on the plate is disabled either way, and it
        # reads as the offer rather than as the withdrawal of one — a control
        # that cannot be pressed must still say the true thing.
        "schedule_on": not eligible or any(not row["scheduled"] for row in eligible),
        "notify_on": not standing or any(not row["notify"] for row in standing),
    }


def register_snapshot() -> dict[str, Any]:
    """What the register may restrike without a reload.

    Deliberately not the bulk controls: their labels name the action that is not
    yet true of every job, and a label the browser writes for itself is a second
    copy of this app's own words. They change on the press that changes them.
    """
    rows = [register_row(job) for job in db.list_jobs(limit=50)]
    return {
        "allocation": allocation_view(),
        "running_id": jobs.runner.running_job_id(),
        "run_done": run_settled(),
        "jobs": rows,
        # Which records the register should be holding. A job removing itself
        # on completion takes a whole row away, and every control and count
        # around it is server-rendered against the set that was there — so a
        # change here is a reload, exactly as a run starting or ending is.
        "row_ids": [row["id"] for row in rows],
        # Rendered from the register's own partial, exactly as the schedule
        # endpoint does: the count and the "when does the next one go" line are
        # the sort of thing that goes quietly wrong in a second copy.
        "summary": templates.get_template("partials/orders.html").render(
            standing=standing_view()
        ),
    }


def connection_state() -> dict[str, Any]:
    return {
        "has_client_secret": youtube.has_client_secret(),
        "connected": youtube.is_connected(),
        "account": youtube.account_email(),
        "redirect_uri": youtube.redirect_uri(),
    }


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template,
        status_code=status_code,
        context={
            "conn": connection_state(),
            "allocation": allocation_view(),
            "standing": standing_view(),
            "available_update": updates.available_update(),
            **context,
        },
    )


# --- Error surfaces --------------------------------------------------------
# A bad URL or an unhandled fault must still land on the form, not on a JSON
# blob — the error page is part of the world, not an escape from it.


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        detail = "There is no page at this address, and no job with this number."
    elif exc.status_code == 405:
        detail = "That action cannot be reached this way. Go back and try the control again."
    else:
        detail = str(exc.detail)
    return render(
        request, "error.html", code=exc.status_code, detail=detail,
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    # Deliberately does NOT call render(): that reads the token file, the
    # database and the quota ledger, and a fault originating in any of those
    # would raise again here and strip the page away entirely. The 500 page
    # renders from the exception alone.
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        status_code=500,
        context={
            "conn": {},
            "allocation": None,
            "available_update": None,
            "code": 500,
            "detail": (
                f"{type(exc).__name__}: {exc}. This is a fault in the app, not in your "
                "settings. Any running playlist mirror keeps its progress — nothing is lost."
            ),
        },
    )


# --- Setup -----------------------------------------------------------------


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, notice: Optional[str] = None, error: Optional[str] = None):
    return render(request, "setup.html", allowance=allowance_view(), notice=notice, error=error)


@app.post("/setup/allowance")
def set_allowance(units: str = Form(...)):
    """Change the daily allowance without restarting the container.

    The figure is only ever the operator's claim about what Google granted this
    project — the API will not report it — so this is stored and metered
    against, never trusted over YouTube's own refusal.
    """
    raw = units.strip()
    # 10,000 / 10.000 / 10 000 are one figure typed on three keyboards. Grouping
    # is stripped only when the whole string is grouped digits, so "10.5" still
    # fails the parse below instead of silently becoming 105.
    if re.fullmatch(r"\d{1,3}(?:[.,\u00a0 ']\d{3})+", raw):
        raw = re.sub(r"[.,\u00a0 ']", "", raw)
    try:
        value = int(raw)
    except ValueError:
        return RedirectResponse(
            "/setup?error="
            + quote_plus("The allowance has to be a whole number of units, like 10000."),
            status_code=303,
        )
    if value < config.COST_PLAYLIST_ITEM_INSERT:
        return RedirectResponse(
            "/setup?error="
            + quote_plus(
                f"An allowance below {config.COST_PLAYLIST_ITEM_INSERT} units cannot pay for a "
                "single video, so nothing could ever run."
            ),
            status_code=303,
        )
    if value > quota.LIMIT_CEILING:
        return RedirectResponse(
            "/setup?error="
            + quote_plus(
                f"That is larger than any allowance Google grants. The most this accepts is "
                f"{quota.LIMIT_CEILING:,} units."
            ),
            status_code=303,
        )

    held = quota.set_daily_limit(value)
    alloc = quota.allocation()
    notice = (
        f"Daily allowance set to {value:,} units — about {alloc.videos_per_day:,} videos a day."
    )
    if held:
        notice += (
            " YouTube already refused calls today, so the day stays spent and the new figure "
            f"first applies at the reset ({quota.reset_human()})."
        )
    return RedirectResponse("/setup?notice=" + quote_plus(notice), status_code=303)


def safe_return_path(raw: str) -> str:
    """Where a control on the shared allocation band sends the user back to.

    The band is printed on every page, so its one control has to return to the
    page it was pressed on. Only this app's own paths are honoured — a value
    that is not a plain absolute path is not repaired, it is replaced with the
    register, because an open redirect is not a formatting problem.
    """
    if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        return raw.split("?", 1)[0].split("#", 1)[0]
    return "/"


_PLAYLIST_PREFIX = "https://www.youtube.com/playlist?list="


def safe_playlist_url(raw: Optional[str]) -> str:
    """The playlist link a notice is allowed to carry.

    A removal notice hands back the playlist the job built, and it travels as a
    query parameter — which means anyone can put a link in front of this app's
    own voice by sending the user a crafted URL. Only a YouTube playlist
    address, with a plain playlist id after it, is printed; anything else is
    dropped rather than corrected.
    """
    if not raw or not raw.startswith(_PLAYLIST_PREFIX):
        return ""
    list_id = raw[len(_PLAYLIST_PREFIX):]
    if not list_id or not all(c.isalnum() or c in "-_" for c in list_id):
        return ""
    return raw


@app.post("/allowance/reset")
def reset_allowance(back: str = Form("/")):
    """Rule off the day's tally by hand, when the printed reset time is wrong.

    This resets nothing at Google — it cannot, and the notice says so. It moves
    this app's own counting line to now, so a playlist mirror is allowed to make the
    one call that will settle the question. If the day really is spent, that
    call comes back refused, the runner holds on it exactly as it does at the
    wall, and the band reads used up again a second later. One refused call is
    the entire cost of being wrong, and it is charged nothing by YouTube.
    """
    was_pinned = quota.pinned_today()
    quota.mark_reset()
    alloc = quota.allocation()
    notice = (
        f"Credit count ruled off — this app now reads {alloc.remaining:,} units for today, "
        f"about {alloc.videos_affordable:,} videos. Nothing was reset at Google: if its day "
        "has not turned over yet, YouTube refuses the next call, nothing is added, and the "
        "count fills straight back in."
    )
    if was_pinned:
        notice += (
            " YouTube had already refused a call since the last reset, so that is the more "
            "likely outcome here."
        )
    return RedirectResponse(
        f"{safe_return_path(back)}?notice={quote_plus(notice)}", status_code=303
    )


@app.post("/setup/client-secret")
async def upload_client_secret(file: UploadFile):
    raw = await file.read()
    if len(raw) > 64 * 1024:
        return RedirectResponse(
            "/setup?error=That+file+is+too+large+to+be+an+OAuth+client+file.", status_code=303
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return RedirectResponse(
            "/setup?error=That+file+is+not+valid+JSON.+Download+it+again+from+the+"
            "Credentials+page.",
            status_code=303,
        )
    if not any(key in parsed for key in ("installed", "web")):
        return RedirectResponse(
            "/setup?error=That+JSON+has+no+%22installed%22+or+%22web%22+section%2C+so+it+is+"
            "not+an+OAuth+client+file.",
            status_code=303,
        )
    config.ensure_dirs()
    config.CLIENT_SECRET_FILE.write_bytes(raw)
    return RedirectResponse("/setup?notice=Credentials+saved.", status_code=303)


@app.get("/oauth/start")
def oauth_start():
    if not youtube.has_client_secret():
        return RedirectResponse("/setup?error=Add+the+OAuth+client+file+first.", status_code=303)
    state = secrets.token_urlsafe(24)
    try:
        flow = youtube.build_flow()
        # select_account forces Google's account chooser instead of silently
        # reusing the browser's default session, which is the only way to reach
        # a second Google account — or a brand channel, whose picker follows the
        # account choice — from this instance.
        url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent select_account",
            state=state,
        )
    except Exception as exc:  # noqa: BLE001 — a malformed client file is a user-fixable error
        return RedirectResponse(
            "/setup?error="
            + quote_plus(
                f"That OAuth client file could not be used ({type(exc).__name__}). Download it "
                "again from the Credentials page and make sure it is the client file, not the "
                "API key."
            ),
            status_code=303,
        )
    # Read back rather than generated here: the library overwrites whatever is
    # set beforehand when it autogenerates, so after the call is the only moment
    # the verifier behind the challenge Google just received is knowable.
    _oauth_state.clear()
    _oauth_state[state] = flow.code_verifier or ""
    return RedirectResponse(url, status_code=303)


@app.get("/oauth/callback")
def oauth_callback(request: Request, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"/setup?error=Google+returned%3A+{error}", status_code=303)
    if not state or state not in _oauth_state:
        return RedirectResponse(
            "/setup?error=That+sign-in+link+expired.+Start+the+sign-in+again.", status_code=303
        )
    code_verifier = _oauth_state.pop(state, None)
    try:
        flow = youtube.build_flow(state=state)
        # This is a second Flow object: the one that produced the code challenge
        # lived in the /oauth/start request and is long gone. Without its
        # verifier restored here Google answers "invalid_grant: Missing code
        # verifier" and no token is ever issued.
        if code_verifier:
            flow.code_verifier = code_verifier
        # Rebuild the callback URL from PUBLIC_BASE_URL rather than trusting
        # request.url: behind a reverse proxy the request arrives as plain http
        # on an internal host, which would not match the registered redirect URI.
        authorization_response = f"{config.PUBLIC_BASE_URL}{request.url.path}?{request.url.query}"
        flow.fetch_token(authorization_response=authorization_response)
        youtube.save_credentials(flow.credentials)
    except Exception as exc:  # noqa: BLE001 - shown to the user verbatim
        return RedirectResponse(f"/setup?error=Sign-in+failed%3A+{exc}", status_code=303)
    return RedirectResponse("/?notice=Google+account+connected.", status_code=303)


@app.post("/setup/disconnect")
def disconnect():
    youtube.disconnect()
    return RedirectResponse("/setup?notice=Signed+out.+The+stored+token+was+deleted.",
                            status_code=303)


# --- Telegram --------------------------------------------------------------
# Its own surface rather than a fifth step on the Google page: it has nothing to
# do with Google, it is optional, and a stranger setting it up needs the two
# steps in front of them rather than folded into someone else's flow.


def _telegram(notice: str = "", error: str = "") -> RedirectResponse:
    query = f"?notice={quote_plus(notice)}" if notice else f"?error={quote_plus(error)}"
    return RedirectResponse(f"/notify{query}", status_code=303)


@app.get("/notify", response_class=HTMLResponse)
def telegram_page(request: Request, notice: Optional[str] = None, error: Optional[str] = None):
    return render(request, "notify.html", telegram=telegram_view(), notice=notice, error=error)


@app.post("/notify/token")
def set_bot_token(token: str = Form("")):
    """Store the bot token. Blank means keep the one on file.

    The field is rendered empty every time — a stored secret is never printed
    back into a page — so a blank submission is far more likely to be "I did not
    want to change this" than "delete it". Clearing it has its own control.
    """
    raw = token.strip()
    if not raw:
        return _telegram(error="Paste the bot token, or use Forget these settings to clear it.")
    if ":" not in raw or not raw.split(":", 1)[0].isdigit():
        return _telegram(
            error=(
                "That does not look like a bot token. @BotFather gives you one long line: "
                "digits, a colon, then letters — copy the whole thing."
            )
        )
    notify.set_bot_token(raw)
    return _telegram(notice="Bot token saved.")


@app.post("/notify/chat")
def set_chat(chat: str = Form("")):
    raw = chat.strip()
    if not raw:
        return _telegram(error="Enter the chat id, or use Find my chat to read it off the bot.")
    # Group and channel ids are negative and long; a user id is a plain integer.
    # Nothing else is a chat id, and storing a username here would fail later,
    # at the one moment nobody is watching.
    if not re.fullmatch(r"-?\d{1,20}", raw):
        return _telegram(
            error=(
                "A chat id is a number, like 123456789 for you or -1001234567890 for a group. "
                "A @username will not work here — use Find my chat below."
            )
        )
    notify.set_chat_id(raw)
    return _telegram(notice=f"Chat set to {raw}.")


@app.post("/notify/discover")
async def discover_chat():
    """Read the chat id off whatever was last said to the bot.

    Telegram does not show you your own chat id anywhere, and every guide
    answers that with "message this third-party bot", which for a self-hoster
    means handing a stranger the conversation. The bot already knows, so this
    asks it.
    """
    if not notify.bot_token():
        return _telegram(error="Save the bot token first — that is what this reads through.")
    try:
        found, label = await asyncio.to_thread(notify.discover_chat)
    except notify.NotifyError as error:
        return _telegram(error=str(error))
    if not found:
        return _telegram(
            error=(
                "The bot has not been messaged yet. Open it in Telegram, send it anything at "
                "all, then press this again. Telegram only keeps those messages for 24 hours."
            )
        )
    notify.set_chat_id(found)
    named = f" ({label})" if label else ""
    return _telegram(notice=f"Chat found and saved: {found}{named}.")


@app.post("/notify/test")
async def send_test():
    if not notify.configured():
        return _telegram(error="Set the bot token and the chat first.")
    try:
        await asyncio.to_thread(notify.send, notify.test_message())
    except notify.NotifyError as error:
        return _telegram(error=str(error))
    return _telegram(notice="Test message sent. If it did not arrive, the chat id is not yours.")


@app.post("/notify/forget")
def forget_telegram():
    notify.set_bot_token("")
    notify.set_chat_id("")
    return _telegram(
        notice=(
            "Token and chat cleared. Daily runs still run — they just report nowhere "
            "until this is set up again."
        )
    )


# --- Register --------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    notice: Optional[str] = None,
    error: Optional[str] = None,
    notice_url: Optional[str] = None,
):
    conn = connection_state()
    if not conn["connected"]:
        return RedirectResponse("/setup", status_code=303)
    rows = [job_view(job) for job in db.list_jobs(limit=50)]
    return render(
        request,
        "index.html",
        jobs=rows,
        bulk=bulk_view(rows),
        run_done=run_settled(),
        notice=notice,
        notice_url=safe_playlist_url(notice_url),
        error=error,
        running_id=jobs.runner.running_job_id(),
        cost_per_video=config.COST_PLAYLIST_ITEM_INSERT,
    )


def parse_run_limit(limit_on: str, run_limit: str) -> tuple[Optional[int], Optional[str]]:
    """Read the run ceiling off a form as (limit, error message).

    The checkbox is the switch and the number is only consulted when it is on,
    so a number left behind from an earlier submission never silently caps a run
    the user meant to leave uncapped.
    """
    if not limit_on:
        return None, None
    raw = run_limit.strip()
    if not raw:
        return None, "Set a number of videos for the run limit, or untick it."
    try:
        value = int(raw)
    except ValueError:
        return None, "The run limit has to be a whole number of videos."
    if value < 1:
        return None, "A run limit has to be at least one video."
    return value, None


@app.post("/jobs")
async def submit_job(
    # Defaulted rather than required: an empty field is "missing" to Pydantic,
    # and a 422 JSON blob is not an answer this app is allowed to give. The
    # empty case belongs on the form, in the form's own words.
    source: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
):
    source = source.strip()
    title = title.strip()
    if not source or not title:
        return RedirectResponse(
            "/?error=A+channel+or+playlist+and+a+playlist+title+are+both+required.",
            status_code=303,
        )

    # No run ceiling is set here: it belongs to a run, and the control that
    # starts one carries it. A job begins with none, which is "spend the day's
    # credits", and the first start is where that can be changed.
    job_id = db.create_job(source, title, description.strip())
    try:
        await asyncio.to_thread(jobs.runner.preflight, job_id)
    except youtube.SourceNotFound as error:
        db.update_job(job_id, status=db.FAILED, message=str(error), finished_at=db.utcnow())
    except youtube.QuotaExceeded:
        db.update_job(
            job_id,
            status=db.HELD_QUOTA,
            message=(
                "Today's free credits are already used up, so the source could not even be "
                f"read. Try again after the reset ({quota.reset_human()})."
            ),
            finished_at=db.utcnow(),
        )
    except youtube.NotAuthorised as error:
        db.update_job(job_id, status=db.FAILED, message=str(error), finished_at=db.utcnow())
    except Exception as error:  # noqa: BLE001
        db.update_job(
            job_id,
            status=db.FAILED,
            message=f"{type(error).__name__}: {error}",
            finished_at=db.utcnow(),
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(
    request: Request,
    job_id: int,
    error: Optional[str] = None,
    notice: Optional[str] = None,
):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    data = snapshot(job_id)
    return render(
        request,
        "job.html",
        error=error,
        notice=notice,
        cost_per_video=config.COST_PLAYLIST_ITEM_INSERT,
        another_running=(
            jobs.runner.busy and jobs.runner.running_job_id() != job_id
        ),
        running_id=jobs.runner.running_job_id(),
        **data,
    )


@app.post("/jobs/{job_id}/start")
def start_job(job_id: int):
    """Start or resume a run.

    It carries no ceiling of its own. The ceiling is a setting the job holds and
    both surfaces print, saved by the control that shows it — so a run started
    from the register and one started from the job page are governed by the same
    stored figure, and there is only ever one place it can be changed from.
    """
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    try:
        jobs.runner.start(job_id)
    except jobs.JobBusy as error:
        return RedirectResponse(f"/jobs/{job_id}?error={quote_plus(str(error))}", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/limit")
async def set_limit(
    request: Request,
    job_id: int,
    limit_on: str = Form(""),
    run_limit: str = Form(""),
    back: str = Form("/"),
):
    """Set or lift the ceiling on a run.

    The mirror of the daily run in every respect: printed on both surfaces,
    posted from both, and answering in whichever form the caller asked for —
    JSON when the page is driving it in place, a redirect back to the row when
    the browser is submitting the form itself.
    """
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")

    wants_json = "application/json" in (request.headers.get("accept") or "")

    # A run reads the ceiling once, when it starts. Changing it mid-run would
    # silently do nothing, and a control that silently does nothing is worse
    # than one that is not there — so while a job runs the figure is printed,
    # not enterable, and this says so if the request arrives anyway.
    if jobs.runner.running_job_id() == job_id:
        message = (
            "This job is running, and a run reads its limit once when it starts. "
            "Change it after the run holds."
        )
        if wants_json:
            return {"error": message}
        return RedirectResponse(
            f"{safe_return_path(back)}?error={quote_plus(message)}", status_code=303
        )

    limit, error = parse_run_limit(limit_on, run_limit)
    if error:
        if wants_json:
            return {"error": error}
        return RedirectResponse(
            f"{safe_return_path(back)}?error={quote_plus(error)}", status_code=303
        )

    db.update_job(job_id, run_limit=limit)
    updated = db.get_job(job_id)
    assert updated is not None
    view = job_view(updated)

    if wants_json:
        return {
            "run_limit": view["run_limit"],
            "line": view["limit_line"],
            # The ceiling is quoted inside the daily run's own sentence and
            # in the plan ledger, so both are restruck from the same answer
            # rather than left saying what was true a moment ago.
            "schedule_line": view["schedule_line"],
            "stops_after": view["run_stops_after"],
        }
    return RedirectResponse(f"{safe_return_path(back)}#job-{job_id}", status_code=303)


@app.post("/jobs/{job_id}/schedule")
async def set_schedule(
    request: Request,
    job_id: int,
    scheduled: str = Form(""),
    notify_on: str = Form(""),
    back: str = Form("/"),
):
    """Tick or lift a daily run.

    Printed on two surfaces and posted from both, so it answers in whichever
    form the caller asked for: JSON when the page is driving it in place, a
    redirect back to the row when the browser is submitting the form itself.
    Notification is never stored without a daily run — it describes an
    automatic run, and without one there is no run to describe.
    """
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")

    on = bool(scheduled)
    telling = bool(notify_on) and on
    db.set_schedule(job_id, on, telling)

    if "application/json" in (request.headers.get("accept") or ""):
        updated = db.get_job(job_id)
        assert updated is not None
        summary = standing_view()
        return {
            "scheduled": on,
            "notify": telling,
            "line": job_view(updated)["schedule_line"],
            # Rendered from the register's own partial rather than rebuilt in
            # the browser: the count and the "when does the next one go" line
            # are the sort of thing that goes quietly wrong in a second copy.
            "summary": templates.get_template("partials/orders.html").render(
                standing=summary
            ),
        }
    return RedirectResponse(
        f"{safe_return_path(back)}#job-{job_id}", status_code=303
    )


# --- Every job at once -----------------------------------------------------
# Two plates, each swinging both ways. They exist because these two settings are
# almost always wanted uniformly — a register of twenty jobs is twenty identical
# decisions — and because ticking twenty boxes by hand is where a person makes
# the one mistake the whole daily-run design is built to avoid.


@app.post("/jobs/schedule-all")
def schedule_all(on: str = Form("")):
    """Set or lift a daily run on every job it can mean something for."""
    turning_on = bool(on)
    changed = 0
    for job in db.list_jobs(limit=50):
        view = job_view(job)
        if turning_on:
            # A finished job, or one removed in all but name, has nothing to run, so
            # ticking it would put a daily run on the register that can
            # never fire — exactly the quiet lie the order mark must not tell.
            if view["scheduled"] or not view["resumable"]:
                continue
            db.set_schedule(job["id"], True, bool(job["notify"]))
        else:
            if not view["scheduled"]:
                continue
            db.set_schedule(job["id"], False, False)
        changed += 1

    if not changed:
        return RedirectResponse(
            "/?notice=" + quote_plus(
                "Nothing to change — every job was already as you asked."
            ),
            status_code=303,
        )
    jobs_word = f"{changed} job{'' if changed == 1 else 's'}"
    if turning_on:
        notice = (
            f"A daily run is now set on {jobs_word}. Each starts itself once a credit "
            f"day, and the register says when the next one goes."
        )
    else:
        notice = f"Daily runs turned off for {jobs_word}. Nothing starts itself now."
    return RedirectResponse(f"/?notice={quote_plus(notice)}", status_code=303)


@app.post("/jobs/notify-all")
def notify_all(on: str = Form("")):
    """Switch the Telegram report on or off for every daily run.

    Only over jobs that have one: a report describes an automatic run, and a job
    with no daily run has none to describe.
    """
    turning_on = bool(on)
    changed = 0
    for job in db.standing_orders():
        if bool(job["notify"]) == turning_on:
            continue
        db.set_schedule(job["id"], True, turning_on)
        changed += 1

    if not changed:
        return RedirectResponse(
            "/?notice=" + quote_plus(
                "Nothing to change — every daily run was already as you asked."
            ),
            status_code=303,
        )
    orders = f"{changed} daily run{'' if changed == 1 else 's'}"
    if turning_on:
        notice = f"{orders} will report to Telegram once they have all run."
        if not notify.configured():
            notice += (
                " No bot token and chat are set yet, so those messages have nowhere to go —"
                " set up Telegram and the runs report from then on."
            )
    else:
        notice = f"{orders} will no longer report to Telegram. The runs happen either way."
    return RedirectResponse(f"/?notice={quote_plus(notice)}", status_code=303)


@app.post("/jobs/auto-remove")
def set_auto_remove(on: str = Form("")):
    """Keep, or stop keeping, a job on the register once it has finished.

    One stored decision for the whole register rather than a box per job: this
    is housekeeping about the list, not an instruction to any one playlist mirror, and
    a fourth box on every record would say otherwise.

    Switching it on sweeps what is already there. That is deliberate and it is
    printed on the control before it is pressed — a setting that tidied only
    future jobs would leave the operator looking at the very register they just
    asked to be rid of, wondering whether it had worked.
    """
    turning_on = bool(on)
    db.set_auto_remove_completed(turning_on)
    if not turning_on:
        return RedirectResponse(
            "/?notice=" + quote_plus(
                "Finished jobs now stay on the register until you remove them."
            ),
            status_code=303,
        )
    swept = jobs.remove_completed()
    if swept:
        notice = (
            f"{swept} finished job{'' if swept == 1 else 's'} removed, and each job from now "
            "on comes off the register as it finishes. Your YouTube playlists were not touched."
        )
    else:
        notice = (
            "Each job now comes off the register as soon as it finishes cleanly. Nothing on "
            "the register had finished yet, so nothing was removed."
        )
    return RedirectResponse(f"/?notice={quote_plus(notice)}", status_code=303)


@app.post("/jobs/{job_id}/refresh")
async def refresh_job_source(job_id: int):
    """Re-read the source into an existing job, so uploads made since it was
    registered are copied too.

    Unlike registration, a failure here never marks the job failed: the job's
    own state is untouched by a read that did not happen, and turning a healthy
    paused job into a FAILED one because the network blinked would be a lie about it.
    Every outcome lands back on the job page as a line.
    """
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    if jobs.runner.running_job_id() == job_id:
        return RedirectResponse(
            f"/jobs/{job_id}?error=" + quote_plus(
                "This job is running. Stop it before re-reading the source — a run works "
                "from the listing it started with."
            ),
            status_code=303,
        )

    try:
        result = await asyncio.to_thread(jobs.runner.refresh_source, job_id)
    except youtube.QuotaExceeded:
        return RedirectResponse(
            f"/jobs/{job_id}?error=" + quote_plus(
                "Today's free credits are used up, so the source could not be read. "
                f"Nothing about this job changed. Try again after the reset "
                f"({quota.reset_human()})."
            ),
            status_code=303,
        )
    except (youtube.SourceNotFound, youtube.NotAuthorised) as error:
        return RedirectResponse(f"/jobs/{job_id}?error={quote_plus(str(error))}", status_code=303)
    except Exception as error:  # noqa: BLE001 - surfaced to the user verbatim
        return RedirectResponse(
            f"/jobs/{job_id}?error={quote_plus(f'{type(error).__name__}: {error}')}",
            status_code=303,
        )

    counted = f"{result.source_total:,} video{'' if result.source_total == 1 else 's'}"
    if result.new_videos or result.revived or result.dropped:
        found = []
        if result.new_videos:
            found.append(f"{result.new_videos:,} new")
        if result.revived:
            found.append(f"{result.revived:,} copyable again")
        if result.dropped:
            found.append(f"{result.dropped:,} gone from the source")
        notice = f"Source re-read: {counted}, {', '.join(found)}. Cost {result.units:,} units."
    else:
        notice = f"Source re-read: {counted}, nothing new to copy. Cost {result.units:,} units."
    return RedirectResponse(f"/jobs/{job_id}?notice={quote_plus(notice)}", status_code=303)


@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: int):
    if not jobs.runner.cancel(job_id):
        return RedirectResponse(
            f"/jobs/{job_id}?error=That+job+is+not+running.", status_code=303
        )
    # Pressing Stop on a job that starts itself has to mean something, and the
    # only honest meaning is that it stops starting itself: left ticked, the
    # daily run would begin the same job again within the minute and the
    # control would read as broken. Lifted here and said out loud, never
    # silently.
    job = db.get_job(job_id)
    if job is not None and job["scheduled"]:
        db.set_schedule(job_id, False, False)
        return RedirectResponse(
            f"/jobs/{job_id}?notice=" + quote_plus(
                "Stopping. The daily run was turned off too, so this job will not start "
                "itself again — tick it back on when you want it to."
            ),
            status_code=303,
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/remove")
def remove_job(job_id: int):
    """Take one job off the register by hand.

    Only the record goes. The playlist it built stays on YouTube exactly as it
    is, which is the whole reason this is a removal and not a deletion — and it
    is said on the control, in the confirmation, and again in the notice, because
    "will this delete my playlist?" is the only question this button raises.
    """
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    if jobs.runner.running_job_id() == job_id:
        return RedirectResponse(
            f"/jobs/{job_id}?error=Stop+the+job+before+removing+it.", status_code=303
        )
    playlist_url = (
        f"https://www.youtube.com/playlist?list={job['playlist_id']}"
        if job["playlist_id"]
        else ""
    )
    db.delete_job(job_id)
    notice = (
        f"Job {job_id:04d} removed from this app. Your YouTube playlist was not touched."
    )
    target = f"/?notice={quote_plus(notice)}"
    if playlist_url:
        target += f"&notice_url={quote_plus(playlist_url)}"
    return RedirectResponse(target, status_code=303)


@app.get("/jobs/{job_id}/state")
def job_state(job_id: int):
    return snapshot(job_id)


@app.get("/events")
async def register_events():
    """The register, streamed.

    A daily run spends credits with nobody on the job page. Whoever has the
    register open must see that happen as it happens, not on the next reload:
    the allocation band, the record's figures and the orders slip all come from
    the database, so this is the same snapshot-and-diff the job page uses, cut
    down to what a register record actually prints.
    """

    async def stream():
        previous: Optional[str] = None
        idle_ticks = 0
        while True:
            payload = await asyncio.to_thread(register_snapshot)
            body = json.dumps(payload, default=str)
            if body != previous:
                previous = body
                idle_ticks = 0
                yield f"data: {body}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks % 15 == 0:
                    yield ": keep-alive\n\n"
            # Slow when nothing is running — the register is a page people leave
            # open for days, and it must cost nothing to do so.
            await asyncio.sleep(0.9 if payload["running_id"] else 3)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: int):
    """Server-sent events. The database is the source of truth, so the stream
    survives a reload, a second tab, or a reconnect with no extra machinery."""

    async def stream():
        previous: Optional[str] = None
        idle_ticks = 0
        while True:
            try:
                payload = await asyncio.to_thread(snapshot, job_id)
            except HTTPException:
                # The job is not there any more. Usually it was removed by
                # hand from another tab; with auto-removal on it may have just
                # finished under the very eyes watching it, and then the run's
                # own result is only ever printed once — here. The farewell the
                # register wrote travels with the event so the page lands on a
                # sentence rather than on an empty register.
                farewell = jobs.runner.removals.get(job_id, {})
                payload = json.dumps({
                    "notice": farewell.get("notice", ""),
                    "playlist_url": farewell.get("playlist_url", ""),
                })
                yield f"event: gone\ndata: {payload}\n\n"
                return
            body = json.dumps(payload, default=str)
            if body != previous:
                previous = body
                idle_ticks = 0
                yield f"data: {body}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks % 15 == 0:
                    yield ": keep-alive\n\n"
            if not payload["job"]["is_running"]:
                # Terminal states still tick slowly, so a resume started in
                # another tab is picked up here too.
                await asyncio.sleep(3)
            else:
                await asyncio.sleep(0.7)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
