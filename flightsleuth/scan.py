"""The search itself, shared by the command line and the window."""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from . import config, providers
from .cache import Cache
from .errors import UserError
from .models import Query, SearchResult
from .planner import date_pairs, one_way_legs
from .providers import Capability, Provider

DEFAULT_MAX_SEARCHES = 25
DEFAULT_MAX_AGE_HOURS = 6
WORKERS = 3


def airports(value: str) -> str:
    """Tidy a list of airport codes such as "jfk, ewr" into "JFK,EWR"."""
    codes = value.upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z]{3}(,[A-Z]{3})*", codes):
        raise UserError(f"{value!r} is not an airport list like JFK or JFK,EWR")
    return codes


@dataclass(frozen=True)
class Request:
    """What the user asked for, before it is expanded into individual searches."""

    origin: str
    destination: str
    depart: date
    return_date: date | None = None
    nights: tuple[int, int] | None = None
    flex: int = 0
    split: bool = False
    stops: str = "any"
    cabin: str = "economy"
    adults: int = 1
    currency: str = "USD"
    deep: bool = False


@dataclass(frozen=True)
class Call:
    """One search sent to one price source."""

    provider: str
    query: Query


@dataclass(frozen=True)
class Reply:
    """A source's raw answer to one call."""

    body: dict
    fetched_at: datetime  # when it reached this machine, in UTC
    live: bool  # fetched during this run rather than read back from the cache


@dataclass
class Plan:
    """A request broken into calls, split by whether a saved reply already covers each."""

    trips: list[Query]  # the date pairs being compared
    split: bool  # each trip is also priced as two one-ways
    calls: list[Call]  # every call the request needs, in order
    replies: dict[Call, Reply]  # the calls a saved reply already answers

    @property
    def missing(self) -> list[Call]:
        """The calls that have to be made, and paid for where the source charges."""
        return [call for call in self.calls if call not in self.replies]

    @property
    def saved(self) -> int:
        return len(self.replies)

    @property
    def live(self) -> int:
        return len(self.calls) - len(self.replies)

    @property
    def total(self) -> int:
        return len(self.calls)


@dataclass
class Outcome:
    trips: list[Query]
    results: dict[Query, SearchResult]
    split: bool
    searches_left: int | None  # the fewest credits left at any metered source; unknown if nothing was fetched


def plan(
    request: Request,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    fresh: bool = False,
    today: date | None = None,
) -> Plan:
    """Work out every call the request needs and which of them are already saved. Costs nothing."""
    today = today or date.today()
    if request.depart < today:
        raise UserError("the departure date is in the past")
    if request.return_date and request.return_date < request.depart:
        raise UserError("the return date is before the departure date")

    base = Query(
        origin=request.origin,
        destination=request.destination,
        depart_date=request.depart,
        adults=request.adults,
        cabin=request.cabin,
        stops=request.stops,
        currency=request.currency,
        deep=request.deep,
    )
    pairs = date_pairs(
        request.depart, return_date=request.return_date, nights=request.nights, flex=request.flex, today=today
    )
    trips = [replace(base, depart_date=depart, return_date=back) for depart, back in pairs]
    split = request.split and trips[0].return_date is not None
    wanted = list(trips)
    if split:
        for trip in trips:
            wanted.extend(one_way_legs(trip))
    wanted = list(dict.fromkeys(wanted))  # one-way legs repeat across date pairs

    sources = providers.offering(Capability.LIVE_SEARCH)
    if not sources:
        raise UserError("no price source is available")
    keys = {Call(source.name, query): source.cache_key(query) for query in wanted for source in sources}

    replies = {}
    if not fresh:
        cache = Cache(config.cache_path())
        try:
            for call, key in keys.items():
                saved = cache.lookup(key, max_age_hours * 3600)
                if saved is not None:
                    body, when = saved
                    replies[call] = Reply(body, datetime.fromtimestamp(when, timezone.utc), live=False)
        finally:
            cache.close()
    return Plan(trips, split, list(keys), replies)


