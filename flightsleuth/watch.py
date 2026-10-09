"""Watches: fares that are checked on a schedule, with an alert when one drops.

A watch is one pair of dates. A check is one search, and only a price worth an alert goes on to
have its return flights and its sellers fetched. Everything a watch has done is in `history.db`;
where it stands is worked out from that each time, so nothing here keeps a state of its own.
"""
from __future__ import annotations

import calendar
import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import config, providers
from .errors import UserError
from .history import History
from .models import Insights, Query
from .providers import Allowance, Capability

MAX_WATCHES = 3
# Searches kept back for each watch, so that one alert can be confirmed: two return lookups and the sellers.
HOLD = 3
MEDIAN_DAYS = 30  # the "recent median" is of Google's daily lows over this many days
MEDIAN_POINTS = 10  # fewer daily lows than this is too little to call a median
DEFAULT_DROP = 15.0  # percent under that median
# A price is only news again once it is this far under the lowest price already alerted: this
# much in the watch's own currency, and this many percent.
FURTHER_DROP = 5
FURTHER_PERCENT = 2
OUTAGE_INTERVALS = 2  # the owner is told once a watch has gone this many of its intervals unchecked
LONGEST_INTERVAL = 60  # days


@dataclass(frozen=True)
class Watch:
    id: int  # how the owner names it; 0 for one not yet kept
    trip: Query
    below: float | None  # alert under this price
    drop_percent: float  # alert this far under the recent median; 0 turns the rule off
    typical: bool  # alert under Google's typical range
    every_days: int
    created_day: date

    @property
    def rules(self) -> int:
        return (self.below is not None) + bool(self.drop_percent) + self.typical


def trip(
    origin: str,
    destination: str,
    depart: date,
    return_date: date | None = None,
    *,
    adults: int = 1,
    cabin: str = "economy",
    stops: str = "any",
    currency: str = "USD",
) -> Query:
    """The search a watch repeats: Google's own order for a round trip, cheapest first for a one-way.

    Sorted by price, a round trip's reply has no price rating and lists fares that may not exist;
    in Google's own order it has the rating, the typical range and two months of daily lows. A
    one-way's sorted reply already carries all three. `tests/fixtures/README.md` has the replies.
    """
    return Query(
        origin=origin,
        destination=destination,
        depart_date=depart,
        return_date=return_date,
        adults=adults,
        cabin=cabin,
        stops=stops,
        currency=currency,
        by_price=return_date is None,
    )


@dataclass(frozen=True)
class Fired:
    """One rule a price satisfied, with the number it fell under."""

    rule: str  # "below", "median" or "typical"
    mark: float  # the price that was set, the median, or the bottom of the typical range
    top: float | None = None  # for "typical": the top of the range
    days: int | None = None  # for "median": how many days it is the median of


def median(insights: Insights | None, today: date) -> float | None:
    """The median of Google's daily lows over the last MEDIAN_DAYS days. None if it gave too few to say."""
    if insights is None:
        return None
    first = today - timedelta(days=MEDIAN_DAYS)
    lows = [price for when, price in insights.price_history if first < when.astimezone(timezone.utc).date() <= today]
    return statistics.median(lows) if len(lows) >= MEDIAN_POINTS else None


def fired(watch: Watch, price: float, insights: Insights | None, today: date) -> list[Fired]:
    """The rules of a watch that a price satisfies, in the order the owner gave them."""
    found = []
    if watch.below is not None and price < watch.below:
        found.append(Fired("below", watch.below))
    middle = median(insights, today)
    if watch.drop_percent and middle and price <= middle * (1 - watch.drop_percent / 100):
        found.append(Fired("median", middle, days=MEDIAN_DAYS))
    if watch.typical and insights and insights.typical_range and price < insights.typical_range[0]:
        found.append(Fired("typical", insights.typical_range[0], top=insights.typical_range[1]))
    return found


def news_below(lowest: float) -> float:
    """The most a price may be and still be worth a second alert, given the lowest already alerted."""
    return lowest - max(FURTHER_DROP, lowest * FURTHER_PERCENT / 100)


def is_news(price: float, lowest: float | None) -> bool:
    """Whether a price is worth an alert: none has been sent yet, or this is a further drop."""
    return lowest is None or price <= news_below(lowest)


