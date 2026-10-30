"""The search itself, shared by the command line and the window."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from . import planner, providers
from .cache import Cache
from .errors import UserError
from .history import History
from .models import Alternative, Check, Destination, Explore, Offer, Query, SearchResult, itinerary_key
from .planner import date_pairs, one_way_legs
from .providers import Capability, Provider

DEFAULT_MAX_SEARCHES = 25
THOROUGH_MAX_SEARCHES = 40
DEFAULT_MAX_AGE_HOURS = 6
WORKERS = 3
STRATEGIES = ("auto", "grid", "sampled", "one-way", "explore")
# "full" confirms the best fare and sets other ways to fly beside it. "confirm" stops after the
# return flights and the sellers, which is all a price watch needs before it alerts.
LOOKS = ("full", "confirm")
# A listing's tokens are only followed while it is this fresh. SerpApi serves a repeat of the
# same search from its own cache for an hour; nothing recorded says a token lasts longer.
TOKEN_MINUTES = 60
# How many listed fares to ask return flights for before giving up: the first and one spare.
RETURN_TRIES = 2
# Of the searches a sampled window has left after the closer look, the share kept back for refining.
REFINE_SHARE = (3, 8)
_FOLLOW_UPS = frozenset({Capability.RETURN_FLIGHTS, Capability.BOOKING_OPTIONS})


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
    window: tuple[date, date] | None = None  # depart on any day from the first to the second; replaces `flex`
    strategy: str = "auto"  # how a window is covered; one of STRATEGIES
    thorough: bool = False  # a bigger budget: more dates priced, and the two best fares looked at closely
    check: bool | None = None  # look closer at the best fare; None means only for a window
    market: str = ""  # the country whose prices to ask for; "" leaves it to the source
    other_market: str = ""  # the second country to price the best fare in; "" means the destination's
    bags: int = 0  # carry-on bags every price must include
    risky: bool = False  # also list hidden-city and self-transfer fares, labelled
    by_price: bool = True  # cheapest first; False asks for the source's own order
    look: str = "full"  # how far the closer look goes; one of LOOKS
    # Fares already found to have no return flights, as (outbound flights, price). A closer look
    # leaves them out without asking again, for as long as they are listed at that price.
    known_empty: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class Call:
    """One search sent to one price source."""

    provider: str
    query: Query | Explore


@dataclass(frozen=True)
class Reply:
    """A source's raw answer to one call."""

    body: dict
    fetched_at: datetime  # when it reached this machine, in UTC
    live: bool  # fetched during this run rather than read back from the cache


@dataclass
class Plan:
    """A request broken into calls, split by whether a saved reply already covers each.

    A window too wide to price in full is covered in stages, and the later stages depend on
    what the first replies say. Their calls cannot be listed yet, so they are counted instead:
    `reserved` is the most each source may still be sent, and `live` includes it.
    """

    trips: list[Query]  # the date pairs being compared, as far as they are known before any reply
    split: bool  # each trip is also priced as two one-ways
    calls: list[Call]  # every call known in advance, in order
    replies: dict[Call, Reply]  # the calls a saved reply already answers
    request: Request | None = None  # None for an explore search, which has no dates to work through
    strategy: str = "grid"
    window: tuple[date, date] | None = None  # the request's window, without any days already past
    cap: int = DEFAULT_MAX_SEARCHES  # the most live calls the plan was sized for
    searchers: tuple[str, ...] = ()  # the sources every dated search goes to
    refine: int = 0  # dated searches kept back for the dates the first replies point to
    checks: int = 0  # date pairs whose best fare gets a closer look
    reserved: dict[str, int] = field(default_factory=dict)  # by source: live calls beyond `calls`
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS
    fresh: bool = False

    @property
    def missing(self) -> list[Call]:
        """The calls that have to be made, and paid for where the source charges."""
        return [call for call in self.calls if call not in self.replies]

    @property
    def saved(self) -> int:
        return len(self.replies)

    @property
    def later(self) -> int:
        """The most live calls the later stages may make."""
        return sum(self.reserved.values())

    @property
    def refine_calls(self) -> int:
        return self.refine * len(self.searchers)

    @property
    def check_calls(self) -> int:
        return self.later - self.refine_calls

    @property
    def live(self) -> int:
        """The most live calls a run of this plan can make. Exact when nothing is reserved."""
        return len(self.calls) - len(self.replies) + self.later

    @property
    def total(self) -> int:
        return len(self.calls) + self.later


