"""Watches: fares that are checked on a schedule, with an alert when one drops.

A watch is one pair of dates. A check is one search, and only a price worth an alert goes on to
have its return flights and its sellers fetched. Everything a watch has done is in `history.db`;
where it stands is worked out from that each time, so nothing here keeps a state of its own.
"""
from __future__ import annotations

import calendar
import json
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from . import config, links, notify, providers, render, scan
from .errors import UserError
from .history import History
from .models import Check, Insights, Query
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
RESEND_HOURS = 24  # an alert that did not reach ntfy is tried again for this long, then let go as stale
CONFIRM_MINUTES = 5  # a saved reply older than this is never what an alert is confirmed on
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

    median_days = MEDIAN_DAYS

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
    events, checks, alerts = history.rows("watch_events"), history.rows("watch_checks"), history.rows("watch_alerts")
    found = []
    for row in history.rows("watches"):
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


def _months_on(day: date, months: int) -> date:
    """The same day that many months later (earlier if negative), or that month's last day where it is shorter."""
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
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
        return _months_on(allowance.resets_on, -1)
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
    after = _months_on(reset, 1)

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
    return sum(check["searches"] for check in history.rows("watch_checks") if check["day"] >= start.isoformat())


def request(found: Watch) -> scan.Request:
    """The search one check makes. No closer look: that comes only if the price is worth one."""
    wanted = found.trip
    return scan.Request(
        origin=wanted.origin,
        destination=wanted.destination,
        depart=wanted.depart_date,
        return_date=wanted.return_date,
        stops=wanted.stops,
        cabin=wanted.cabin,
        adults=wanted.adults,
        currency=wanted.currency,
        by_price=wanted.by_price,
        check=False,
    )


class OverBudget(UserError):
    """A watch the searches cannot cover. `budget` holds the sums; `fit` is the shortest interval that would do."""

    def __init__(self, budget: Budget, fit: int | None):
        hint = f"Checked every {fit} days it would fit." if fit else "It would not fit at any interval."
        super().__init__(f"the searches cannot cover this watch. {hint}")
        self.budget = budget
        self.fit = fit


@dataclass(frozen=True)
class Added:
    watch: Watch
    budget: Budget | None  # None when the price source is not metered
    kept: bool  # False for a dry run


def add(
    wanted: Query,
    *,
    below: float | None = None,
    drop_percent: float = DEFAULT_DROP,
    typical: bool = True,
    every_days: int = 1,
    now: datetime | None = None,
    dry_run: bool = False,
) -> Added:
    """Keep a new watch, if there is room for one and the searches can cover it. Spends nothing."""
    now = now or datetime.now().astimezone()
    today = now.date()
    wanted = trip(
        wanted.origin,
        wanted.destination,
        wanted.depart_date,
        wanted.return_date,
        adults=wanted.adults,
        cabin=wanted.cabin,
        stops=wanted.stops,
        currency=wanted.currency,
    )
    if wanted.depart_date < today:
        raise UserError("the departure date is in the past")
    if wanted.return_date and wanted.return_date < wanted.depart_date:
        raise UserError("the return date is before the departure date")
    if below is not None and below <= 0:
        raise UserError("the price to alert below must be more than nothing")
    if not 0 <= drop_percent < 100:
        raise UserError("the drop under the median must be a percentage from 0 to 99")
    if not 1 <= every_days <= LONGEST_INTERVAL:
        raise UserError(f"the days between checks must be from 1 to {LONGEST_INTERVAL}")
    new = Watch(0, wanted, below, float(drop_percent), typical, every_days, today)
    if not new.rules:
        raise UserError("a watch needs at least one rule: a price to alert below, a drop under the median, or the typical range")
    with History() as history:
        others = [(standing.watch, standing.due) for standing in standings(history) if not standing.ended]
        if len(others) >= MAX_WATCHES:
            raise UserError(
                f"there are already {MAX_WATCHES} watches, which is the most. "
                f"Remove one first: py -m flightsleuth watch remove N"
            )
        allowance, per_check = account()
        sums = None
        if allowance is not None:
            limits = dict(
                today=today,
                ceiling=config.watch_monthly(),
                used=used_since(history, month_start(allowance, today)),
                per_check=per_check,
            )
            sums = budget([*others, (new, today)], allowance, **limits)
            if not sums.fits:
                raise OverBudget(sums, shortest_fit(new, others, allowance, **limits))
        if dry_run:
            return Added(new, sums, False)
        number = history.add_watch(
            wanted,
            below=below,
            drop_percent=new.drop_percent,
            typical=typical,
            every_days=every_days,
            created_at=now,
            created_day=today,
        )
        return Added(replace(new, id=number), sums, True)