class OverCap(UserError):
    """A plan needs more live searches than one run may make."""

    def __init__(self, live: int, limit: int):
        super().__init__(f"this needs {live} live searches, which is over the limit of {limit} per run")
        self.live = live
        self.limit = limit


def check_cap(plan: Plan, max_searches: int = DEFAULT_MAX_SEARCHES) -> None:
    """Refuse a plan that is too big. Front ends call this early so they can word the refusal themselves."""
    if plan.live > max_searches:
        raise OverCap(plan.live, max_searches)


def run(plan: Plan, *, max_searches: int = DEFAULT_MAX_SEARCHES, on_search=None) -> Outcome:
    """Make the plan's missing calls, save the replies, and parse everything.

    Nothing is spent on a plan over `max_searches`, whoever the caller is.
    `on_search(done, live)` is called after each live search lands.
    """
    check_cap(plan, max_searches)
    sources = {name: providers.load(name) for name in dict.fromkeys(call.provider for call in plan.calls)}
    replies = dict(plan.replies)
    missing = plan.missing
    searches_left = None

    if missing:
        needed: dict[str, int] = {}
        for call in missing:
            needed[call.provider] = needed.get(call.provider, 0) + 1
        for name, count in needed.items():
            problem = sources[name].budget_problem(count)
            if problem:
                raise UserError(problem)

        cache = Cache(config.cache_path())

        def store(call: Call, body: dict) -> None:
            now = time.time()
            cache.put(sources[call.provider].cache_key(call.query), body, now=now)
            replies[call] = Reply(body, datetime.fromtimestamp(now, timezone.utc), live=True)
            if on_search:
                on_search(len(replies) - plan.saved, plan.live)

        try:
            error = fetch_all(sources, missing, store)
        finally:
            cache.close()
        if error:
            raise UserError(
                f"stopped after {len(replies) - plan.saved} of {plan.live} live searches: {error}. "
                f"Finished searches are saved, so running this again picks up where it left off."
            )
        left = []
        for name, count in needed.items():
            credits = sources[name].credits_left()
            if credits is not None:
                left.append(credits - count)
        searches_left = min(left, default=None)

    parts: dict[Query, list[SearchResult]] = {}
    for call in plan.calls:
        source, reply = sources[call.provider], replies[call]
        result = source.parse(call.query, reply.body)
        when = source.fetched_at(reply.body) or reply.fetched_at
        parts.setdefault(call.query, []).append(_stamp(result, source.name, when, reply.live))
    results = {query: _merge(found) for query, found in parts.items()}
    return Outcome(plan.trips, results, plan.split, searches_left)


def _stamp(result: SearchResult, source: str, fetched_at: datetime, live: bool) -> SearchResult:
    """Mark every offer with where and when its price came from, so no source can leave that out."""
    offers = tuple(
        replace(offer, source=source, fetched_at=fetched_at, verified_live=live) for offer in result.offers
    )
    return replace(result, offers=offers)


def _merge(parts: list[SearchResult]) -> SearchResult:
    """One result for a search that several sources answered, cheapest offer first."""
    if len(parts) == 1:
        return parts[0]
    offers = sorted((offer for part in parts for offer in part.offers), key=lambda o: (o.price, o.duration_min))
    return SearchResult(
        query=parts[0].query,
        offers=tuple(offers),
        insights=next((part.insights for part in parts if part.insights), None),
        url=next((part.url for part in parts if part.url), None),
    )


def fetch_all(sources: dict[str, Provider], calls: list[Call], on_result) -> UserError | None:
    """Make the live calls a few at a time, handing each reply to `on_result` as it lands.

    The first failure cancels everything not yet started and is returned. Calls
    already in flight still finish and are handed over, so no paid result is thrown away.
    """
    error = None
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(sources[call.provider].search, call.query): call for call in calls}
        for future in as_completed(futures):
            if future.cancelled():
                continue
            try:
                raw = future.result()
            except UserError as e:
                if error is None:
                    error = e
                    for pending in futures:
                        pending.cancel()
                continue
            on_result(futures[future], raw)
    return error
