"""Turns search results into the rows and sentences that both front ends show."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING

from . import links
from .models import Alternative, Check, Destination, Insights, Offer, Query, SearchResult, Segment
from .planner import one_way_legs

if TYPE_CHECKING:  # watch words its alerts through this module, so it cannot be imported here for real
    from . import watch

NO_FARES = "No fares found. Try other dates, more airports, or fewer filters."
UNCONFIRMED = "This is the listed price: its return flights have not been looked up."
NEXT_STEP = "Check this fare on {site}"
FREE_CHECK = "Checking it on {site} is a free way to confirm it: it uses none of your searches."
UNSENT = "The link carries only the airports and dates. Set the page to {what} before comparing prices."
LONG_LAYOVER_MIN = 5 * 60
SELLERS_SHOWN = 3
ALTERNATIVE_HEADERS = ["Price", "How", "Flights", "Catch", "Fetched"]
PLACE_HEADERS = ["Price", "Place", "Airport", "Dates", "Stops", "Time", "Airline", "Fetched"]


def money(amount: float, currency: str) -> str:
    text = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"${text}" if currency == "USD" else f"{text} {currency}"


def duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m"


def day(d: date) -> str:
    return d.strftime("%a %d %b")


def stops(count: int) -> str:
    return "nonstop" if count == 0 else f"{count} stop{'s' if count > 1 else ''}"


def times(offer: Offer) -> str:
    return _times(offer.depart_at, offer.arrive_at)


def _times(leaves: datetime, lands: datetime) -> str:
    text = f"{leaves:%H:%M} -> {lands:%H:%M}"
    days_later = (lands.date() - leaves.date()).days
    return f"{text} +{days_later}" if days_later else text


def fetched(when: datetime | None, live: bool) -> str:
    """When a price was fetched, on this machine's clock, and whether that was before this run."""
    if when is None:
        return "time unknown"
    text = when.astimezone().strftime("%d %b %H:%M")
    return text if live else f"{text} (saved)"


def _oldest(offers: list[Offer]) -> str:
    """One fetch time for several prices shown together: the oldest, and saved if any of them is."""
    known = [offer.fetched_at for offer in offers if offer.fetched_at]
    return fetched(min(known, default=None), all(offer.verified_live for offer in offers))