@dataclass(frozen=True)
class Standing:
    """Where one watch stands, worked out from what `history.db` holds about it."""

    watch: Watch
    ended: str = ""  # "" while it is being checked; else "removed" or "retired"
    last: dict | None = None  # its latest check, finished or not
    done: dict | None = None  # its latest check that finished
    priced: dict | None = None  # its latest check that found a fare
    lowest_alerted: float | None = None  # the lowest price an alert that reached ntfy was about
    searches: int = 0  # everything its checks have spent
    empties: tuple[tuple[str, float], ...] = ()  # fares found to have no return flights: (outbound flights, price)
    outage_told: bool = False  # the owner has been told that it has gone unchecked since `done`
    unsent: dict | None = None  # the latest alert, if no try at sending it has reached ntfy
    unsent_since: datetime | None = None  # when that alert was first tried

    @property
    def due(self) -> date:
        """The day its next check falls due: its interval after the last that finished, or the day it was made."""
        if self.done is None:
            return self.watch.created_day
        return date.fromisoformat(self.done["day"]) + timedelta(days=self.watch.every_days)

    @property
    def overdue_since(self) -> date:
        """The day from which it counts as having missed OUTAGE_INTERVALS intervals."""
        last = date.fromisoformat(self.done["day"]) if self.done else self.watch.created_day
        return last + timedelta(days=OUTAGE_INTERVALS * self.watch.every_days)


def _watch(row: dict) -> Watch:
    return Watch(
        id=row["id"],
        trip=trip(
            row["origin"],
            row["destination"],
            date.fromisoformat(row["depart_date"]),
            date.fromisoformat(row["return_date"]) if row["return_date"] else None,
            adults=row["adults"],
            cabin=row["cabin"],
            stops=row["stops"],
            currency=row["currency"],
        ),
        below=row["below"],
        drop_percent=row["drop_percent"],
        typical=bool(row["typical"]),
        every_days=row["every_days"],
        created_day=date.fromisoformat(row["created_day"]),
    )


def standings(history: History) -> list[Standing]:
    """Every watch ever made, in the order they were made, with where each stands."""
    events, checks, alerts = history.watch_events(), history.watch_checks(), history.watch_alerts()
    found = []
    for row in history.watches():
        mine = [check for check in checks if check["watch_id"] == row["id"]]
        told = [alert for alert in alerts if alert["watch_id"] == row["id"]]
        happened = [event for event in events if event["watch_id"] == row["id"]]
        done = next((check for check in reversed(mine) if check["finished"]), None)
        since = done["checked_at"] if done else row["created_at"]
        # Only the newest alert can still be waiting to go: an older one is about an older price.
        newest = [alert for alert in told if alert["check_id"] == told[-1]["check_id"]] if told else []
        unsent = newest[-1] if newest and not any(alert["sent"] for alert in newest) else None
        found.append(
            Standing(
                watch=_watch(row),
                ended=next((event["event"] for event in happened if event["event"] in ("removed", "retired")), ""),
                last=mine[-1] if mine else None,
                done=done,
                priced=next((check for check in reversed(mine) if check["price"] is not None), None),
                lowest_alerted=min((alert["price"] for alert in told if alert["sent"]), default=None),
                searches=sum(check["searches"] for check in mine),
                empties=tuple(dict.fromkeys((key, price) for check in mine for key, price in json.loads(check["no_returns"]))),
                outage_told=any(event["event"] == "outage" and event["at"] >= since for event in happened),
                unsent=unsent,
                unsent_since=datetime.fromisoformat(newest[0]["at"]) if unsent else None,
            )
        )
    return found


