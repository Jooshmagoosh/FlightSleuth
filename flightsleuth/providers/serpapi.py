"""SerpApi's Google Flights engine, as a provider."""
from __future__ import annotations

from .. import config, serpapi
from ..models import Query, SearchResult
from .base import Capability, DataClass, Provider


class SerpApiProvider(Provider):
    name = "serpapi"
    # What this adapter does today. The API can also return return flights and booking options.
    capabilities = frozenset({Capability.LIVE_SEARCH, Capability.DEEP_LINK})
    cost_per_call = 1  # one search credit per request, even when no flights come back
    data_class = DataClass.VENDOR
    client_class = serpapi.SerpApi

    def __init__(self):
        self._client = None
        self._account: dict | None = None

    def _connect(self):
        # The key is only needed once something is fetched, so planning and dry runs work without one.
        if self._client is None:
            self._client = self.client_class(config.load_api_key())
        return self._client

    def cache_key(self, query: Query) -> str:
        return serpapi.cache_key(query)

    def search(self, query: Query) -> dict:
        return self._connect().search(query)

    def parse(self, query: Query, raw: dict) -> SearchResult:
        return serpapi.parse(query, raw)

    def budget_problem(self, calls: int) -> str | None:
        self._account = account = self._connect().account()
        month_left = account.get("total_searches_left")
        if month_left is not None and calls > month_left:
            return f"this scan needs {calls} live searches but only {month_left} are left this month"
        hourly_cap = account.get("account_rate_limit_per_hour")
        if hourly_cap is not None:
            hour_left = hourly_cap - account.get("this_hour_searches", 0)
            if calls > hour_left:
                return f"this scan needs {calls} live searches but only {hour_left} more are allowed this hour"
        return None

    def credits_left(self) -> int | None:
        return (self._account or {}).get("total_searches_left")

    def quota_lines(self) -> list[str]:
        account = self._connect().account()
        return [
            f"Plan: {account.get('plan_name')}",
            f"This month: {account.get('total_searches_left')} of {account.get('searches_per_month')} searches left",
            f"This hour: {account.get('this_hour_searches')} used of "
            f"{account.get('account_rate_limit_per_hour')} allowed",
        ]