def catches(offer: Offer) -> list[str]:
    """What a buyer should know before taking this fare at its price."""
    found = []
    if offer.hidden_city:
        found.append("hidden-city: you leave the trip at a stop")
    if offer.self_transfer:
        found.append("self-transfer: you change planes on your own")
    if offer.separate_tickets:
        found.append("two separate tickets")
    if offer.sold_by_airline is False:
        found.append("an agency, not the airline")
    if offer.basic_economy:
        found.append("basic economy")
    if offer.longest_layover_min >= LONG_LAYOVER_MIN:
        found.append(f"{duration(offer.longest_layover_min)} layover")
    return found


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows)]
    lines = [headers, ["-" * w for w in widths], *rows]
    return ["  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in lines]


def _dates(trip: Query) -> str:
    if trip.return_date is None:
        return day(trip.depart_date)
    return f"{day(trip.depart_date)} - {day(trip.return_date)}"


def _flights(segments: tuple[Segment, ...]) -> str:
    """Who flies a leg, with the flight numbers when the source gave them."""
    airlines = " + ".join(dict.fromkeys(segment.carrier for segment in segments))
    numbers = ", ".join(segment.flight_number for segment in segments if segment.flight_number)
    return f"{airlines} ({numbers})" if numbers else airlines


def _leg(label: str, segments: tuple[Segment, ...], minutes: int | None) -> list[str]:
    return [
        label,
        f"{segments[0].origin}->{segments[-1].destination}",
        f"{day(segments[0].depart_at.date())} {_times(segments[0].depart_at, segments[-1].arrive_at)}",
        stops(len(segments) - 1),
        duration(minutes) if minutes else "",
        _flights(segments),
    ]


@dataclass
class DateRow:
    """One date pair, priced as a single ticket and, if asked, as two one-ways."""

    trip: Query
    whole: Offer | None
    halves: tuple[Offer, Offer] | None

    @property
    def halves_price(self) -> float | None:
        return self.halves[0].price + self.halves[1].price if self.halves else None

    @property
    def price(self) -> float | None:
        prices = [self.whole.price if self.whole else None, self.halves_price]
        return min((p for p in prices if p is not None), default=None)

    @property
    def split_wins(self) -> bool:
        return self.halves is not None and (self.whole is None or self.halves_price < self.whole.price)


@dataclass
class Closer:
    """One closer look at a date pair's best fare, already worded."""

    title: str
    legs: list[list[str]]  # the confirmed flights out and back
    lines: list[str]  # what was and was not confirmed
    alternatives: list[list[str]]  # rows under ALTERNATIVE_HEADERS, cheapest first


@dataclass
class NextStep:
    """What to do next with the best fare: look it up on the site that has most often listed it for less."""

    site: str  # as it is recorded, such as "Momondo"
    label: str  # the words on the link
    url: str
    lines: list[str]  # why, and what to set on the page first
    trip: Query
    fare: Offer  # the fare a price found there is set against
    confirmed: bool  # whether that fare's return flights had been fetched

    @property
    def against(self) -> str:
        """That fare in a line, with when it was fetched."""
        state = "confirmed" if self.confirmed else "listed, not confirmed"
        when = fetched(self.fare.fetched_at, self.fare.verified_live)
        return f"{_dates(self.trip)}: {money(self.fare.price, self.trip.currency)} {state}, fetched {when}"


@dataclass
class Summary:
    """Everything worth showing about a scan, already worded but not yet laid out."""

    title: str
    round_trip: bool
    split: bool
    currency: str
    rows: list[DateRow]
    best: DateRow | None
    offers: list[Offer]  # cheapest first
    insight: str | None  # Google's rating of the best price
    links: list[tuple[str, str]]  # (label, url) for booking the best option
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what the search decided along the way
    more_links: list[tuple[str, str]] = field(default_factory=list)  # the best dates on other sites
    next_step: NextStep | None = None  # the one of those sites to open first, for a round trip

    @property
    def other_links(self) -> list[tuple[str, str]]:
        """The other sites' links, less the one the next step already offers."""
        return [link for link in self.more_links if not self.next_step or link[1] != self.next_step.url]

    @property
    def check(self) -> Check | None:
        """The closer look at the best date pair, if it had one."""
        return next((check for check in self.checks if self.best and check.trip == self.best.trip), None)

    @property
    def headline(self) -> str:
        if self.best is None:
            return NO_FARES
        price = money(self.best.price, self.currency)
        if self.best.split_wins:
            return f"Cheapest: {price} as two one-way tickets, {_dates(self.best.trip)}"
        kind = "round trip" if self.round_trip else "one way"
        if self.round_trip:
            # Only a round trip whose return flights were fetched may be called the cheapest.
            listed = f"Cheapest listed: {price} {kind}, {_dates(self.best.trip)}"
            if not self.checks:
                return f"{listed} (not yet confirmed)"  # nobody has looked
            check = self.check
            if check is None or check.confirmed is None:
                return f"{listed} (not confirmed)"  # somebody looked, and it did not hold up
            price = money(check.confirmed.price, self.currency)
            if check.unchecked:
                return f"Cheapest confirmed: {price} {kind}, {_dates(self.best.trip)}"
        return f"Cheapest: {price} {kind}, {_dates(self.best.trip)}"

    @property
    def caveat(self) -> str | None:
        """What to say under a round-trip price nobody has looked at closely."""
        if self.best is None or not self.round_trip or self.best.split_wins or self.checks:
            return None
        return UNCONFIRMED

    @property
    def closer(self) -> list[Closer]:
        """The closer looks, the best date pair's first."""
        ordered = sorted(self.checks, key=lambda check: check is not self.check)
        return [_closer(check, self.currency) for check in ordered]

    @property
    def date_headers(self) -> list[str]:
        headers = ["Depart", "Return", "Round trip"] if self.round_trip else ["Depart", "Cheapest"]
        return headers + ["Two one-ways"] if self.split else headers

    def date_cells(self, row: DateRow) -> list[str]:
        cells = [day(row.trip.depart_date)]
        if self.round_trip:
            cells.append(day(row.trip.return_date))
        cells.append(money(row.whole.price, self.currency) if row.whole else "-")
        if self.split:
            cells.append(money(row.halves_price, self.currency) if row.halves else "-")
        return cells

    def date_fetched(self, row: DateRow) -> str:
        """When the prices in one date row were fetched."""
        return _oldest([offer for offer in (row.whole, *(row.halves or ())) if offer])

    @property
    def offer_headers(self) -> list[str]:
        if self.round_trip:
            return ["Price", "Route", "Outbound", "Return", "Stops", "Time", "Airline"]
        return ["Price", "Route", "Departs", "Stops", "Time", "Airline"]

    def offer_cells(self, offer: Offer) -> list[str]:
        cells = self._flight_cells(offer)
        if self.round_trip:
            cells.insert(3, day(offer.query.return_date))
        return cells

    def offer_fetched(self, offer: Offer) -> str:
        return fetched(offer.fetched_at, offer.verified_live)

    def offer_catch(self, offer: Offer) -> str:
        return "; ".join(catches(offer))

    @property
    def split_legs(self) -> list[list[str]]:
        """The two one-way flights, when buying them separately is the cheapest option."""
        if self.best is None or not self.best.split_wins:
            return []
        return [[label, *self._flight_cells(leg)] for label, leg in zip(("out", "back"), self.best.halves)]

    def _flight_cells(self, offer: Offer) -> list[str]:
        return [
            money(offer.price, self.currency),
            f"{offer.origin}->{offer.destination}",
            f"{day(offer.depart_at.date())} {times(offer)}",
            stops(offer.stops),
            duration(offer.duration_min),
            " + ".join(offer.airlines),
        ]


def _closer(check: Check, currency: str) -> Closer:
    confirmed = check.confirmed
    round_trip = check.trip.return_date is not None
    legs, lines = [], []
    for missed in check.failed:
        lines.append(
            f"The {money(missed.price, currency)} fare listed for these dates ({_flights(missed.segments)}) "
            f"had no return flights, so it was left out."
        )
    if confirmed is None:
        title = f"{_dates(check.trip)}: not confirmed"
        if check.stale:
            lines.append(
                "That listing was saved too long ago to look up its return flights. "
                "Search again with fresh results to confirm it."
            )
        elif not check.failed:
            lines.append("The source gave no way to look up the return flights of these fares.")
    else:
        title = f"{_dates(check.trip)}: {money(confirmed.price, currency)} confirmed"
        legs.append(_leg("out", confirmed.segments, confirmed.duration_min))
        if confirmed.return_segments:
            legs.append(_leg("back", confirmed.return_segments, confirmed.return_duration_min))
        what = "Return flights" if round_trip else "Fare"
        lines.append(f"{what} fetched {fetched(confirmed.fetched_at, confirmed.verified_live)}.")
        if check.unchecked:
            count = len(check.unchecked)
            lines.append(
                f"{count} cheaper fare{'s are' if count > 1 else ' is'} listed for these dates, "
                f"from {money(check.unchecked[0].price, currency)}, but {'their' if count > 1 else 'its'} "
                f"return flights were not looked up."
            )
    return Closer(title, legs, lines, [alternative_cells(found, currency) for found in _shown(check.alternatives)])


def _shown(alternatives: tuple[Alternative, ...]) -> list[Alternative]:
    """Every comparison, but only the cheapest few sellers.

    The cheapest seller of one ticket is always among them, and so is the cheapest airline selling
    one: those are the prices to weigh a split or an agency fare against.
    """
    sellers = [found for found in alternatives if found.kind == "seller"]
    keep = sellers[:SELLERS_SHOWN]
    whole = [found for found in sellers if not found.offers[0].separate_tickets]
    for extra in (next(iter(whole), None), next((found for found in whole if found.offers[0].sold_by_airline), None)):
        if extra and extra not in keep:
            keep.append(extra)
    return [found for found in alternatives if found.kind != "seller" or found in keep]


def alternative_cells(found: Alternative, currency: str) -> list[str]:
    offer = found.offers[0]
    notes = [note for note in catches(offer) if found.kind == "seller" or note != "two separate tickets"]
    if found.kind == "seller":
        how = f"Buy from {found.name}"
        if found.parts:
            sold = " + ".join(f"{name} {money(price, currency)}" for name, price in found.parts)
            notes = [f"two separate tickets ({sold})" if note == "two separate tickets" else note for note in notes]
        elif not offer.separate_tickets:
            notes.insert(0, "one ticket")
    elif found.kind == "market":
        how = f"As sold in {found.name.upper()}"
    elif found.kind == "split":
        how = "Two one-way tickets"
        prices = " + ".join(money(leg.price, currency) for leg in found.offers)
        notes = [f"two separate tickets ({prices})"] + [n for leg in found.offers[1:] for n in catches(leg)]
    else:
        how = f"From {found.name}"
    if found.kind in ("market", "source") and offer.query.return_date and offer.return_segments is None:
        notes.append("listed; return flights not looked up")
    if found.same_flights is None:
        flights = ""
    elif found.same_flights:
        flights = "same flights" if found.kind in ("seller", "split") or offer.return_segments else "same outbound flights"
    else:
        flights = "different itinerary"
    return [money(found.price, currency), how, flights, "; ".join(notes), _oldest(list(found.offers))]


def _date_row(trip: Query, results: dict[Query, SearchResult], split: bool) -> DateRow:
    halves = None
    if split:
        out, back = (results[leg].cheapest for leg in one_way_legs(trip))
        if out and back:
            halves = (out, back)
    return DateRow(trip, results[trip].cheapest, halves)


def _insight(insights: Insights | None, currency: str) -> str | None:
    if not insights or not insights.level or insights.lowest_price is None:
        return None
    note = f"Google rates {money(insights.lowest_price, currency)} as {insights.level} for those dates"
    if insights.typical_range:
        low, high = (money(p, currency) for p in insights.typical_range)
        note += f" (typical range {low} to {high})"
    return note + "."


def _most_stops(trip: Query) -> str:
    return "nonstop only" if trip.stops == "nonstop" else f"{stops(int(trip.stops))} or fewer"


def _unsent(trip: Query) -> list[str]:
    """What a search asked for that a link to another site does not carry."""
    asked = []
    if trip.adults != 1:
        asked.append(f"{trip.adults} adults")
    if trip.cabin != "economy":
        asked.append("premium economy" if trip.cabin == "premium" else f"{trip.cabin} class")
    if trip.stops != "any":
        asked.append(_most_stops(trip))
    if trip.bags:
        asked.append(f"{trip.bags} carry-on bag{'s' if trip.bags != 1 else ''} in the price")
    return asked


def _next_step(best: DateRow | None, checks: list[Check]) -> NextStep | None:
    """Offered for a round trip sold as one ticket. A one-way's link to that site has never been checked."""
    if best is None or best.whole is None or best.trip.return_date is None or best.split_wins:
        return None
    first = links.first_site(best.trip)
    if first is None:
        return None
    check = next((check for check in checks if check.trip == best.trip), None)
    confirmed = check.confirmed if check else None
    lines = [first.why] if confirmed else [FREE_CHECK.format(site=first.name), first.why]
    asked = _unsent(best.trip)
    if asked:
        lines.append(UNSENT.format(what=", ".join(asked)))
    return NextStep(
        site=first.name,
        label=NEXT_STEP.format(site=first.label),
        url=first.url,
        lines=lines,
        trip=best.trip,
        fare=confirmed or best.whole,
        confirmed=confirmed is not None,
    )


def recorded(step: NextStep, price: float, seller: str, typed_at: datetime) -> str:
    """What was just kept about a price read on the step's site, and how it compares with the fare of ours."""
    currency = step.trip.currency
    there = f"{step.site}: {money(price, currency)}" + (f" from {seller}" if seller else "")
    gap = round(step.fare.price - price, 2)
    if gap:
        versus = f"{money(abs(gap), currency)} {'less' if gap > 0 else 'more'} than"
    else:
        versus = "the same as"
    ours = (
        f"the {money(step.fare.price, currency)} {'confirmed' if step.confirmed else 'listed'} fare "
        f"fetched {fetched(step.fare.fetched_at, step.fare.verified_live)}"
    )
    return f"Saved. {there}, typed {fetched(typed_at, True)}. That is {versus} {ours}."


def summarise(
    trips: list[Query],
    results: dict[Query, SearchResult],
    *,
    split: bool = False,
    top: int = 10,
    checks: list[Check] | None = None,
    notes: list[str] | None = None,
) -> Summary:
    first = trips[0]
    round_trip = first.return_date is not None
    kind = "round trip" if round_trip else "one way"
    people = f"{first.adults} adult{'s' if first.adults != 1 else ''}"
    title = f"{first.origin} -> {first.destination}, {kind}, {people}, {first.cabin}"
    if first.bags:
        title += f", {first.bags} carry-on bag{'s' if first.bags != 1 else ''} in every price"
    if first.market:
        title += f", priced for {first.market.upper()}"

    rows = [_date_row(trip, results, split) for trip in trips]
    best = min((row for row in rows if row.price is not None), key=lambda row: row.price, default=None)
    offers = sorted(
        (offer for trip in trips for offer in results[trip].offers),
        key=lambda o: (o.price, o.duration_min),
    )[:top]

    insight, booking = None, []
    if best and best.split_wins:
        # The round trip lost, so its price rating and link would describe the wrong ticket.
        for label, leg in zip(("out", "back"), one_way_legs(best.trip)):
            if results[leg].url:
                booking.append((f"Check and book the flight {label}", results[leg].url))
    elif best:
        result = results[best.trip]
        insight = _insight(result.insights, first.currency)
        if result.url:
            booking.append(("Check and book", result.url))

    return Summary(
        title=title,
        round_trip=round_trip,
        split=split,
        currency=first.currency,
        rows=rows,
        best=best,
        offers=offers,
        insight=insight,
        links=booking,
        checks=list(checks or []),
        notes=list(notes or []),
        more_links=links.other_sites(best.trip) if best else [],
        next_step=_next_step(best, list(checks or [])),
    )


def render_text(summary: Summary, *, hint: str = "") -> str:
    """Plain ASCII, so it survives whatever code page the console is using.

    `hint` is how this front end lets the user confirm a price nobody has looked at closely.
    """
    lines = [summary.title, ""]
    if summary.best is None:
        # A closer look can empty the list: every fare it tried may have had no return flights.
        said = [line for closer in summary.closer for line in closer.lines]
        return "\n".join(lines + [NO_FARES] + said)

    if len(summary.rows) > 1 or summary.split:
        body = [
            summary.date_cells(row) + ["<- cheapest" if row is summary.best else "", summary.date_fetched(row)]
            for row in summary.rows
        ]
        lines += table(summary.date_headers + ["", "Fetched"], body) + [""]

    if summary.offers:
        note = " (price is the round-trip total, flight shown is the outbound)" if summary.round_trip else ""
        lines.append(f"Cheapest itineraries{note}:")
        caught = any(summary.offer_catch(offer) for offer in summary.offers)
        headers = summary.offer_headers + ["Fetched"] + (["Catch"] if caught else [])
        body = [
            summary.offer_cells(offer)
            + [summary.offer_fetched(offer)]
            + ([summary.offer_catch(offer)] if caught else [])
            for offer in summary.offers
        ]
        lines += table(headers, body) + [""]

    lines.append(summary.headline)
    if summary.split_legs:
        legs = table(["", "Price", "Route", "Departs", "Stops", "Time", "Airline"], summary.split_legs)
        lines += ["  " + line for line in legs[2:]]
    if summary.insight:
        lines.append(summary.insight)
    if summary.caveat:
        lines.append(f"{summary.caveat} {hint}".rstrip())
    for closer in summary.closer:
        lines += ["", closer.title]
        if closer.legs:
            lines += ["  " + line for line in table([""] * 6, closer.legs)[2:]]
        lines += ["  " + line for line in closer.lines]
        if closer.alternatives:
            lines += ["  Other ways to fly these dates:"]
            lines += ["  " + line for line in table(ALTERNATIVE_HEADERS, closer.alternatives)]
    if summary.notes:
        lines += ["", *summary.notes]
    lines += [""] if summary.closer or summary.notes else []
    if summary.next_step:
        lines += [f"{summary.next_step.label}: {summary.next_step.url}"]
        lines += ["  " + line for line in summary.next_step.lines]
    lines += [f"{label}: {url}" for label, url in summary.links]
    lines += [f"Same dates on {label}: {url}" for label, url in summary.other_links]
    return "\n".join(lines)


def watch_title(trip: Query) -> str:
    """A watch in a few words: where and when."""
    return f"{trip.origin} -> {trip.destination}, {_dates(trip)}"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}{'es' if noun.endswith('ch') else 's'}"