def _month_on(day: date) -> date:
    """The same day a month later, or that month's last day where it is shorter."""
    year, month = divmod(day.year * 12 + day.month, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _month_back(day: date) -> date:
    year, month = divmod(day.year * 12 + day.month - 2, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def checks_between(watch: Watch, due: date, start: date, end: date) -> int:
    """How many checks a watch makes on the days from `start` up to, but not including, `end`.

    It is checked on `due` and every `every_days` after it, never before `start`, and for the last
    time on the day it departs.
    """
    day = max(due, start)
    if day > due:  # carry on from the cadence it is already on
        day = due + timedelta(days=-(-(start - due).days // watch.every_days) * watch.every_days)
    last = min(end - timedelta(days=1), watch.trip.depart_date)
    return (last - day).days // watch.every_days + 1 if day <= last else 0


@dataclass(frozen=True)
class Span:
    """One stretch of days: what the watches would use in it, set against what there is."""

    start: date
    end: date  # the first day after it
    checks: tuple[tuple[Watch, int], ...]  # how many checks each watch makes in it
    per_check: int
    available: int
    hold: int = HOLD  # searches kept back for each watch that has a check in it

    @property
    def count(self) -> int:
        return sum(count for _, count in self.checks)

    @property
    def watching(self) -> int:
        """The watches with a check in it. One with none holds nothing back."""
        return sum(1 for _, count in self.checks if count)

    @property
    def held(self) -> int:
        return self.hold * self.watching

    @property
    def needed(self) -> int:
        return self.count * self.per_check + self.held

    @property
    def spare(self) -> int:
        return self.available - self.needed


@dataclass(frozen=True)
class Budget:
    """Whether the watches fit the searches there are: until the reset, and in the month after it."""

    now: Span
    after: Span
    left: int  # searches the account has left before the reset
    per_month: int | None  # what the plan gives each month, if the account says
    ceiling: int  # the most the watches may use in one of the plan's months
    used: int  # what they have used in this one so far
    reset_known: bool  # False when the account named no reset day and 30 days from today stands in

    @property
    def fits(self) -> bool:
        return self.now.spare >= 0 and self.after.spare >= 0


def month_start(allowance: Allowance, today: date) -> date:
    """The first day of the plan's month that today is in. Thirty days back if the account names no reset."""
    if allowance.resets_on and allowance.resets_on > today:
        return _month_back(allowance.resets_on)
    return today - timedelta(days=30)


def budget(
    watches: list[tuple[Watch, date]],
    allowance: Allowance,
    *,
    today: date,
    ceiling: int,
    used: int,
    per_check: int = 1,
) -> Budget:
    """Set the watches, each with the day it is next due, against what the account says is left.

    Nothing about the plan is assumed. What is left, the monthly total and the reset day all come
    from `allowance`, which is read from the account each time.
    """
    known = allowance.resets_on is not None and allowance.resets_on > today
    reset = allowance.resets_on if known else today + timedelta(days=30)
    after = _month_on(reset)

    def span(start: date, end: date, available: int) -> Span:
        # A watch already overdue is checked today, and its interval counts from then.
        counts = tuple((watch, checks_between(watch, max(due, today), start, end)) for watch, due in watches)
        return Span(start, end, counts, per_check, max(available, 0))

    monthly = ceiling if allowance.per_month is None else min(ceiling, allowance.per_month)
    return Budget(
        now=span(today, reset, min(allowance.left, ceiling - used)),
        after=span(reset, after, monthly),
        left=allowance.left,
        per_month=allowance.per_month,
        ceiling=ceiling,
        used=used,
        reset_known=known,
    )


def shortest_fit(
    watch: Watch, others: list[tuple[Watch, date]], allowance: Allowance, *, today: date, ceiling: int, used: int, per_check: int = 1
) -> int | None:
    """The fewest days between checks at which a new watch would fit beside the others. None if no interval does."""
    for days in range(1, LONGEST_INTERVAL + 1):
        slower = Watch(watch.id, watch.trip, watch.below, watch.drop_percent, watch.typical, days, watch.created_day)
        if budget([*others, (slower, today)], allowance, today=today, ceiling=ceiling, used=used, per_check=per_check).fits:
            return days
    return None


def account() -> tuple[Allowance | None, int]:
    """What the metered price source says is left, and how many searches one check costs. Asking is free."""
    sources = providers.offering(Capability.LIVE_SEARCH)
    if not sources:
        raise UserError("no price source is available")
    metered = [allowance for source in sources if (allowance := source.allowance()) is not None]
    return min(metered, key=lambda allowance: allowance.left, default=None), max(len(metered), 1)


def used_since(history: History, start: date) -> int:
    """The searches every watch, ended or not, has spent on or after one day."""
    return sum(check["searches"] for check in history.watch_checks() if check["day"] >= start.isoformat())
