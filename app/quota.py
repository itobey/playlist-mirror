"""The daily allocation.

The YouTube Data API publishes no remaining-quota endpoint, so every figure this
module produces is a local projection: units this app has charged itself since
the last reset, subtracted from the configured daily limit. It is an estimate,
and the UI is required to say so. The authoritative signal is the API's own
403 quotaExceeded, which overrides anything computed here.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db


SETTING_DAILY_LIMIT = "quota_daily_limit"

# A reset the operator declared by hand, as an ISO timestamp. It exists because
# the reset time this app prints is computed from a configured timezone, and a
# configured timezone can be wrong — a missing tzdata falls back to a fixed
# -08:00 that drifts an hour under daylight saving, and an operator is free to
# set QUOTA_RESET_TZ to something that is not Pacific at all. When the printed
# clock disagrees with Google's real one, this lets the tally be ruled off now
# rather than at a midnight that has already passed. It is deliberately not a
# stored flag that has to be cleared: the next real midnight simply overtakes
# it and it stops meaning anything.
SETTING_RESET_MARK = "quota_reset_mark"

# The ledger row written when YouTube itself refused a call. Its presence today
# means the day's credits are gone whatever figure the operator has configured.
PIN_OP = "quotaExceeded.reconciliation"

# Google's largest published grants are in the millions; past this a figure is a
# typo, and a typo here would make every estimate on every page meaningless.
LIMIT_CEILING = 10_000_000


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.QUOTA_RESET_TZ)
    except Exception:
        # No tzdata on this machine: fall back to a fixed -08:00, which keeps
        # the reset within an hour of the real one year-round.
        return timezone(timedelta(hours=-8))  # type: ignore[return-value]


def _reset_mark() -> datetime | None:
    """The operator's hand-declared reset, if one still stands."""
    row = db.get_setting(SETTING_RESET_MARK)
    if row is None:
        return None
    try:
        stamp = datetime.fromisoformat(row["value"])
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def last_reset() -> datetime:
    """Most recent midnight in the quota timezone, as an aware UTC datetime.

    A hand-declared reset wins over that midnight while it is newer than it —
    which is the whole point, since the operator only reaches for it when the
    computed midnight is the thing that is wrong. It is ignored once the clock
    has passed it into a new day, and ignored outright if it reads in the
    future, because a mark from a skewed clock must not hide real spending.
    """
    tz = _tz()
    local_now = datetime.now(tz)
    local_midnight = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    midnight = local_midnight.astimezone(timezone.utc)

    mark = _reset_mark()
    # A minute of tolerance on the upper bound, because the mark is written one
    # second ahead (see mark_reset) and a container clock is not a metrology
    # lab. Anything further into the future is a clock fault, not a decision,
    # and must not be allowed to hide a day of real spending.
    horizon = datetime.now(timezone.utc) + timedelta(minutes=1)
    if mark is not None and midnight < mark <= horizon:
        return mark
    return midnight


def next_reset() -> datetime:
    tz = _tz()
    local_now = datetime.now(tz)
    local_midnight = datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc)


def seconds_until_reset() -> int:
    return max(0, int((next_reset() - datetime.now(timezone.utc)).total_seconds()))


@dataclass
class Allocation:
    limit: int
    charged: int
    reserve: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.charged)

    @property
    def spendable(self) -> int:
        """What a job may actually consume, holding the reserve back."""
        return max(0, self.remaining - self.reserve)

    @property
    def videos_affordable(self) -> int:
        return self.spendable // config.COST_PLAYLIST_ITEM_INSERT

    @property
    def videos_per_day(self) -> int:
        """What a whole fresh allowance is worth in videos.

        Not the same figure as videos_affordable, which is what is left of
        today: this one explains the rate, and it must be derived rather than
        written down, because the allowance is the operator's to change.
        """
        return max(0, self.limit - self.reserve) // config.COST_PLAYLIST_ITEM_INSERT

    @property
    def percent_charged(self) -> float:
        if self.limit <= 0:
            return 0.0
        return min(100.0, round(self.charged / self.limit * 100, 1))

    @property
    def exhausted(self) -> bool:
        return self.spendable < config.COST_PLAYLIST_ITEM_INSERT