def why(found: watch.Fired, price: float, currency: str) -> str:
    """One rule a price satisfied, as the alert says it."""
    if found.rule == "below":
        return f"below the {money(found.mark, currency)} you set"
    if found.rule == "median":
        under = (found.mark - price) / found.mark * 100
        return f"{under:.0f}% under the {found.days}-day median of {money(found.mark, currency)}"
    return f"below Google's typical range of {money(found.mark, currency)} to {money(found.top, currency)}"


def _sold(check: Check, currency: str) -> str:
    """Who sells the confirmed fare, and whether its price is for one ticket or two."""
    sellers = [found for found in check.alternatives if found.kind == "seller"]
    if not sellers:
        return "Who sells it was not looked up."
    cheapest = sellers[0]
    offer = cheapest.offers[0]
    if not offer.separate_tickets:
        agency = ", an agency, not the airline" if offer.sold_by_airline is False else ""
        return f"Sold on one ticket by {cheapest.name}{agency} at {money(cheapest.price, currency)}."
    parts = " + ".join(f"{name} {money(price, currency)}" for name, price in cheapest.parts)
    text = f"{money(cheapest.price, currency)} is two separate tickets" + (f" ({parts})." if parts else ".")
    whole = next((found for found in sellers if not found.offers[0].separate_tickets), None)
    if whole is None:
        return f"{text} No seller listed it on one ticket."
    agency = ", an agency, not the airline" if whole.offers[0].sold_by_airline is False else ""
    return f"{text} On one ticket it is {money(whole.price, currency)} from {whole.name}{agency}."