@dataclass
class Outcome:
    trips: list[Query]
    results: dict[Query, SearchResult]
    split: bool
    searches_left: int | None  # the fewest credits left at any metered source; unknown if nothing was fetched
    checks: list[Check] = field(default_factory=list)  # the closer looks, best date pair first
    notes: list[str] = field(default_factory=list)  # what the run decided along the way
    destinations: tuple[Destination, ...] = ()  # filled by an explore search, cheapest first
    live: int = 0  # live calls actually made
    saved: int = 0  # calls answered from the cache
    strategy: str = "grid"


def plan(
    request: Request,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    fresh: bool = False,
    today: date | None = None,
    max_searches: int | None = None,
    reserve: int | None = None,
) -> Plan:
    """Work out every call the request needs and which of them are already saved. Costs nothing.

    `reserve` lowers the most any one source may be sent in the later stages, for a caller with
    fewer searches to spare than those stages can use. They then do as much as that allows.
    """
    today = today or date.today()
    if request.strategy not in STRATEGIES:
        raise UserError(f"{request.strategy!r} is not a strategy; choose one of {', '.join(STRATEGIES)}")
    if request.look not in LOOKS:
        raise UserError(f"{request.look!r} is not a kind of closer look; choose one of {', '.join(LOOKS)}")
    window = _window(request, today)
    cap = max_searches if max_searches is not None else cap_for(request)
    base = _base(request)
    trips = [replace(base, depart_date=depart, return_date=back) for depart, back in _pairs(request, window, today)]
    round_trip = trips[0].return_date is not None
    split = request.split and round_trip
    grid = list(trips)
    if split:
        for trip in trips:
            grid.extend(one_way_legs(trip))
    grid = list(dict.fromkeys(grid))  # one-way legs repeat across date pairs

    sources = providers.offering(Capability.LIVE_SEARCH)
    if not sources:
        raise UserError("no price source is available")
    check = request.check if request.check is not None else window is not None
    checks = (2 if request.thorough else 1) if check else 0

    def closer_look(split_done: bool) -> dict[str, int]:
        return {source.name: _check_cost(source, round_trip, split_done, request.look) for source in sources}

    strategy = request.strategy
    if strategy == "auto":
        cost = len(grid) * len(sources) + checks * sum(closer_look(split).values())
        strategy = "grid" if window is None or cost <= cap else "sampled"

    refine = 0
    askers = sources  # who the calls known in advance go to
    wanted: list[Query | Explore]
    if strategy == "grid":
        wanted, costs = grid, closer_look(split)
    elif strategy == "sampled":
        costs, split = closer_look(False), False
        slots = (cap - checks * sum(costs.values())) // len(sources)
        if slots < 2:
            raise UserError(
                f"a limit of {cap} live searches leaves too few to sample these dates. Raise the limit or narrow the window."
            )
        refine = slots * REFINE_SHARE[0] // REFINE_SHARE[1]
        sampled = planner.sample(*window, request.nights, slots - refine)
        wanted = trips = [replace(base, depart_date=depart, return_date=back) for depart, back in sampled]
    elif strategy == "one-way":
        if not round_trip:
            raise UserError("the one-way strategy prices a round trip as two one-way tickets, so it needs a trip length")
        costs, split = closer_look(True), True
        wanted = list(dict.fromkeys(leg for trip in trips for leg in one_way_legs(trip)))
        refine = min(len(trips), 6 if request.thorough else 3)
        trips = []  # the pairs worth a round-trip search are chosen once the one-way prices are in
    else:
        askers = providers.offering(Capability.EXPLORE)
        if not askers:
            raise UserError("no price source can suggest dates")
        costs, split = closer_look(False), False
        wanted = _explore_months(request, window, today)
        refine = (cap - len(wanted) * len(askers) - checks * sum(costs.values())) // len(sources)
        if refine < 1:
            raise UserError(f"a limit of {cap} live searches leaves none to price the dates explore suggests")
        trips = []

    keys = {Call(source.name, query): source.cache_key(query) for query in wanted for source in askers}
    replies = _saved(keys, max_age_hours, fresh)
    reserved = {}
    for source in sources:
        later = refine + _follow_ups(source, checks * costs[source.name], replies)
        later = later if reserve is None else min(later, reserve)
        if later:
            reserved[source.name] = later
    return Plan(
        trips,
        split,
        list(keys),
        replies,
        request=request,
        strategy=strategy,
        window=window,
        cap=cap,
        searchers=tuple(source.name for source in sources),
        refine=refine,
        checks=checks,
        reserved=reserved,
        max_age_hours=max_age_hours,
        fresh=fresh,
    )