def remove(number: int, *, now: datetime | None = None) -> Watch:
    """Stop checking a watch. Its rows stay, as everything in the history does."""
    with History() as history:
        standing = next((standing for standing in standings(history) if standing.watch.id == number), None)
        if standing is None:
            raise UserError(f"there is no watch {number}. See them with: py -m flightsleuth watch list")
        if standing.ended:
            raise UserError(f"watch {number} was already {standing.ended}")
        history.add_watch_event(number, "removed", "by the owner", now or datetime.now().astimezone())
        return standing.watch


def listed(*, ended: bool = False) -> list[Standing]:
    """The watches being checked, or with `ended` every watch there has been."""
    with History() as history:
        return [standing for standing in standings(history) if ended or not standing.ended]


def setup(*, new: bool = False) -> tuple[str, bool]:
    """The alert topic, made and saved if there was none or another is asked for. Returns it, and whether it is new."""
    topic = None if new else config.ntfy_topic()
    if topic is not None:
        return topic, False
    topic = notify.new_topic()
    config.save_setting(config.TOPIC_VAR, topic)
    return topic, True


def send_test() -> notify.Alert:
    """Send an alert that is plainly a test. No search is made. Tapping it opens the first watch's dates, if there is one."""
    watching = listed()
    first = links.first_site(watching[0].watch.trip) if watching else None
    alert = notify.Alert(*render.TEST_ALERT, click=first.url if first else "")
    notify.send(alert)
    return alert


def log(lines: list[str], now: datetime | None = None) -> None:
    """Add what a run did to the log, each line with the time of the run."""
    stamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    path = config.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.writelines(f"{stamp}  {line}\n" for line in lines)


@dataclass
class Report:
    """What one run did: a line for each thing, for the screen and the log."""

    lines: list[str] = field(default_factory=list)
    spent: int = 0
    failed: bool = False  # a check could not be made, and stays due

    def say(self, standing: Standing, text: str) -> None:
        """Add a line about one watch, named by its route and dates."""
        self.lines.append(f"{render.watch_title(standing.watch.trip)}: {text}")


class _Room:
    """How many searches a run may still make, and what it was that ran out when that is none."""

    def __init__(self, allowance: Allowance | None, ceiling: int, used: int, limit: int | None):
        self._limits: list[list] = []
        if allowance is not None:
            self._limits.append([allowance.left, "the account has no searches left this month"])
            self._limits.append([ceiling - used, f"watches have used the {ceiling} searches a month they may"])
        if limit is not None:
            self._limits.append([limit, f"this run's limit of {limit} is used up"])

    @property
    def left(self) -> int | None:
        """None when nothing limits the run."""
        return max(min(count for count, _ in self._limits), 0) if self._limits else None

    @property
    def why(self) -> str:
        return min(self._limits, key=lambda limit: limit[0])[1]

    def spend(self, searches: int) -> None:
        for limit in self._limits:
            limit[0] -= searches


def _send(alert: notify.Alert) -> str:
    """Send an alert. Returns why it did not go, or nothing if it did."""
    try:
        notify.send(alert)
    except UserError as e:
        return str(e)
    return ""


def run_due(*, now: datetime | None = None, max_searches: int | None = None, dry_run: bool = False) -> Report:
    """Check every watch that is due, once, and alert on what is worth it. This is what the scheduler starts.

    A watch whose check fails stays due, so the next run tries it again; a run missed altogether
    is made up the same way. `now` is the clock, for the tests. A dry run says what is due and
    what it could cost, and writes nothing.
    """
    now = now or datetime.now().astimezone()
    report = Report()
    with History() as history:
        active = _retire(history, now, report, write=not dry_run)
        due = [standing for standing in active if standing.due <= now.date()]
        for standing in active:
            if standing not in due:
                report.say(standing, f"not due until {render.day(standing.due)}.")
        if not active:
            report.lines.append("There are no watches to check.")
        if dry_run:
            for standing in due:
                most = scan.plan(replace(request(standing.watch), check=True, look="confirm"), today=now.date()).total
                report.say(standing, f"due. 1 search, and up to {most - 1} more if the price is worth an alert.")
            return report
        if not due and not any(standing.unsent for standing in active):
            return report
        if config.ntfy_topic() is None:
            raise UserError(f"no alert topic is set up, so nothing was checked. Run: {notify.SETUP}")
        # An alert that never reached ntfy goes first, while its price is still recent. It needs no search.
        for standing in active:
            _resend(history, standing, now, report)
        if due:
            _check_all(history, [standing.watch.id for standing in due], now, max_searches, report)
            _tell_of_outages(history, now, report)
    return report