def alert(check: Check, reasons: list[str]) -> tuple[str, str, str]:
    """A price alert: its title, its text and the address it opens.

    Only ever built for a fare the closer look confirmed, so a round trip's return flights have
    been fetched by the time anybody is told about it.
    """
    trip, fare = check.trip, check.confirmed
    currency = trip.currency
    closer = _closer(check, currency)
    title = (
        f"{trip.origin} to {trip.destination}: {money(fare.price, currency)} "
        f"{'round trip' if trip.return_date else 'one way'}"
    )
    lines = [f"{_dates(trip)}. Why: {'; '.join(reasons)}."]
    for label, route, when, stop_count, time, flights in closer.legs:
        lines.append(f"{label.capitalize()}: {route} {when}, {', '.join(part for part in (stop_count, time, flights) if part)}.")
    lines += closer.lines
    lines.append(_sold(check, currency))
    first = links.first_site(trip)
    if first is not None:
        lines.append(f"Tap to check it on {first.name}.")
        asked = _unsent(trip)
        if asked:
            lines.append(UNSENT.format(what=", ".join(asked)))
    return title, "\n".join(lines), first.url if first else ""


def outage(trip: Query, last_day: date | None, due: date, last_said: str) -> tuple[str, str]:
    """What to tell the owner, once, about a watch that has gone unchecked for too long."""
    title = f"FlightSleuth has not checked {trip.origin} to {trip.destination}"
    last = f"It was last checked on {day(last_day)}" if last_day else "It has never been checked"
    lines = [f"{_dates(trip)}. {last}, and a check has been due since {day(due)}."]
    if last_said:
        lines.append(f"The last try: {last_said}")
    lines.append('See where it stands with "py -m flightsleuth watch list". You will not be told again until it has been checked.')
    return title, "\n".join(lines)


