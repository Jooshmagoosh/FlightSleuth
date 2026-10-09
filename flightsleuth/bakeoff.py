"""Measure, on a file of routes, what each way of finding a fare turns up against SerpApi's own."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, links, render, scan, typed
from .errors import UserError
from .history import History
from .models import Check, Offer, Query

DEFAULT_ROUTES = 6  # a short run: the routes at the top of the file
DEFAULT_BUDGET = 150  # the most live searches one bake-off may plan for
# Kept back for each route: one search in Google's own order, made only if a fare fails its return lookup.
PROBE = 1
BASELINE = "baseline"
MEASURES = {"split": "Two one-way tickets", "one ticket": "Same flights on one ticket"}

_LINE = re.compile(
    r"^(?P<origin>[^|]+?)\s+to\s+(?P<destination>[^|]+?)\s*\|\s*depart between\s+(?P<earliest>\S+)\s+and\s+"
    r"(?P<latest>\S+)\s*\|\s*(?P<length>[^|]+?)\s*$",
    re.IGNORECASE,
)
_NIGHTS = re.compile(r"^(\d+)(?:\s+to\s+(\d+))?\s+nights?$", re.IGNORECASE)


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    earliest: date
    latest: date
    nights: tuple[int, int] | None  # None for a one-way

    @property
    def label(self) -> str:
        return f"{self.origin} to {self.destination}"

    def request(self) -> scan.Request:
        return scan.Request(
            self.origin, self.destination, self.earliest, nights=self.nights, window=(self.earliest, self.latest), check=True
        )


def read_routes(path: Path) -> list[Route]:
    """Routes from a file, in its order. A line reads:

    MCO,SFB to JFK,EWR | depart between 2026-11-10 and 2026-12-20 | 2 to 4 nights

    The length may also be "7 nights" or "one way". Blank lines and lines starting with # are skipped.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise UserError(f"cannot read the route file {path}: {e.strerror}") from None
    routes = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            routes.append(_route(line))
        except (UserError, ValueError) as e:
            raise UserError(f"{path.name} line {number}: {e}") from None
    if not routes:
        raise UserError(f"{path.name} has no routes in it")
    return routes


def _route(line: str) -> Route:
    match = _LINE.match(line)
    if not match:
        raise UserError('expected "AAA to BBB | depart between YYYY-MM-DD and YYYY-MM-DD | N to M nights"')
    length = match["length"].strip()
    nights = None
    if length.lower() != "one way":
        found = _NIGHTS.match(length)
        if not found:
            raise UserError(f'{length!r} is not a trip length like "7 nights", "5 to 9 nights" or "one way"')
        nights = (int(found[1]), int(found[2] or found[1]))
        if nights[1] < nights[0]:
            raise UserError(f"{length!r} runs backwards")
    earliest, latest = date.fromisoformat(match["earliest"]), date.fromisoformat(match["latest"])
    return Route(scan.airports(match["origin"]), scan.airports(match["destination"]), earliest, latest, nights)