def daily_limit() -> int:
    """The allowance in force: the operator's stored figure, else the container's.

    Read on every call rather than cached, because the whole point of storing it
    is that a change reaches the next insert without restarting anything — the
    runner asks for an allocation before every video.
    """
    row = db.get_setting(SETTING_DAILY_LIMIT)
    if row is None:
        return config.QUOTA_DAILY_LIMIT
    try:
        stored = int(row["value"])
    except (TypeError, ValueError):
        return config.QUOTA_DAILY_LIMIT
    # Same floor as the container value: an allowance that cannot pay for one
    # insert would divide the whole UI by zero.
    return max(config.COST_PLAYLIST_ITEM_INSERT, min(LIMIT_CEILING, stored))


def set_daily_limit(units: int) -> bool:
    """Store the allowance. Returns whether it must wait for the reset.

    Raising the figure must never resurrect a day YouTube has already refused.
    When the tally was pinned by an authoritative 403, the credits are gone no
    matter what is configured, so the pin is simply re-struck against the new
    limit: the band goes on reading used-up until the reset, which is the first
    moment a larger allowance can mean anything.
    """
    db.set_setting(SETTING_DAILY_LIMIT, str(int(units)))
    if not pinned_today():
        return False
    shortfall = allocation().remaining
    if shortfall > 0:
        db.charge(PIN_OP, shortfall, job_id=None)
    return True


def mark_reset() -> None:
    """Rule the tally off at this moment and count the day from here.

    Nothing is deleted: every charge stays in the ledger, including the pin
    YouTube's own refusal wrote. They simply fall on the far side of the line,
    which is what makes this safe to press — the record of what was really
    called today survives being wrong about when today began.
    """
    # One second ahead of now, because the ledger keeps second precision and a
    # charge written in this same second belongs to the day being ruled off —
    # in particular the pin YouTube's refusal wrote, which is very often the
    # thing that put this control on the page in the first place.
    stamp = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
    db.set_setting(SETTING_RESET_MARK, stamp.isoformat(timespec="seconds"))


def pinned_today() -> bool:
    """Whether YouTube itself has refused a call since the tally last started.

    The one figure on this band that is not a projection: it is Google's own
    answer, and it is the difference between 'this app thinks the day is spent'
    and 'the day is spent'.
    """
    return db.has_charge_since(PIN_OP, last_reset().isoformat(timespec="seconds"))


def allocation() -> Allocation:
    charged = db.units_since(last_reset().isoformat(timespec="seconds"))
    return Allocation(
        limit=daily_limit(),
        charged=charged,
        reserve=config.QUOTA_RESERVE,
    )


def estimate_units(video_count: int, needs_playlist: bool = True) -> int:
    """What a job of this size costs, start to finish.

    Enumerating the uploads is one list call per 50 videos at 1 unit each;
    creating the playlist is 50; every insert is 50.
    """
    listing = max(1, -(-video_count // 50)) * config.COST_LIST
    creation = config.COST_PLAYLIST_INSERT if needs_playlist else 0
    inserts = video_count * config.COST_PLAYLIST_ITEM_INSERT
    return listing + creation + inserts + (2 * config.COST_LIST)  # + channel lookups


def days_required(video_count: int, needs_playlist: bool = True) -> int:
    """How many fresh allocations a job of this size needs, including today's
    remaining one."""
    alloc = allocation()
    units = estimate_units(video_count, needs_playlist)
    if units <= alloc.spendable:
        return 1
    per_day = max(1, alloc.limit - config.QUOTA_RESERVE)
    return 1 + -(-(units - alloc.spendable) // per_day)


def reset_human() -> str:
    """'in 6h 12m' — the answer to 'when can I continue?' without timezone maths."""
    seconds = seconds_until_reset()
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"in {hours}h {minutes:02d}m"
    return f"in {minutes}m"
