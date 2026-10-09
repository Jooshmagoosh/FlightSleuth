"""What every price source has to say about itself, and do."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from ..models import Destination, Explore, Query, SearchResult


class Capability(Enum):
    LIVE_SEARCH = "live_search"  # priced flights for exact dates, fetched now
    DATE_CALENDAR = "date_calendar"  # the cheapest price for each date across a range
    EXPLORE = "explore"  # destinations or dates for a loosely stated trip
    RETURN_FLIGHTS = "return_flights"  # the return legs that go with a chosen outbound
    BOOKING_OPTIONS = "booking_options"  # who sells an itinerary, and for how much
    DEEP_LINK = "deep_link"  # a link that opens the same search on the source's own site
    MARKETS = "markets"  # the same search priced as another country's customers see it


class DataClass(Enum):
    """How a source may be used. A hosted product may only use the first two."""

    VENDOR = "vendor"  # a commercial API, paid for
    SANCTIONED = "sanctioned"  # offered by its owner for programs to call
    PERSONAL_ONLY = "personal_only"  # only ever from the user's own machine


@dataclass(frozen=True)
class Allowance:
    """What a metered source's account says right now. Asked for afresh each time; nothing here is assumed."""

    left: int  # calls left before the count starts again
    per_month: int | None  # what the plan gives each month, if the account says
    resets_on: date | None  # the day the count starts again, if the account says


class Provider:
    """One price source. Subclasses set the four labels and implement what their capabilities need."""

    name: str
    capabilities: frozenset[Capability]
    cost_per_call: float  # in the source's own credits; 0 for a free source
    data_class: DataClass
    default_market = ""  # the country it prices for when a query names none, if that is known

    def cache_key(self, query: Query | Explore) -> str:
        """Stable id for one search at this source. Never includes a secret."""
        blob = json.dumps([self.name, type(query).__name__, dataclasses.astuple(query)], default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def search(self, query: Query | Explore) -> dict:
        """Fetch the raw reply for one call. Needed for LIVE_SEARCH.

        A query carrying a `departure_token` asks for return flights (RETURN_FLIGHTS), one carrying
        a `booking_token` for sellers (BOOKING_OPTIONS), one naming a `market` for that country's
        prices (MARKETS), and an `Explore` for destinations (EXPLORE). scan only sends a source
        what its capabilities cover.
        """
        raise NotImplementedError

    def parse(self, query: Query, raw: dict) -> SearchResult:
        """Turn a raw reply, fresh or saved, into offers. Needed for LIVE_SEARCH.

        Leave `source`, `fetched_at` and `verified_live` alone: scan stamps them on every offer.
        """
        raise NotImplementedError

    def destinations(self, trip: Explore, raw: dict) -> tuple[Destination, ...]:
        """Turn an explore reply into destinations. Needed for EXPLORE. scan stamps these too."""
        raise NotImplementedError

    def fetched_at(self, raw: dict) -> datetime | None:
        """When the source itself fetched this reply, in UTC, if the reply says. Else None."""
        return None

    def budget_problem(self, calls: int) -> str | None:
        """Why this source cannot afford `calls` more calls right now, if it can't."""
        return None

    def credits_left(self) -> int | None:
        """Credits left as of the last budget check; None if unknown or unmetered."""
        return None

    def allowance(self) -> Allowance | None:
        """The account's count of calls left, its monthly total and its reset day. None for an unmetered source."""
        return None

    def quota_lines(self) -> list[str]:
        """The account's plan and usage, worded for the quota command."""
        return []
