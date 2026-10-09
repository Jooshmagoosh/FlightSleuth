"""A stand-in for SerpApi, and the setup that keeps tests away from the real key and cache."""
import contextlib
import os
import tempfile
from unittest import mock

from flightsleuth import config, providers, scan
from flightsleuth.providers import SerpApiProvider
from flightsleuth.serpapi import RateLimited

# Round-trip fare by departure day of March 2030; the 11th is the cheap one.
ROUND_TRIP_FARES = {10: 640, 11: 455, 12: 520, 13: 590, 14: 700}
ONE_WAY_FARE = 200


def reply(query):
    """A SerpApi-shaped response whose price depends only on the query."""
    price = ONE_WAY_FARE if query.return_date is None else ROUND_TRIP_FARES.get(query.depart_date.day, 900)
    origin = query.origin.split(",")[0]
    destination = query.destination.split(",")[0]
    return {
        "search_metadata": {"status": "Success", "google_flights_url": "https://www.google.com/travel/flights?tfs=X"},
        "other_flights": [
            {
                "flights": [
                    {
                        "departure_airport": {"id": origin, "time": f"{query.depart_date} 18:30"},
                        "arrival_airport": {"id": destination, "time": f"{query.depart_date} 23:10"},
                        "airline": "Testair",
                    }
                ],
                "total_duration": 430,
                "price": price,
            }
        ],
        "price_insights": {"lowest_price": price, "price_level": "low", "typical_price_range": [500, 800]},
    }


class FakeSerpApi:
    """Replaces the real client: counts searches and can run out of quota part-way."""

    searches = []
    account_reply = {}
    fail_after = None

    def __init__(self, api_key):
        self.api_key = api_key

    def account(self):
        return dict(FakeSerpApi.account_reply)

    def search(self, query):
        if FakeSerpApi.fail_after is not None and len(FakeSerpApi.searches) >= FakeSerpApi.fail_after:
            raise RateLimited("Your account has run out of searches.")
        FakeSerpApi.searches.append(query)
        return reply(query)


class FakeProvider(SerpApiProvider):
    """The real SerpApi provider with the fake client behind it."""

    client_class = FakeSerpApi


def install(test):
    """Give one test the fake provider, a throwaway home folder and a test key. Returns the folder."""
    FakeSerpApi.searches = []
    FakeSerpApi.fail_after = None
    FakeSerpApi.account_reply = {
        "total_searches_left": 250,
        "account_rate_limit_per_hour": 50,
        "this_hour_searches": 0,
    }
    home = test.enterContext(tempfile.TemporaryDirectory())
    test.enterContext(mock.patch.dict(os.environ, {config.HOME_VAR: home, config.KEY_VAR: "test-key"}))
    providers.register(FakeProvider)  # takes the real one's place under the same name
    test.addCleanup(providers.register, SerpApiProvider)
    # One at a time, so "fails after N searches" means exactly N.
    test.enterContext(mock.patch.object(scan, "WORKERS", 1))
    # Run from the throwaway folder: a real .env in the project must never be read or overwritten.
    test.enterContext(contextlib.chdir(home))
    return home
