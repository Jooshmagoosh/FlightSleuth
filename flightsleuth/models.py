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
class Offer:
    """A bookable option. For a round trip the price is the total and the flight is the outbound leg."""

    query: Query
    price: float
    airlines: tuple[str, ...]
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime
    stops: int
    duration_min: int


@dataclass(frozen=True)
class Insights:
    """Google's own read on whether the price is good for these dates."""

    lowest_price: float | None
    level: str | None
    typical_range: tuple[float, float] | None


@dataclass(frozen=True)
class SearchResult:
    query: Query
    offers: tuple[Offer, ...]  # cheapest first
    insights: Insights | None = None
    url: str | None = None

    @property
    def cheapest(self) -> Offer | None:
        return self.offers[0] if self.offers else None