def _retire(history: History, now: datetime, report: Report, *, write: bool) -> list[Standing]:
    """The watches still being checked, after retiring those whose departure date has passed."""
    active = []
    for standing in standings(history):
        if standing.ended:
            continue
        if standing.watch.trip.depart_date < now.date():
            if write:
                history.add_watch_event(standing.watch.id, "retired", "its departure date has passed", now)
            report.say(standing, "retired. Its departure date has passed.")
            continue
        active.append(standing)
    return active


def _check_all(history: History, numbers: list[int], now: datetime, max_searches: int | None, report: Report) -> None:
    """Check each of the watches named, in turn, inside what the account, the ceiling and the run allow."""
    latest = {standing.watch.id: standing for standing in standings(history)}  # as they stand after any resend
    try:
        allowance, per_check = account()
    except UserError as e:
        # Nothing can be budgeted without the account, so nothing is spent.
        detail = render.checked("failed", "", error=f"the account could not be read: {str(e).rstrip('.')}")
        for number in numbers:
            history.add_watch_check(
                number, checked_at=now, day=now.date(), finished=False, outcome="failed", detail=detail
            )
            report.say(latest[number], detail)
        report.failed = True
        return
    used = used_since(history, month_start(allowance, now.date())) if allowance else 0
    room = _Room(allowance, config.watch_monthly(), used, max_searches)
    fetched: set[scan.Request] = set()
    for number in numbers:
        detail, searches, failed = _Check(history, latest[number], room, now).run(fetched, per_check)
        count = "" if not searches else f" {searches} search{'es' if searches != 1 else ''}."
        report.say(latest[number], f"{detail}{count}")
        report.spent += searches
        report.failed = report.failed or failed


def _resend(history: History, standing: Standing, now: datetime, report: Report) -> None:
    waiting = standing.unsent
    if waiting is None or (now - standing.unsent_since).total_seconds() > RESEND_HOURS * 3600:
        return
    error = _send(notify.Alert(waiting["title"], f"{render.SENT_LATE}\n{waiting['message']}", waiting["click"]))
    history.add_watch_alert(
        standing.watch.id,
        waiting["check_id"],
        price=waiting["price"],
        title=waiting["title"],
        message=waiting["message"],
        click=waiting["click"],
        sent=not error,
        error=error,
        fetched_at=datetime.fromisoformat(waiting["fetched_at"]),
        at=now,
    )
    price = render.money(waiting["price"], standing.watch.trip.currency)
    went = f"still did not go: {error.rstrip('.')}." if error else "has now been sent."
    report.say(standing, f"the alert about {price} that did not go before {went}")


def _tell_of_outages(history: History, now: datetime, report: Report) -> None:
    """Tell the owner, once for each outage, about a watch that has missed OUTAGE_INTERVALS of its intervals."""
    for standing in standings(history):
        if standing.ended or standing.outage_told or now.date() < standing.overdue_since:
            continue
        said = standing.last["detail"] if standing.last else ""
        last_day = date.fromisoformat(standing.done["day"]) if standing.done else None
        error = _send(notify.Alert(*render.outage(standing.watch.trip, last_day, standing.due, said)))
        if error:
            report.say(standing, f"it has gone unchecked, and telling you so failed: {error.rstrip('.')}.")
        else:
            history.add_watch_event(standing.watch.id, "outage", said, now)
            report.say(standing, "you have been told that it has gone unchecked.")