TEST_ALERT = (
    "FlightSleuth test alert",
    "This is a test. No search was made and no price was fetched. A real alert names the route, the dates and "
    "the price, says why it fired, and opens the site to check it on when tapped.",
)
SENT_LATE = "Sent late: ntfy could not be reached when this price was fetched."


def checked(
    outcome: str,
    currency: str,
    *,
    price: float | None = None,
    confirmed: float | None = None,
    lowest: float | None = None,
    bar: float | None = None,
    reasons: list[str] | None = None,
    error: str = "",
) -> str:
    """What one check of a watch came to, in a sentence or two, for the log and for `watch list`."""
    listed = money(price, currency) if price is not None else ""
    held = money(confirmed, currency) if confirmed is not None else ""
    because = "; ".join(reasons or [])
    if outcome == "no fares":
        return "No fares were listed."
    if outcome == "quiet":
        return f"{listed} listed. No rule fired."
    if outcome == "repeat":
        shown = f"{held} confirmed" if held else f"{listed} listed"
        return f"{shown}. Already alerted at {money(lowest, currency)}; a new alert needs {money(bar, currency)} or less."
    if outcome == "no return flights":
        return (
            f"{listed} listed ({because}), but its return flights could not be found, so no alert. "
            f"It will not be asked about again at that price."
        )
    if outcome == "known empty":
        return (
            f"{listed} listed, but every fare listed is one whose return flights could not be found before "
            f"at the same price, so none was asked about again."
        )
    if outcome == "not confirmed":
        return f"{listed} listed ({because}), but it could not be confirmed, so no alert: {error}."
    if outcome == "rose":
        return f"{listed} listed, but with its return flights it came to {held}, which fires no rule."
    if outcome == "alerted":
        return f"{held} confirmed. Alert sent: {because}."
    if outcome == "alert not sent":
        return f"{held} confirmed ({because}), but the alert did not go: {error}. The next run tries again."
    if outcome == "skipped":
        return f"Not checked: {error}. It stays due."
    return f"Check failed: {error}. It stays due."