def plan(
    routes: list[Route], *, budget: int = DEFAULT_BUDGET, today: date | None = None, **options
) -> list[tuple[Route, scan.Plan]]:
    """One search plan per route, sized so that all of them together fit the budget. Costs nothing."""
    each = min(scan.DEFAULT_MAX_SEARCHES, budget // len(routes))
    if each <= PROBE:
        raise UserError(f"a budget of {budget} live searches is too small for {len(routes)} routes")
    return [(route, scan.plan(route.request(), max_searches=each - PROBE, today=today, **options)) for route in routes]


def ceiling(plans: list[tuple[Route, scan.Plan]]) -> int:
    """The most live searches the whole bake-off can make."""
    return sum(plan.live + (PROBE if plan.live else 0) for _, plan in plans)


def _flights(offer: Offer) -> str:
    text = ", ".join(segment.flight_number or segment.carrier for segment in offer.segments)
    if offer.return_segments:
        text += " / " + ", ".join(segment.flight_number or segment.carrier for segment in offer.return_segments)
    return text


def _how(offers) -> str:
    return "live" if all(offer.verified_live for offer in offers) else "saved"


def _when(offers) -> datetime:
    return min((offer.fetched_at for offer in offers if offer.fetched_at), default=datetime.now(timezone.utc))


def run_route(run: str, route: Route, search: scan.Plan, *, on_search=None, ask=None, open_link=None, say=print) -> scan.Outcome:
    """Search one route, record what each measure found, and ask for the sites that have to be read by eye.

    `ask(prompt)` returns what the owner typed, and `open_link(url)` shows them the page; leave
    `ask` out to skip the typed prices.
    """
    outcome = scan.run(search, on_search=on_search)
    if not outcome.trips:
        say(f"{route.label}: no fares found.")
        return outcome
    check = outcome.checks[0] if outcome.checks else None
    trip = check.trip if check else outcome.trips[0]
    base = (check.confirmed if check else None) or outcome.results[trip].cheapest
    confirmed = bool(check and check.confirmed)

    probed = _probe(trip, check.failed) if check and check.failed else None
    history = History(config.history_path())
    try:
        def fare(measure, price, offers, *, same, detail, how=None):
            history.add_bakeoff_fare(
                run, route.label, trip, measure, price, same_flights=same, how=how or _how(offers), detail=detail,
                fetched_at=_when(offers),
            )

        if base is not None:
            note = "" if confirmed or trip.return_date is None else "listed, not confirmed: "
            fare(BASELINE, base.price, [base], same=None, detail=note + _flights(base))
        if check:
            for position, missed in enumerate(check.failed):
                found = None if probed is None else missed.outbound_key in probed
                history.add_bakeoff_lookup(run, route.label, missed, cheapest=position == 0, found=False, in_unsorted=found)
            if confirmed and trip.return_date is not None:
                history.add_bakeoff_lookup(run, route.label, check.confirmed, cheapest=not check.failed, found=True)
            _record_alternatives(check, fare)
        say(_route_lines(route, trip, base, confirmed, check, trip.currency))
        if ask and base is not None:
            for label, url in links.other_sites(trip):
                typed = _typed(label, url, trip.currency, ask, open_link, say)
                if typed is not None:
                    price, same = typed
                    fare(label.split(" (")[0], price, [], same=same, detail="typed from the site", how="typed")
    finally:
        history.close()
    return outcome


def _probe(trip: Query, failed: tuple[Offer, ...]) -> set[str] | None:
    """Whether Google's own order would have listed the fares that had no return flights: one more search."""
    request = scan.Request(
        trip.origin, trip.destination, trip.depart_date, return_date=trip.return_date, adults=trip.adults,
        cabin=trip.cabin, stops=trip.stops, currency=trip.currency, by_price=False,
    )
    try:
        search = scan.plan(request, max_searches=PROBE)
        result = scan.run(search).results[search.trips[0]]
    except UserError:
        return None  # the lookup is recorded all the same, with this question left open
    return {offer.outbound_key for offer in result.offers}


def _record_alternatives(check: Check, fare) -> None:
    sellers = [found for found in check.alternatives if found.kind == "seller"]
    if sellers and sellers[0].offers[0].separate_tickets:
        whole = next((found for found in sellers if not found.offers[0].separate_tickets), None)
        if whole:
            fare("one ticket", whole.price, whole.offers, same=True, detail=f"sold by {whole.name}")
    for found in check.alternatives:
        if found.kind == "seller":
            continue
        measure = {"split": "split", "market": f"market:{found.name}"}.get(found.kind, found.name)
        detail = " + ".join(_flights(offer) for offer in found.offers)
        fare(measure, found.price, found.offers, same=found.same_flights, detail=detail)


def _route_lines(route: Route, trip: Query, base: Offer | None, confirmed: bool, check: Check | None, currency: str) -> str:
    if base is None:
        return f"{route.label}: every fare listed for the best dates failed its return lookup."
    state = "confirmed" if confirmed or trip.return_date is None else "listed, not confirmed"
    dates = render.day(trip.depart_date) + (f" - {render.day(trip.return_date)}" if trip.return_date else "")
    lines = [f"{route.label}: {render.money(base.price, currency)} {state}, {dates}, {_flights(base)}"]
    lines[0] += f", fetched {render.fetched(base.fetched_at, base.verified_live)}"
    for found in (check.alternatives if check else ()):
        if found.kind != "seller":
            row = render.alternative_cells(found, currency)
            lines.append("  " + "  ".join(cell for cell in row if cell))
    return "\n".join(lines)


def _typed(label: str, url: str, currency: str, ask, open_link, say) -> tuple[float, bool | None] | None:
    say(f"  {label}: {url}")
    if open_link:
        open_link(url)
    price = typed.ask_price(
        ask, lambda line: say("  " + line), f"  Best {currency} price there for these dates (Enter to skip): "
    )
    if price is None:
        return None
    return price, typed.yes_no(ask("  Is that the same flights as above? y, n, or Enter if unsure: "))


def new_run() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def report() -> str:
    """Everything recorded, for each route's most recent bake-off, as plain text."""
    history = History(config.history_path())
    try:
        fares, lookups = history.bakeoff_fares(), history.bakeoff_lookups()
    finally:
        history.close()
    latest: dict[str, dict] = {}
    for row in fares:
        if row["measure"] == BASELINE:
            latest[row["route"]] = row  # rows come back oldest first, so the last one stands
    if not latest:
        return "No bake-off has been recorded yet."

    def same_run(row: dict) -> bool:
        return latest.get(row["route"], {}).get("run") == row["run"]

    measured: dict[str, dict[str, dict]] = {}
    for row in fares:
        if row["measure"] != BASELINE and same_run(row):
            measured.setdefault(row["measure"], {})[row["route"]] = row

    lines = [f"Bake-off report: {len(latest)} route{'s' if len(latest) != 1 else ''}", ""]
    body = []
    for route, row in latest.items():
        dates = render.day(date.fromisoformat(row["depart_date"]))
        if row["return_date"]:
            dates += " - " + render.day(date.fromisoformat(row["return_date"]))
        when = render.fetched(datetime.fromisoformat(row["fetched_at"]), row["how"] == "live")
        body.append([route, dates, render.money(row["price"], row["currency"]), row["detail"], when])
    lines += render.table(["Route", "Dates", "SerpApi", "Flights", "Fetched"], body) + [""]

    whole = measured.pop("one ticket", {})
    if measured:
        lines.append("Against SerpApi's fare for the same dates:")
        body = [_measure_cells(measure, rows, latest) for measure, rows in measured.items()]
        headers = ["Measure", "Compared", "Cheaper", "Same flights", "Different itinerary", "Flights not known"]
        lines += render.table(headers, body) + [""]
    else:
        lines += ["Nothing was measured against it.", ""]
    if whole:
        extra = statistics.mean(row["price"] - latest[route]["price"] for route, row in whole.items())
        currency = next(iter(latest.values()))["currency"]
        lines.append(
            f"Separate tickets: SerpApi's cheapest fare was two separate tickets on {len(whole)} of {len(latest)} "
            f"routes. The same flights on one ticket cost {render.money(round(extra, 2), currency)} more on average."
        )

    mine = [row for row in lookups if same_run(row)]
    first = [row for row in mine if row["cheapest"]]
    missed = [row for row in first if not row["found"]]
    if first:
        lines.append(
            f"Return lookups: the cheapest fare listed had no return flights on {len(missed)} of {len(first)} routes."
        )
        probed = [row for row in missed if row["in_unsorted"] is not None]
        if probed:
            absent = sum(1 for row in probed if not row["in_unsorted"])
            lines.append(
                f"Google's own order (no sort_by) left that fare out {absent} of {len(probed)} times, "
                f"so it would have avoided {absent} of those misses."
            )
    return "\n".join(lines).rstrip()


def _measure_cells(measure: str, rows: dict[str, dict], latest: dict[str, dict]) -> list[str]:
    name = MEASURES.get(measure) or (f"As sold in {measure[7:].upper()}" if measure.startswith("market:") else measure)
    if any(row["how"] == "typed" for row in rows.values()):
        name += " (typed)"
    savings: dict[int | None, list[tuple[float, float]]] = {1: [], 0: [], None: []}
    for route, row in rows.items():
        base = latest[route]["price"]
        if row["price"] < base:
            savings[row["same_flights"]].append((base - row["price"], (base - row["price"]) / base))
    currency = next(iter(latest.values()))["currency"]

    def cell(found: list[tuple[float, float]]) -> str:
        if not found:
            return "-"
        saved = render.money(round(statistics.mean(amount for amount, _ in found), 2), currency)
        share = statistics.mean(share for _, share in found)
        return f"{len(found)}, saving {saved} ({share:.0%}) on average"

    cheaper = sum(len(found) for found in savings.values())
    return [name, str(len(rows)), str(cheaper), cell(savings[1]), cell(savings[0]), cell(savings[None])]