def _pairs(request: Request, window: tuple[date, date] | None, today: date) -> list[planner.Pair]:
    """The date pairs a request asks about: its window's, or its fixed dates slid by `flex`."""
    if window is not None:
        return planner.window_pairs(*window, request.nights)
    if request.depart < today:
        raise UserError("the departure date is in the past")
    if request.return_date and request.return_date < request.depart:
        raise UserError("the return date is before the departure date")
    if request.strategy not in ("auto", "grid"):
        raise UserError(f"the {request.strategy} strategy needs a date window")
    return date_pairs(
        request.depart, return_date=request.return_date, nights=request.nights, flex=request.flex, today=today
    )


def _explore_months(request: Request, window: tuple[date, date], today: date) -> list[Explore]:
    """One explore call for each month the window touches, for the explore strategy to seed the dates from."""
    duration = planner.explore_duration(request.nights)
    months = planner.window_months(*window, today)
    if duration is None:
        raise UserError("the explore strategy suits trips of a weekend, about a week or about two weeks")
    if months is None:
        raise UserError(f"the explore strategy only reaches {planner.EXPLORE_MONTHS} months ahead")
    return [
        Explore(
            origin=request.origin,
            destination=request.destination.split(",")[0],  # the explore engine takes one place
            month=month,
            duration=duration,
            adults=request.adults,
            cabin=request.cabin,
            stops=request.stops,
            currency=request.currency,
            market=request.market,
            bags=request.bags,
        )
        for month in months
    ]


