"""The search itself, shared by the command line and the window."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date

from . import config
from .cache import Cache
from .errors import UserError
from .models import Query, SearchResult
from .planner import date_pairs, one_way_legs
from .serpapi import SerpApi, SerpApiError, cache_key, parse

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


@dataclass
class Plan:
    """A request broken into searches, split by whether a saved reply already covers each."""

    trips: list[Query]  # the date pairs being compared
    split: bool  # each trip is also priced as two one-ways
    raw: dict[Query, dict]  # saved replies
    missing: list[Query]  # searches that have to be paid for

    @property
    def saved(self) -> int:
        return len(self.raw)

    @property
    def live(self) -> int:
        return len(self.missing)

    @property
    def total(self) -> int:
        return self.saved + self.live


@dataclass
class Outcome:
    trips: list[Query]
    results: dict[Query, SearchResult]
    split: bool
    searches_left: int | None  # unknown when nothing live was needed


def plan(
    request: Request,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    fresh: bool = False,
    today: date | None = None,
) -> Plan:
    """Work out every search the request needs and which of them are already saved. Costs nothing."""
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

    raw = {}
    if not fresh:
        cache = Cache(config.cache_path())
        try:
            for query in wanted:
                saved = cache.get(cache_key(query), max_age_hours * 3600)
                if saved is not None:
                    raw[query] = saved
        finally:
            cache.close()
    return Plan(trips, split, raw, [query for query in wanted if query not in raw])


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
    """Pay for the plan's missing searches, save them, and parse everything.

    Nothing is spent on a plan over `max_searches`, whoever the caller is.
    `on_search(done, live)` is called after each live search lands.
    """
    check_cap(plan, max_searches)
    raw = dict(plan.raw)
    searches_left = None
    if plan.missing:
        client = SerpApi(config.load_api_key())
        account = client.account()
        problem = budget_problem(plan.live, account)
        if problem:
            raise UserError(problem)

        cache = Cache(config.cache_path())

        def store(query: Query, body: dict) -> None:
            cache.put(cache_key(query), body)
            raw[query] = body
            if on_search:
                on_search(len(raw) - plan.saved, plan.live)

        try:
            error = fetch_all(client, plan.missing, store)
        finally:
            cache.close()
        if error:
            raise UserError(
                f"stopped after {len(raw) - plan.saved} of {plan.live} live searches: {error}. "
                f"Finished searches are saved, so running this again picks up where it left off."
            )
        if account.get("total_searches_left") is not None:
            searches_left = account["total_searches_left"] - plan.live

    results = {query: parse(query, body) for query, body in raw.items()}
    return Outcome(plan.trips, results, plan.split, searches_left)


def budget_problem(needed: int, account: dict) -> str | None:
    """Why the account cannot afford `needed` live searches right now, if it can't."""
    month_left = account.get("total_searches_left")
    if month_left is not None and needed > month_left:
        return f"this scan needs {needed} live searches but only {month_left} are left this month"
    hourly_cap = account.get("account_rate_limit_per_hour")
    if hourly_cap is not None:
        hour_left = hourly_cap - account.get("this_hour_searches", 0)
        if needed > hour_left:
            return f"this scan needs {needed} live searches but only {hour_left} more are allowed this hour"
    return None


def fetch_all(client: SerpApi, queries: list[Query], on_result) -> SerpApiError | None:
    """Run the live searches a few at a time, handing each reply to `on_result` as it lands.

    The first failure cancels everything not yet started and is returned. Searches
    already in flight still finish and are handed over, so no paid result is thrown away.
    """
    error = None
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(client.search, query): query for query in queries}
        for future in as_completed(futures):
            if future.cancelled():
                continue
            try:
                raw = future.result()
            except SerpApiError as e:
                if error is None:
                    error = e
                    for pending in futures:
                        pending.cancel()
                continue
            on_result(futures[future], raw)
    return error
