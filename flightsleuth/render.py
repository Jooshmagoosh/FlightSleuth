"""Turns search results into the rows and sentences that both front ends show."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import Insights, Offer, Query, SearchResult
from .planner import one_way_legs

NO_FARES = "No fares found. Try other dates, more airports, or fewer filters."


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
    text = f"{offer.depart_at:%H:%M} -> {offer.arrive_at:%H:%M}"
    days_later = (offer.arrive_at.date() - offer.depart_at.date()).days
    return f"{text} +{days_later}" if days_later else text


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows)]
    lines = [headers, ["-" * w for w in widths], *rows]
    return ["  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in lines]


def _dates(trip: Query) -> str:
    if trip.return_date is None:
        return day(trip.depart_date)
    return f"{day(trip.depart_date)} - {day(trip.return_date)}"


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

    @property
    def headline(self) -> str:
        if self.best is None:
            return NO_FARES
        price = money(self.best.price, self.currency)
        if self.best.split_wins:
            return f"Cheapest: {price} as two one-way tickets, {_dates(self.best.trip)}"
        kind = "round trip" if self.round_trip else "one way"
        return f"Cheapest: {price} {kind}, {_dates(self.best.trip)}"

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


def summarise(
    trips: list[Query], results: dict[Query, SearchResult], *, split: bool = False, top: int = 10
) -> Summary:
    first = trips[0]
    round_trip = first.return_date is not None
    kind = "round trip" if round_trip else "one way"
    people = f"{first.adults} adult{'s' if first.adults != 1 else ''}"

    rows = [_date_row(trip, results, split) for trip in trips]
    best = min((row for row in rows if row.price is not None), key=lambda row: row.price, default=None)
    offers = sorted(
        (offer for trip in trips for offer in results[trip].offers),
        key=lambda o: (o.price, o.duration_min),
    )[:top]

    insight, links = None, []
    if best and best.split_wins:
        # The round trip lost, so its price rating and link would describe the wrong ticket.
        for label, leg in zip(("out", "back"), one_way_legs(best.trip)):
            if results[leg].url:
                links.append((f"Check and book the flight {label}", results[leg].url))
    elif best:
        result = results[best.trip]
        insight = _insight(result.insights, first.currency)
        if result.url:
            links.append(("Check and book", result.url))

    return Summary(
        title=f"{first.origin} -> {first.destination}, {kind}, {people}, {first.cabin}",
        round_trip=round_trip,
        split=split,
        currency=first.currency,
        rows=rows,
        best=best,
        offers=offers,
        insight=insight,
        links=links,
    )


def render_text(summary: Summary) -> str:
    """Plain ASCII, so it survives whatever code page the console is using."""
    lines = [summary.title, ""]
    if summary.best is None:
        return "\n".join(lines + [NO_FARES])

    if len(summary.rows) > 1 or summary.split:
        body = [summary.date_cells(row) + ["<- cheapest" if row is summary.best else ""] for row in summary.rows]
        lines += table(summary.date_headers + [""], body) + [""]

    if summary.offers:
        note = " (price is the round-trip total, flight shown is the outbound)" if summary.round_trip else ""
        lines.append(f"Cheapest itineraries{note}:")
        lines += table(summary.offer_headers, [summary.offer_cells(offer) for offer in summary.offers]) + [""]

    lines.append(summary.headline)
    if summary.split_legs:
        legs = table(["", "Price", "Route", "Departs", "Stops", "Time", "Airline"], summary.split_legs)
        lines += ["  " + line for line in legs[2:]]
    if summary.insight:
        lines.append(summary.insight)
    lines += [f"{label}: {url}" for label, url in summary.links]
    return "\n".join(lines)
