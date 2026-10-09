"""What every price source has to say about itself, and do."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from enum import Enum

from ..models import Query, SearchResult


class Capability(Enum):
    LIVE_SEARCH = "live_search"  # priced flights for exact dates, fetched now
    DATE_CALENDAR = "date_calendar"  # the cheapest price for each date across a range
    EXPLORE = "explore"  # destinations or dates for a loosely stated trip
    RETURN_FLIGHTS = "return_flights"  # the return legs that go with a chosen outbound
    BOOKING_OPTIONS = "booking_options"  # who sells an itinerary, and for how much
    DEEP_LINK = "deep_link"  # a link that opens the same search on the source's own site


class DataClass(Enum):
    """How a source may be used. A hosted product may only use the first two."""

    VENDOR = "vendor"  # a commercial API, paid for
    SANCTIONED = "sanctioned"  # offered by its owner for programs to call
    PERSONAL_ONLY = "personal_only"  # only ever from the user's own machine


class Provider:
    """One price source. Subclasses set the four labels and implement what their capabilities need."""

    name: str
    capabilities: frozenset[Capability]
    cost_per_call: float  # in the source's own credits; 0 for a free source
    data_class: DataClass

    def cache_key(self, query: Query) -> str:
        """Stable id for one search at this source. Never includes a secret."""
        blob = json.dumps([self.name, dataclasses.astuple(query)], default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def search(self, query: Query) -> dict:
        """Fetch the raw reply for one search. Needed for LIVE_SEARCH."""
        raise NotImplementedError

    def parse(self, query: Query, raw: dict) -> SearchResult:
        """Turn a raw reply, fresh or saved, into offers. Needed for LIVE_SEARCH."""
        raise NotImplementedError

    def budget_problem(self, calls: int) -> str | None:
        """Why this source cannot afford `calls` more calls right now, if it can't."""
        return None

    def credits_left(self) -> int | None:
        """Credits left as of the last budget check; None if unknown or unmetered."""
        return None

    def quota_lines(self) -> list[str]:
        """The account's plan and usage, worded for the quota command."""
        return []
