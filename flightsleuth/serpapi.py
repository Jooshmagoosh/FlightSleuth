"""SerpApi's Google Flights engine: build the request, fetch it politely, parse the reply."""
from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .errors import UserError
from .models import Insights, Offer, Query, SearchResult, Segment

SEARCH_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"
USER_AGENT = "flightsleuth/0.1"
TIMEOUT_S = 90
RETRIES = 2

CABINS = {"economy": "1", "premium": "2", "business": "3", "first": "4"}
# User-facing names for the most stops allowed, mapped to SerpApi's codes.
STOPS = {"any": "0", "nonstop": "1", "1": "2", "2": "3"}


class SerpApiError(UserError):
    pass


class RateLimited(SerpApiError):
    """HTTP 429: out of searches, or over the hourly throughput cap."""


def search_params(query: Query) -> dict[str, str]:
    params = {
        "engine": "google_flights",
        "departure_id": query.origin,
        "arrival_id": query.destination,
        "outbound_date": query.depart_date.isoformat(),
        "type": "2" if query.return_date is None else "1",
        "travel_class": CABINS[query.cabin],
        "stops": STOPS[query.stops],
        "adults": str(query.adults),
        "currency": query.currency,
        "hl": "en",
        "sort_by": "2",  # price
        "show_hidden": "true",
    }
    if query.return_date is not None:
        params["return_date"] = query.return_date.isoformat()
    if query.deep:
        params["deep_search"] = "true"
    return params


def cache_key(query: Query) -> str:
    """Stable id for a search. Built from the request without the API key."""
    blob = json.dumps(search_params(query), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class SerpApi:
    def __init__(self, api_key: str, *, opener=urllib.request.urlopen, sleep=time.sleep):
        self._api_key = api_key
        self._open = opener
        self._sleep = sleep

    def account(self) -> dict:
        """Plan and usage numbers. Free: it does not count against the quota."""
        return self._get(ACCOUNT_URL, {})

    def search(self, query: Query) -> dict:
        raw = self._get(SEARCH_URL, search_params(query))
        # A search that found no flights is still a success, with an "error" note; only a
        # search SerpApi could not complete has this status.
        if (raw.get("search_metadata") or {}).get("status") == "Error":
            raise SerpApiError(raw.get("error") or "SerpApi could not complete the search")
        return raw

    def _get(self, url: str, params: dict[str, str]) -> dict:
        request = urllib.request.Request(
            url + "?" + urllib.parse.urlencode({**params, "api_key": self._api_key}),
            headers={"User-Agent": USER_AGENT},
        )
        for attempt in range(RETRIES + 1):
            # `from None` below keeps the request URL, which carries the key, out of tracebacks.
            try:
                with self._open(request, timeout=TIMEOUT_S) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                message = _error_message(e)
                if e.code == 429:
                    # Retrying into a throttle only digs the hole deeper; let the caller stop.
                    raise RateLimited(message) from None
                if e.code < 500:
                    raise SerpApiError(message) from None
                failure = message
            except (urllib.error.URLError, TimeoutError) as e:
                failure = f"network error: {getattr(e, 'reason', e)}"
            except ValueError:
                failure = "SerpApi sent a response that is not JSON"
            if attempt < RETRIES:
                self._sleep(2 ** (attempt + 1) + random.random())
        raise SerpApiError(failure) from None


def _error_message(e: urllib.error.HTTPError) -> str:
    try:
        return json.load(e)["error"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {e.code} {e.reason}"


def parse(query: Query, raw: dict) -> SearchResult:
    options = (raw.get("best_flights") or []) + (raw.get("other_flights") or [])
    try:
        offers = [_offer(query, option) for option in options if option.get("price") is not None]
        insights = _insights(raw.get("price_insights"))
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise SerpApiError(f"unexpected response shape from SerpApi ({e!r})") from None
    offers.sort(key=lambda o: (o.price, o.duration_min))
    return SearchResult(
        query=query,
        offers=tuple(offers),
        insights=insights,
        url=(raw.get("search_metadata") or {}).get("google_flights_url"),
    )


def fetched_at(raw: dict) -> datetime | None:
    """When SerpApi asked Google, in UTC. Earlier than now when SerpApi answers from its own cache."""
    stamp = (raw.get("search_metadata") or {}).get("processed_at")
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _offer(query: Query, option: dict) -> Offer:
    flights = option["flights"]
    if not flights:
        raise ValueError("an option with no flights")
    return Offer(
        query=query,
        price=option["price"],
        segments=tuple(_segment(flight) for flight in flights),
        duration_min=option.get("total_duration") or sum(flight.get("duration", 0) for flight in flights),
    )


def _segment(flight: dict) -> Segment:
    return Segment(
        carrier=flight["airline"],
        flight_number=flight.get("flight_number", ""),
        origin=flight["departure_airport"]["id"],
        destination=flight["arrival_airport"]["id"],
        depart_at=datetime.fromisoformat(flight["departure_airport"]["time"]),
        arrive_at=datetime.fromisoformat(flight["arrival_airport"]["time"]),
    )


def _insights(raw: dict | None) -> Insights | None:
    if not raw:
        return None
    typical = raw.get("typical_price_range")
    history = raw.get("price_history") or []
    return Insights(
        lowest_price=raw.get("lowest_price"),
        level=raw.get("price_level"),
        typical_range=tuple(typical) if typical and len(typical) == 2 else None,
        price_history=tuple((datetime.fromtimestamp(stamp, timezone.utc), price) for stamp, price in history),
    )
