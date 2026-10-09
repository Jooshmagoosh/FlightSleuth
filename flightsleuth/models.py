from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime


@dataclass(frozen=True)
class Query:
    """One priced search. Airports are comma-separated IATA codes; no return date means one way."""

    origin: str
    destination: str
    depart_date: date
    return_date: date | None = None
    adults: int = 1
    cabin: str = "economy"
    stops: str = "any"
    currency: str = "USD"
    deep: bool = False
    market: str = ""  # the country whose prices to ask for, such as "gb"; "" leaves it to the source
    bags: int = 0  # carry-on bags the price has to include
    by_price: bool = True  # cheapest first; False is the source's own order
    departure_token: str = ""  # set to ask for the return flights that go with one outbound
    booking_token: str = ""  # set to ask who sells one itinerary

    @property
    def endpoint(self) -> str:
        """What this asks a source for, as the call log names it."""
        if self.booking_token:
            return "booking_options"
        return "return_flights" if self.departure_token else "search"

    @property
    def plain(self) -> bool:
        """An ordinary search, whose cheapest fare can stand in the price history beside any other."""
        return self.endpoint == "search" and not self.market and not self.bags and self.by_price

    def follow(self, *, departure_token: str = "", booking_token: str = "") -> Query:
        """The same search, narrowed to one outbound's return flights or one itinerary's sellers."""
        return replace(self, departure_token=departure_token, booking_token=booking_token)


@dataclass(frozen=True)
class Explore:
    """A loosely stated trip: where from, roughly when, and anywhere, one region or one place."""

    origin: str
    destination: str = ""  # one airport, or "" for anywhere
    area: str = ""  # a region or country as Google names it, such as "/m/02j9z" for Europe
    month: int = 0  # 1 to 12, or 0 for any time in the next six months
    duration: str = "week"  # "weekend", "week" or "two weeks"
    adults: int = 1
    cabin: str = "economy"
    stops: str = "any"
    currency: str = "USD"
    market: str = ""
    bags: int = 0

    endpoint = "explore"
    plain = False


@dataclass(frozen=True)
class Segment:
    """One flight. Times are local to each airport, as a ticket prints them."""

    carrier: str  # the airline's name, as the source spells it
    flight_number: str  # the carrier's code and the number, such as "UA 14"
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime


def itinerary_key(segments: tuple[Segment, ...]) -> str:
    """Identifies a run of flights, so the same flights found at two sources match.

    Built from each flight number, which starts with the carrier's code, and its departure
    time. The airline's name is left out because sources spell it differently. A codeshare
    sold under another carrier's number is a different key.
    """
    return ">".join(
        f"{''.join(segment.flight_number.split()).upper()}@{segment.depart_at:%Y-%m-%dT%H:%M}" for segment in segments
    )


@dataclass(frozen=True)
class Offer:
    """A priced itinerary. For a round trip the price is the total for both directions."""

    query: Query
    price: float
    segments: tuple[Segment, ...]  # the outbound flights, or the only flights of a one-way
    duration_min: int  # outbound, door to door, layovers included
    return_segments: tuple[Segment, ...] | None = None  # None until the return flights are known
    return_duration_min: int | None = None
    seller: str | None = None  # who sells it at this price, once that has been looked up
    sold_by_airline: bool | None = None  # False for an agency; None until the seller is known
    source: str = ""  # the provider it came from
    fetched_at: datetime | None = None  # when the source was asked, in UTC
    verified_live: bool = False  # fetched during this run rather than replayed from a saved reply
    # Catches. Each is True only when the source says so; False can also mean "not stated".
    self_transfer: bool = False
    hidden_city: bool = False
    separate_tickets: bool = False
    basic_economy: bool = False
    # What the source hands back for looking closer. Good for a short while only.
    departure_token: str = ""
    booking_token: str = ""

    @property
    def airlines(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(segment.carrier for segment in self.segments))

    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination

    @property
    def depart_at(self) -> datetime:
        return self.segments[0].depart_at

    @property
    def arrive_at(self) -> datetime:
        return self.segments[-1].arrive_at

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def itinerary_key(self) -> str:
        """The outbound flights, plus the return flights once they are known."""
        key = itinerary_key(self.segments)
        if self.return_segments:
            key += "|" + itinerary_key(self.return_segments)
        return key

    @property
    def outbound_key(self) -> str:
        return itinerary_key(self.segments)

    @property
    def longest_layover_min(self) -> int:
        """The longest wait between flights, either way. Both times are local to the same airport."""
        waits = [
            round((later.depart_at - earlier.arrive_at).total_seconds() / 60)
            for leg in (self.segments, self.return_segments or ())
            for earlier, later in zip(leg, leg[1:])
        ]
        return max(waits, default=0)


@dataclass(frozen=True)
class Seller:
    """One way of buying an itinerary. The name is plain text, as the source prints it."""

    name: str
    price: float
    airline: bool  # the airline itself rather than an agency
    separate_tickets: bool = False
    parts: tuple[tuple[str, float], ...] = ()  # (seller, price) of each ticket, when sold separately


@dataclass(frozen=True)
class Alternative:
    """Another way to fly the same dates, set against the fare that was confirmed."""

    kind: str  # "seller", "market", "split" or "source"
    name: str  # the seller, the market's country code or the source; "" for a split
    price: float
    offers: tuple[Offer, ...]  # one itinerary, or the two one-ways of a split
    same_flights: bool | None  # None when there is no confirmed fare to compare with
    parts: tuple[tuple[str, float], ...] = ()  # (seller, price) of each ticket, when a seller splits them


@dataclass(frozen=True)
class Check:
    """What looking closer at the best fare of one date pair found."""

    trip: Query
    confirmed: Offer | None  # the cheapest fare that held up; for a round trip, with its return flights
    failed: tuple[Offer, ...] = ()  # listed fares whose return flights could not be found
    unchecked: tuple[Offer, ...] = ()  # listed fares cheaper than the confirmed one that were not looked up
    alternatives: tuple[Alternative, ...] = ()  # cheapest first
    stale: bool = False  # the listing was too old to follow up and there was no budget to refresh it


@dataclass(frozen=True)
class Destination:
    """One place an explore search suggests, with the dates it priced."""

    name: str
    country: str
    airport: str
    price: float | None
    start: date | None
    end: date | None
    airline: str = ""
    stops: int | None = None
    duration_min: int | None = None
    source: str = ""
    fetched_at: datetime | None = None
    verified_live: bool = False


@dataclass(frozen=True)
class Insights:
    """Google's own read on whether the price is good for these dates."""

    lowest_price: float | None
    level: str | None
    typical_range: tuple[float, float] | None
    price_history: tuple[tuple[datetime, float], ...] = ()  # (when, lowest price then), oldest first


@dataclass(frozen=True)
class SearchResult:
    query: Query
    offers: tuple[Offer, ...]  # cheapest first
    insights: Insights | None = None
    url: str | None = None
    sellers: tuple[Seller, ...] = ()  # filled by a booking-options reply, cheapest first
    origin_country: str = ""  # two-letter codes, when the source names the countries
    destination_country: str = ""

    @property
    def cheapest(self) -> Offer | None:
        return self.offers[0] if self.offers else None