def _follow_ups(source: Provider, follow: int, replies: dict[Call, Reply]) -> int:
    """The most closer-look calls a source may be sent, given the saved replies a plan starts from.

    A saved listing may be too old for its tokens, and looking closer then starts by fetching it
    again. That call is counted for any source holding such a reply. It can never push a plan
    over a cap it was sized to, because the saved reply it replaces is one call the plan no
    longer needs; and the refining is left as it was, so an old run replays to the same fare.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=TOKEN_MINUTES)
    old = any(call.provider == source.name and reply.fetched_at < cutoff for call, reply in replies.items())
    return follow + 1 if follow and old and source.capabilities & _FOLLOW_UPS else follow


def plan_explore(trip: Explore, *, max_age_hours: float = DEFAULT_MAX_AGE_HOURS, fresh: bool = False) -> Plan:
    """The calls a flexible-destination search needs: one for each source that can explore. Costs nothing."""
    if not 0 <= trip.month <= 12:
        raise UserError("the month must be a number from 1 to 12, or 0 for any time in the next six months")
    sources = providers.offering(Capability.EXPLORE)
    if not sources:
        raise UserError("no price source can suggest destinations")
    keys = {Call(source.name, trip): source.cache_key(trip) for source in sources}
    return Plan([], False, list(keys), _saved(keys, max_age_hours, fresh), max_age_hours=max_age_hours, fresh=fresh)


def cap_for(request: Request) -> int:
    """The per-run limit when the caller names none."""
    return THOROUGH_MAX_SEARCHES if request.thorough else DEFAULT_MAX_SEARCHES


def _window(request: Request, today: date) -> tuple[date, date] | None:
    if request.window is None:
        return None
    earliest, latest = request.window
    if latest < earliest:
        raise UserError("the date window ends before it starts")
    if latest < today:
        raise UserError("the date window is in the past")
    if request.return_date is not None:
        raise UserError("a date window needs a trip length in nights rather than a return date")
    if request.flex:
        raise UserError("a date window already covers the days around it, so flexible days do not apply")
    return max(earliest, today), latest


def _base(request: Request) -> Query:
    return Query(
        origin=request.origin,
        destination=request.destination,
        depart_date=request.depart,
        adults=request.adults,
        cabin=request.cabin,
        stops=request.stops,
        currency=request.currency,
        deep=request.deep,
        market=request.market,
        bags=request.bags,
        by_price=request.by_price,
    )


def _check_cost(source: Provider, round_trip: bool, split_done: bool, look: str = "full") -> int:
    """The most calls one closer look can send to one source."""
    cost = 0
    if round_trip and Capability.RETURN_FLIGHTS in source.capabilities:
        cost += RETURN_TRIES
    if Capability.BOOKING_OPTIONS in source.capabilities:
        cost += 1
    if look == "confirm":
        return cost
    if Capability.MARKETS in source.capabilities:
        cost += 1
    if round_trip and not split_done:
        cost += 2  # the two one-ways
    return cost


def _saved(keys: dict[Call, str], max_age_hours: float, fresh: bool) -> dict[Call, Reply]:
    replies = {}
    if not fresh:
        with Cache() as cache:
            for call, key in keys.items():
                saved = cache.lookup(key, max_age_hours * 3600)
                if saved is not None:
                    body, when = saved
                    replies[call] = Reply(body, datetime.fromtimestamp(when, timezone.utc), live=False)
    return replies


class OverCap(UserError):
    """A plan needs more live searches than one run may make."""

    def __init__(self, live: int, limit: int):
        super().__init__(f"this needs {live} live searches, which is over the limit of {limit} per run")
        self.live = live
        self.limit = limit


def check_cap(plan: Plan, max_searches: int | None = None) -> None:
    """Refuse a plan that is too big. Front ends call this early so they can word the refusal themselves."""
    limit = plan.cap if max_searches is None else max_searches
    if plan.live > limit:
        raise OverCap(plan.live, limit)


def run(plan: Plan, *, max_searches: int | None = None, on_search=None) -> Outcome:
    """Make the plan's missing calls, save the replies, and parse everything.

    Nothing is spent on a plan over `max_searches`, whoever the caller is, and no stage sends a
    source more than the plan counted for it. `on_search(done, live)` is called after each live
    search lands; `live` is the plan's ceiling.
    """
    check_cap(plan, max_searches)
    session = _Session(plan, on_search)
    session.check_budgets()
    notes: list[str] = []
    checks: list[Check] = []
    with session:
        session.fetch(plan.missing, lookup=False)
        trips = list(plan.trips)
        if plan.strategy == "explore":
            trips = _seed(session, notes)
        elif plan.strategy == "one-way":
            trips = _pair_up(session)
        if plan.strategy in ("sampled", "explore"):
            trips = _refine(session, trips)
        for trip in _ranked(session, trips)[: plan.checks]:
            checks.append(_check(session, trip))
        results = session.results()
        places = session.places()
        left = [
            credits - sent
            for name, sent in session.sent.items()
            if (credits := session.sources[name].credits_left()) is not None
        ]
    return Outcome(
        [trip for trip in trips if trip in results],
        results,
        plan.split,
        min(left, default=None),
        checks=checks,
        notes=notes,
        destinations=places,
        live=session.landed,
        saved=session.replayed,
        strategy=plan.strategy,
    )


class _Stale(Exception):
    """A follow-up needs a live call, but the listing its token came from is too old to trust."""


class _Session:
    """One run's bookkeeping: what has been fetched, what it cost, and how much each source may still be sent."""

    def __init__(self, plan: Plan, on_search):
        self.plan = plan
        self.on_search = on_search
        names = dict.fromkeys([call.provider for call in plan.calls] + list(plan.searchers) + list(plan.reserved))
        self.sources = {name: providers.load(name) for name in names}
        self.replies = dict(plan.replies)
        self.order = list(plan.calls)  # every call asked for so far, in the order results are merged
        self.digested: dict[Call, SearchResult | tuple[Destination, ...]] = {}
        self.adjusted: dict[Query, SearchResult] = {}  # results a closer look has corrected
        self.allowance: dict[str, int] = {}
        for call in plan.missing:
            self.allowance[call.provider] = self.allowance.get(call.provider, 0) + 1
        for name, count in plan.reserved.items():
            self.allowance[name] = self.allowance.get(name, 0) + count
        self.sent: dict[str, int] = {}  # live calls that landed, by source
        self.landed = 0
        self.replayed = len(plan.replies)
        self.refine_left = plan.refine
        self.history: History | None = None
        self.cache: Cache | None = None

    def check_budgets(self) -> None:
        """Ask each source whether it can afford the most this run may send it. Raises before anything is written."""
        for name, count in self.allowance.items():
            if count:
                problem = self.sources[name].budget_problem(count)
                if problem:
                    raise UserError(problem)

    def __enter__(self) -> _Session:
        self.history = History()
        for call in self.plan.replies:
            self._log_replay(call)
        return self

    def __exit__(self, *exc) -> None:
        self.history.close()
        if self.cache is not None:
            try:
                if self.landed:
                    # Replies too old for this run, or any default run, to reuse are dead weight.
                    self.cache.prune(max(self.plan.max_age_hours, DEFAULT_MAX_AGE_HOURS) * 3600)
            finally:
                self.cache.close()

    def _open_cache(self) -> Cache:
        if self.cache is None:
            self.cache = Cache()
        return self.cache

    def _log_replay(self, call: Call) -> None:
        self.history.add_call(call.provider, call.query.endpoint, live=False, cost=0, latency_ms=None, status="ok")

    def can(self, name: str, capability: Capability) -> bool:
        return capability in self.sources[name].capabilities

    def fetch(self, calls: list[Call], *, lookup: bool = True, live: bool = True, refresh: bool = False) -> None:
        """Get a reply for each call that lacks one: from the cache if it is there, else from the source.

        A call its source has no allowance left for is skipped, so callers check `replies`
        afterwards. `live=False` reads the cache only; `refresh` fetches again whatever is held.
        """
        wanted = []
        for call in dict.fromkeys(calls):
            if call in self.replies and not refresh:
                continue
            if call not in self.order:
                self.order.append(call)
            if lookup and not refresh and not self.plan.fresh:
                key = self.sources[call.provider].cache_key(call.query)
                saved = self._open_cache().lookup(key, self.plan.max_age_hours * 3600)
                if saved is not None:
                    body, when = saved
                    self.replies[call] = Reply(body, datetime.fromtimestamp(when, timezone.utc), live=False)
                    self.replayed += 1
                    self._log_replay(call)
                    continue
            if live and self.allowance.get(call.provider, 0) > 0:
                self.allowance[call.provider] -= 1
                wanted.append(call)
        if not wanted:
            return
        self._open_cache()
        error = fetch_all(self.sources, wanted, self._landed)
        if error:
            raise UserError(
                f"stopped after {self.landed} of {self.plan.live} live searches: {error}. "
                f"Finished searches are saved, so running this again picks up where it left off."
            )

    def search(self, queries: list[Query], **options) -> None:
        """Send dated searches to every source that prices them."""
        self.fetch([Call(name, query) for query in queries for name in self.plan.searchers], **options)

    def _landed(self, call: Call, body: dict | None, error: UserError | None, latency_ms: int) -> None:
        source = self.sources[call.provider]
        self.history.add_call(
            call.provider,
            call.query.endpoint,
            live=True,
            cost=0 if error else source.cost_per_call,
            latency_ms=latency_ms,
            status=type(error).__name__ if error else "ok",
        )
        if error:
            return
        now = time.time()
        self.cache.put(source.cache_key(call.query), body, now=now)
        self.replies[call] = Reply(body, datetime.fromtimestamp(now, timezone.utc), live=True)
        self.digested.pop(call, None)
        self.sent[call.provider] = self.sent.get(call.provider, 0) + 1
        self.landed += 1
        # The price point is written now, not after the run: if a later call fails, this
        # reply comes back from the cache next time and would never count as live again.
        try:
            found = self.digest(call)
        except UserError:
            pass  # an unreadable reply is reported once every call has landed
        else:
            if isinstance(found, SearchResult):
                self._record(call, found)
        if self.on_search:
            self.on_search(self.landed, self.plan.live)

    def _record(self, call: Call, found: SearchResult) -> None:
        query = call.query
        # price_points has no column for a market or a bag count, so it holds plain searches only.
        if query.plain and found.cheapest:
            self.history.add_price_point(found.cheapest)
        if query.endpoint == "search" and found.insights and found.insights.price_history:
            reply = self.replies[call]
            when = self.sources[call.provider].fetched_at(reply.body) or reply.fetched_at
            self.history.add_price_history(query, call.provider, found.insights.price_history, when)

    def digest(self, call: Call) -> SearchResult | tuple[Destination, ...]:
        """Parse one reply, and mark what it holds with where and when it came from."""
        if call not in self.digested:
            source, reply = self.sources[call.provider], self.replies[call]
            when = source.fetched_at(reply.body) or reply.fetched_at
            if isinstance(call.query, Explore):
                self.digested[call] = tuple(
                    replace(place, source=source.name, fetched_at=when, verified_live=reply.live)
                    for place in source.destinations(call.query, reply.body)
                )
            else:
                result = _stamp(source.parse(call.query, reply.body), source.name, when, reply.live)
                if not (self.plan.request and self.plan.request.risky):
                    safe = tuple(o for o in result.offers if not (o.hidden_city or o.self_transfer))
                    result = replace(result, offers=safe)
                self.digested[call] = result
        return self.digested[call]

    def result(self, query: Query) -> SearchResult | None:
        """What every source asked so far says about one search, merged; None if none has answered."""
        if query in self.adjusted:
            return self.adjusted[query]
        parts = [self.digest(call) for call in self.order if call.query == query and call in self.replies]
        return _merge(parts) if parts else None

    def results(self) -> dict[Query, SearchResult]:
        queries = dict.fromkeys(
            call.query for call in self.order if call in self.replies and isinstance(call.query, Query)
        )
        return {query: self.result(query) for query in queries}

    def places(self) -> tuple[Destination, ...]:
        found = [
            place
            for call in self.order
            if call in self.replies and isinstance(call.query, Explore)
            for place in self.digest(call)
        ]
        return tuple(sorted(found, key=lambda place: (place.price is None, place.price or 0)))

    def is_old(self, query: Query) -> bool:
        """Whether a listing was saved too long ago for its tokens to be followed with a live call."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=TOKEN_MINUTES)
        return any(
            not self.replies[call].live and self.replies[call].fetched_at < cutoff
            for call in self._followable(query)
        )

    def refresh(self, query: Query) -> bool:
        """Fetch an old listing again. False if there was no allowance left to do it."""
        self.fetch(self._followable(query), refresh=True)
        self.adjusted.pop(query, None)
        return not self.is_old(query)

    def _followable(self, query: Query) -> list[Call]:
        return [
            call
            for call in self.order
            if call.query == query and call in self.replies and self.sources[call.provider].capabilities & _FOLLOW_UPS
        ]


def _ranked(session: _Session, trips: list[Query]) -> list[Query]:
    """The trips that have a fare, cheapest first."""
    priced = []
    for trip in trips:
        result = session.result(trip)
        if result and result.cheapest:
            priced.append((result.cheapest.price, trip.depart_date, trip))
    return [trip for *_, trip in sorted(priced, key=lambda entry: entry[:2])]


def _refine(session: _Session, trips: list[Query]) -> list[Query]:
    """Price the dates a day either side of the cheapest ones found, for as long as the plan allows."""
    plan = session.plan
    base = _base(plan.request)
    tried = {(trip.depart_date, trip.return_date): trip for trip in trips}
    expanded = set()
    while session.refine_left > 0:
        seed = next((trip for trip in _ranked(session, list(tried.values())) if trip not in expanded), None)
        if seed is None:
            break
        expanded.add(seed)
        near = planner.neighbours((seed.depart_date, seed.return_date), *plan.window, plan.request.nights)
        new = [pair for pair in near if pair not in tried][: session.refine_left]
        queries = [replace(base, depart_date=depart, return_date=back) for depart, back in new]
        session.search(queries)
        session.refine_left -= len(new)
        tried.update(zip(new, queries))
    return sorted(tried.values(), key=lambda trip: (trip.depart_date, trip.return_date or trip.depart_date))


def _seed(session: _Session, notes: list[str]) -> list[Query]:
    """Price the dates the explore replies suggest; if they suggest none, sample the window instead."""
    plan = session.plan
    request = plan.request
    suggested: dict[planner.Pair, float | None] = {}
    for call in plan.calls:
        if call in session.replies:
            for place in session.digest(call):
                pair = place.start and planner.fit(place.start, place.end, *plan.window, request.nights)
                if pair:
                    suggested.setdefault(pair, place.price)
    pairs = sorted(suggested, key=lambda pair: (suggested[pair] is None, suggested[pair] or 0, pair[0]))
    if not pairs:
        notes.append("Explore suggested no dates inside the window, so the window was sampled instead.")
        keep = session.refine_left * REFINE_SHARE[0] // REFINE_SHARE[1]
        pairs = planner.sample(*plan.window, request.nights, max(session.refine_left - keep, 1))
    pairs = pairs[: session.refine_left]
    base = _base(request)
    trips = [replace(base, depart_date=depart, return_date=back) for depart, back in pairs]
    session.search(trips)
    session.refine_left -= len(trips)
    return trips


def _pair_up(session: _Session) -> list[Query]:
    """Add up the one-way prices for every pair and price the cheapest few as real round trips."""
    plan = session.plan
    base = _base(plan.request)
    sums = []
    for depart, back in planner.window_pairs(*plan.window, plan.request.nights):
        trip = replace(base, depart_date=depart, return_date=back)
        out, home = (session.result(leg) for leg in one_way_legs(trip))
        if out and home and out.cheapest and home.cheapest:
            sums.append((out.cheapest.price + home.cheapest.price, depart, back, trip))
    sums.sort(key=lambda entry: entry[:3])
    chosen = [trip for *_, trip in sums[: session.refine_left]]
    session.search(chosen)
    session.refine_left -= len(chosen)
    return sorted(chosen, key=lambda trip: (trip.depart_date, trip.return_date))


def _check(session: _Session, trip: Query) -> Check:
    """Look closer at one date pair: confirm its cheapest fare, then set other ways to fly it beside that."""
    stale = False
    try:
        confirmed, failed, sellers, insights = _confirm(session, trip, live=not session.is_old(trip))
    except _Stale:
        if session.refresh(trip):
            confirmed, failed, sellers, insights = _confirm(session, trip, live=True)
        else:
            confirmed, failed, sellers, insights, stale = None, [], [], None, True

    result = session.result(trip)
    offers = [offer for offer in result.offers if offer not in failed]
    if confirmed is not None and confirmed not in offers:
        # The return lookup is the newer word on the price, so it replaces the listing it came from.
        offers = [offer for offer in offers if offer.outbound_key != confirmed.outbound_key or offer.source != confirmed.source]
        offers = sorted([*offers, confirmed], key=lambda o: (o.price, o.duration_min))
    unchecked = tuple(o for o in offers if confirmed is not None and o is not confirmed and o.price < confirmed.price)
    lead = confirmed or (offers[0] if offers else None)

    alternatives = list(sellers)
    request = session.plan.request
    full = request.look == "full"
    if lead is not None:
        source = session.sources[lead.source]
        home = (trip.market or source.default_market).lower()
        other = (request.other_market or result.destination_country).lower()
        if full and home and other and other != home and Capability.MARKETS in source.capabilities:
            abroad_query = replace(trip, market=other)
            session.fetch([Call(lead.source, abroad_query)])
            abroad = session.result(abroad_query)
            if abroad and abroad.cheapest:
                offer = abroad.cheapest
                alternatives.append(Alternative("market", other, offer.price, (offer,), _same(offer, confirmed)))
        for name in session.plan.searchers:
            theirs = next((offer for offer in offers if offer.source == name), None)
            if theirs is not None and name != lead.source:
                alternatives.append(Alternative("source", name, theirs.price, (theirs,), _same(theirs, confirmed)))
    if full and trip.return_date is not None:
        legs = one_way_legs(trip)
        session.search(list(legs))
        out, back = (session.result(leg) for leg in legs)
        if out and back and out.cheapest and back.cheapest:
            pair = (out.cheapest, back.cheapest)
            same = None
            if confirmed is not None and confirmed.return_segments is not None:
                same = (
                    pair[0].outbound_key == confirmed.outbound_key
                    and pair[1].outbound_key == itinerary_key(confirmed.return_segments)
                )
            alternatives.append(Alternative("split", "", pair[0].price + pair[1].price, pair, same))

    session.adjusted[trip] = replace(result, offers=tuple(offers), insights=result.insights or insights)
    alternatives.sort(key=lambda alternative: alternative.price)
    return Check(trip, confirmed, tuple(failed), unchecked, tuple(alternatives), stale)


def _confirm(session: _Session, trip: Query, *, live: bool):
    """Fetch the return flights of the cheapest fare that has any, then who sells it.

    Returns the confirmed offer (None if no fare held up), the listed fares that had no return
    flights, the sellers as alternatives, and the price rating the booking reply carries.
    Raises `_Stale` if that needs a live call and `live` is off.
    """
    result = session.result(trip)

    def follow(call: Call) -> SearchResult | None:
        session.fetch([call], live=live)
        if call in session.replies:
            return session.digest(call)
        if not live:
            raise _Stale
        return None  # the source's allowance for this run is used up

    def followable(offer: Offer) -> bool:
        if trip.return_date is None or offer.return_segments is not None:
            return True
        return bool(offer.departure_token) and session.can(offer.source, Capability.RETURN_FLIGHTS)

    # A fare that came back empty before, at this price, counts as failed without being asked again.
    confirmed, failed = None, known_empty(result.offers, session.plan.request.known_empty)
    for _ in range(RETURN_TRIES):
        listing = _next_to_try([offer for offer in result.offers if followable(offer)], failed)
        if listing is None:
            break
        if trip.return_date is None or listing.return_segments is not None:
            confirmed = listing
            break
        returns = follow(Call(listing.source, trip.follow(departure_token=listing.departure_token)))
        if returns is None:
            break
        if returns.cheapest is None:
            failed.append(listing)
            continue
        back = returns.cheapest
        confirmed = replace(
            listing,
            price=back.price,
            return_segments=back.segments,
            return_duration_min=back.duration_min,
            booking_token=back.booking_token,
            fetched_at=back.fetched_at,
            verified_live=back.verified_live,
        )
        break

    sellers, insights = [], None
    if confirmed is not None and confirmed.booking_token and session.can(confirmed.source, Capability.BOOKING_OPTIONS):
        call = Call(confirmed.source, trip.follow(booking_token=confirmed.booking_token))
        booking = follow(call)
        if booking is not None:
            reply = session.replies[call]
            when = session.sources[call.provider].fetched_at(reply.body) or reply.fetched_at
            insights = booking.insights
            for seller in booking.sellers:
                offer = replace(
                    confirmed,
                    price=seller.price,
                    seller=seller.name,
                    sold_by_airline=seller.airline,
                    separate_tickets=seller.separate_tickets,
                    fetched_at=when,
                    verified_live=reply.live,
                )
                sellers.append(Alternative("seller", seller.name, seller.price, (offer,), True, seller.parts))
    return confirmed, failed, sellers, insights


def _next_to_try(offers: list[Offer], failed: list[Offer]) -> Offer | None:
    """The cheapest fare not yet tried, preferring one flown by other airlines than those that came back empty.

    In the one recorded case the ten cheapest fares were all one airline's, and the first of them
    had no return flights. Spending the spare lookup on the next of the same would likely waste it.
    """
    left = [offer for offer in offers if offer not in failed]
    dead = {frozenset(offer.airlines) for offer in failed}
    return next((offer for offer in left if frozenset(offer.airlines) not in dead), left[0] if left else None)


def known_empty(offers: tuple[Offer, ...], pairs: tuple[tuple[str, float], ...]) -> list[Offer]:
    """The listed fares already found, at the price they are listed at now, to have no return flights."""
    return [offer for offer in offers if (offer.outbound_key, offer.price) in pairs]


def first_to_try(result: SearchResult, pairs: tuple[tuple[str, float], ...] = ()) -> Offer | None:
    """The listed fare a closer look would ask about first, given the fares already known to be empty."""
    return _next_to_try(list(result.offers), known_empty(result.offers, pairs))


def _same(offer: Offer, confirmed: Offer | None) -> bool | None:
    """Whether an offer is the confirmed fare's flights, as far as both are known."""
    if confirmed is None:
        return None
    if offer.outbound_key != confirmed.outbound_key:
        return False
    if offer.return_segments is not None and confirmed.return_segments is not None:
        return offer.itinerary_key == confirmed.itinerary_key
    return True


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
        sellers=tuple(sorted((seller for part in parts for seller in part.sellers), key=lambda s: s.price)),
        origin_country=next((part.origin_country for part in parts if part.origin_country), ""),
        destination_country=next((part.destination_country for part in parts if part.destination_country), ""),
    )


def fetch_all(sources: dict[str, Provider], calls: list[Call], on_done) -> UserError | None:
    """Make the live calls a few at a time, reporting each as it finishes.

    `on_done(call, body, error, latency_ms)` gets the reply or the error, never both.
    The first failure is returned, and no call that has not started yet is sent after it.
    Calls already in flight still finish and are reported, so no paid result is thrown away.
    """
    failed = threading.Event()

    def attempt(call: Call) -> tuple[dict | None, UserError | None, int] | None:
        # Checked on the worker itself: it would otherwise take the next call off the
        # queue before the main thread had heard about the failure.
        if failed.is_set():
            return None
        started = time.monotonic()
        try:
            body, error = sources[call.provider].search(call.query), None
        except UserError as e:
            failed.set()
            body, error = None, e
        return body, error, round((time.monotonic() - started) * 1000)

    first_error = None
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(attempt, call): call for call in calls}
        for future in as_completed(futures):
            attempted = future.result()
            if attempted is None:
                continue
            body, error, latency_ms = attempted
            on_done(futures[future], body, error, latency_ms)
            if error and first_error is None:
                first_error = error
    return first_error