def _rules(found: watch.Watch) -> str:
    currency = found.trip.currency
    rules = []
    if found.below is not None:
        rules.append(f"below {money(found.below, currency)}")
    if found.drop_percent:
        rules.append(f"{found.drop_percent:g}% under its {found.median_days}-day median")
    if found.typical:
        rules.append("below Google's typical range")
    return ", or ".join(rules)


def watch_lines(standing: watch.Standing, now: datetime, resend_hours: int) -> list[str]:
    """One watch as `watch list` shows it: what it is, its rules, and where it stands."""
    found, trip = standing.watch, standing.watch.trip
    currency = trip.currency
    people = f"{trip.adults} adult{'s' if trip.adults != 1 else ''}"
    kind = "round trip" if trip.return_date else "one way"
    limit = "" if trip.stops == "any" else f", {_most_stops(trip)}"
    lines = [f"{found.id}. {watch_title(trip)}, {kind}, {people}, {trip.cabin}{limit}"]
    lines.append(f"   Alert when it is {_rules(found)}. Checked every {_count(found.every_days, 'day')}.")
    last = standing.last
    if last is None:
        lines.append("   Last check: none yet.")
    else:
        lines.append(f"   Last check: {fetched(datetime.fromisoformat(last['checked_at']), True)}. {last['detail']}")
    priced = standing.priced
    if priced is None:
        lines.append("   Last price: none yet.")
    else:
        amount = priced["confirmed_price"] if priced["confirmed_price"] is not None else priced["price"]
        state = "confirmed" if priced["confirmed_price"] is not None else "listed"
        when = fetched(datetime.fromisoformat(priced["fetched_at"]), True) if priced["fetched_at"] else "time unknown"
        lines.append(f"   Last price: {money(amount, currency)} {state}, fetched {when}.")
    if standing.unsent is not None:
        fresh = standing.unsent_since and (now - standing.unsent_since).total_seconds() <= resend_hours * 3600
        about = f"An alert about {money(standing.unsent['price'], currency)}"
        lines.append(f"   {about} has not reached ntfy. The next run tries again." if fresh else f"   {about} never reached ntfy.")
    if standing.ended:
        ending = "Removed." if standing.ended == "removed" else "Retired: its departure date has passed."
        lines.append(f"   {ending} Searches used: {standing.searches}.")
    else:
        due = "now" if standing.due <= now.date() else day(standing.due)
        lines.append(f"   Next due: {due}. Searches used: {standing.searches}.")
    return lines