class _Check:
    """One check of one watch: the listing, then the closer look if its price is worth an alert.

    Each step that ends the check calls `close`, which writes the `watch_checks` row with what
    `found` holds by then, and returns what the check came to, what it spent, and whether it failed.
    """

    def __init__(self, history: History, standing: Standing, room: _Room, now: datetime):
        self.history = history
        self.standing = standing
        self.watch = standing.watch
        self.room = room
        self.now = now
        self.today = now.date()
        self.spent_before = history.spent()
        self.found: dict = {}  # the columns of the check's row, filled in as they are learned
        self.number: int | None = None  # the row's id, once it is written

    def paid(self) -> int:
        return round(self.history.spent() - self.spent_before)

    def close(self, outcome: str, *, finished: bool = True, **said) -> tuple[str, int, bool]:
        found = self.found
        detail = render.checked(
            outcome, self.watch.trip.currency, price=found.get("price"), confirmed=found.get("confirmed_price"), **said
        )
        searches = self.paid()
        self.room.spend(searches)
        self.number = self.history.add_watch_check(
            self.watch.id,
            checked_at=self.now,
            day=self.today,
            finished=finished,
            outcome=outcome,
            detail=detail,
            searches=searches,
            **found,
        )
        return detail, searches, outcome == "failed"

    def run(self, fetched: set[scan.Request], per_check: int) -> tuple[str, int, bool]:
        """The listing: one search, unless another watch on the same dates has just paid for it."""
        asked = request(self.watch)
        shared = asked in fetched
        if not shared and self.room.left is not None and self.room.left < per_check:
            return self.close("skipped", finished=False, error=self.room.why)
        try:
            plan = scan.plan(asked, fresh=not shared, today=self.today)
            outcome = scan.run(plan, max_searches=None if shared else self.room.left)
        except UserError as e:
            return self.close("failed", finished=False, error=str(e).rstrip("."))
        fetched.add(asked)
        result = outcome.results.get(plan.trips[0])
        if result is None or result.cheapest is None:
            return self.close("no fares")
        first = scan.first_to_try(result, self.standing.empties)
        if first is None:
            self.found.update(price=result.cheapest.price, fetched_at=result.cheapest.fetched_at)
            return self.close("known empty")
        insights = result.insights
        self.found.update(
            price=first.price,
            itinerary_key=first.outbound_key,
            median=median(insights, self.today),
            typical_range=insights.typical_range if insights else None,
            fetched_at=first.fetched_at,
        )
        reasons, stop = self._judge(first.price, insights, quiet="quiet")
        if stop:
            return stop
        return self._confirm(asked, insights, reasons)

    def _judge(
        self, price: float, insights: Insights | None, *, quiet: str
    ) -> tuple[list[str], tuple[str, int, bool] | None]:
        """The reasons a price would be alerted, and the closed check if it will not be.

        It will not be when no rule fires, which closes the check as `quiet`, or when the price is
        one already alerted, which closes it as a repeat.
        """
        rules = fired(self.watch, price, insights, self.today)
        self.found["reasons"] = tuple(rule.rule for rule in rules)
        reasons = [render.why(rule, price, self.watch.trip.currency) for rule in rules]
        if not rules:
            return reasons, self.close(quiet)
        lowest = self.standing.lowest_alerted
        if not is_news(price, lowest):
            return reasons, self.close("repeat", lowest=lowest, bar=news_below(lowest))
        return reasons, None

    def _confirm(self, asked: scan.Request, insights: Insights | None, reasons: list[str]) -> tuple[str, int, bool]:
        """Worth an alert, so now its return flights and its sellers: up to HOLD more searches."""
        spare = None if self.room.left is None else self.room.left - self.paid()
        if spare is not None and spare <= 0:
            return self.close("not confirmed", reasons=reasons, error=self.room.why)
        try:
            # The listing fetched a moment ago is reused. A follow-up saved any longer ago is not: an
            # alert is only ever sent on return flights fetched for this check.
            closer = scan.plan(
                replace(asked, check=True, look="confirm", known_empty=self.standing.empties),
                today=self.today,
                reserve=spare,
                max_age_hours=CONFIRM_MINUTES / 60,
            )
            outcome = scan.run(closer)
        except UserError as e:
            return self.close("failed", finished=False, error=str(e).rstrip("."))
        check = outcome.checks[0] if outcome.checks else None
        if check is not None:
            self.found["no_returns"] = tuple((offer.outbound_key, offer.price) for offer in check.failed)
        confirmed = check.confirmed if check else None
        if confirmed is None:
            if any(pair not in self.standing.empties for pair in self.found.get("no_returns", ())):
                return self.close("no return flights", reasons=reasons)
            return self.close("not confirmed", reasons=reasons, error="there was no way left to fetch its return flights")
        self.found.update(
            confirmed_price=confirmed.price, itinerary_key=confirmed.itinerary_key, fetched_at=confirmed.fetched_at
        )
        # Still the listing's own rating. The sellers' reply carries a typical range too, but in both
        # recorded pairs it was a different one ($540 to $1,850 against $480 to $830), so it is not
        # borrowed for a listing that came with none.
        reasons, stop = self._judge(confirmed.price, insights, quiet="rose")
        if stop:
            return stop
        if not confirmed.verified_live:
            # A one-way's fare is its listing, which the closer look read back from the cache. This
            # check fetched it a moment ago, so it is not a saved price and must not be called one.
            check = replace(check, confirmed=replace(confirmed, verified_live=True))
        return self._alert(check, reasons)

    def _alert(self, check: Check, reasons: list[str]) -> tuple[str, int, bool]:
        title, message, click = render.alert(check, reasons)
        error = _send(notify.Alert(title, message, click))
        said = self.close("alert not sent" if error else "alerted", reasons=reasons, error=error.rstrip("."))
        self.history.add_watch_alert(
            self.watch.id,
            self.number,
            price=check.confirmed.price,
            title=title,
            message=message,
            click=click,
            sent=not error,
            error=error,
            fetched_at=check.confirmed.fetched_at,
            at=self.now,
        )
        return said
