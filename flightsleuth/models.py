from __future__ import annotations

from dataclasses import dataclass
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
    seller: str | None = None  # who sells it at this price, once that has been looked up
    source: str = ""  # the provider it came from
    fetched_at: datetime | None = None  # when the source was asked, in UTC
    verified_live: bool = False  # fetched during this run rather than replayed from a saved reply
    # Catches. Each is True only when the source says so; False can also mean "not stated".
    self_transfer: bool = False
    hidden_city: bool = False
    separate_tickets: bool = False
    basic_economy: bool = False

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

    @property
    def cheapest(self) -> Offer | None:
        return self.offers[0] if self.offers else None