def _span_lines(span: watch.Span, have: str) -> list[str]:
    lines = [
        f"  {watch_title(found.trip)}: {_count(count, 'check')}, one every {_count(found.every_days, 'day')}"
        for found, count in span.checks
    ]
    cost = span.count * span.per_check
    each = "1 search" if span.per_check == 1 else f"{span.per_check} searches"
    lines.append(
        f"  {_count(span.count, 'check')} at {each} each, and {span.hold} held back for each of "
        f"{_count(span.watching, 'watch')} to confirm an alert: {cost} + {span.held} = {span.needed}"
    )
    verdict = f"so {span.spare} would be spare" if span.spare >= 0 else f"so that is {-span.spare} too many"
    return [*lines, f"  {have}, {verdict}."]


def budget_lines(budget: watch.Budget) -> list[str]:
    """The sums behind accepting or refusing a watch, written out so the owner can check them."""
    now, after = budget.now, budget.after
    if budget.reset_known:
        first = f"Until the searches reset on {day(now.end)}:"
    else:
        first = "In the next 30 days (the account did not say when its searches reset):"
    room = budget.ceiling - budget.used
    if budget.left <= room:
        have = f"{_count(budget.left, 'search')} {'is' if budget.left == 1 else 'are'} left"
    else:
        have = (
            f"Watches have used {budget.used} of the {budget.ceiling} searches a month they may, "
            f"which leaves {max(room, 0)}"
        )
    if budget.per_month is None:
        monthly = f"Watches may use {budget.ceiling} searches a month"
    elif budget.ceiling <= budget.per_month:
        monthly = f"Watches may use {budget.ceiling} of the plan's {budget.per_month} searches a month"
    else:
        monthly = f"The plan gives {budget.per_month} searches a month"
    return [
        first,
        *_span_lines(now, have),
        f"In the month after that, to {day(after.end)}:",
        *_span_lines(after, monthly),
    ]


def place_cells(place: Destination, currency: str) -> list[str]:
    if place.start and place.end:
        when = f"{day(place.start)} - {day(place.end)}"
    else:
        when = f"from {day(place.start)}" if place.start else ""
    return [
        money(place.price, currency) if place.price is not None else "-",
        ", ".join(part for part in (place.name, place.country) if part),
        place.airport,
        when,
        stops(place.stops) if place.stops is not None else "",
        duration(place.duration_min) if place.duration_min else "",
        place.airline,
        fetched(place.fetched_at, place.verified_live),
    ]


def render_places(title: str, places: tuple[Destination, ...], currency: str, *, top: int = 20) -> str:
    """An explore search, as plain ASCII. Its prices are suggestions until a dated search confirms them."""
    if not places:
        return f"{title}\n\nNo destinations found. Try another month, trip length or region."
    rows = [place_cells(place, currency) for place in places[:top]]
    note = "These are the prices explore suggests. Search the dates to confirm one before relying on it."
    # A search for one place comes with one link, to those dates on Google Flights.
    links = dict.fromkeys(place.url for place in places[:top] if place.url.startswith("https://"))
    return "\n".join([title, "", *table(PLACE_HEADERS, rows), "", note, *(f"Check these dates: {url}" for url in links)])
