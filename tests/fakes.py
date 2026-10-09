"""A stand-in for SerpApi, and the setup that keeps tests away from the real key and cache."""
import contextlib
import os
import tempfile
from unittest import mock

from flightsleuth import config, providers, scan
from flightsleuth.models import Explore
from flightsleuth.providers import SerpApiProvider
from flightsleuth.serpapi import RateLimited

# Round-trip fare by departure day of March 2030; the 11th is the cheap one.
ROUND_TRIP_FARES = {10: 640, 11: 455, 12: 520, 13: 590, 14: 700}
ONE_WAY_FARE = 200
NO_FLIGHTS = {
    "search_metadata": {"status": "Success"},
    "error": "Google Flights hasn't returned any results for this query.",
}


def fare(query):
    """The price of a search, which depends only on the query."""
    if FakeSerpApi.fare is not None:
        return FakeSerpApi.fare(query)
    return ONE_WAY_FARE if query.return_date is None else ROUND_TRIP_FARES.get(query.depart_date.day, 900)


def reply(query):
    """A SerpApi-shaped response whose price depends only on the query."""
    if isinstance(query, Explore):
        return FakeSerpApi.explore_reply
    if FakeSerpApi.rich:
        return rich_reply(query)
    price = fare(query)
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


def flight(airline, number, origin, destination, day, leaves="18:30", lands="23:10"):
    return {
        "departure_airport": {"id": origin, "time": f"{day} {leaves}"},
        "arrival_airport": {"id": destination, "time": f"{day} {lands}"},
        "airline": airline,
        "flight_number": number,
    }


def rich_reply(query):
    """A reply shaped like the recorded ones: flight numbers, tokens, countries, and follow-ups that work.

    A search lists three fares: Phantom Air undercutting by 40 when `FakeSerpApi.phantoms` says how
    many, then Testair at the fare, then Otherair 30 above it. A round-trip fare carries a
    `departure_token`, which leads to its return flights unless the fare is a phantom, and
    those carry the `booking_token` that leads to the sellers.
    """
    origin = query.origin.split(",")[0]
    destination = query.destination.split(",")[0]
    metadata = {"status": "Success", "google_flights_url": "https://www.google.com/travel/flights?tfs=X"}
    if query.booking_token:
        _, price, airline = query.booking_token.split(":")
        price = int(price)
        options = [
            {"together": {"book_with": airline, "airline": True, "price": price + (103 if FakeSerpApi.separate else 0)}},
            {"together": {"book_with": "Cheapo Travel", "price": price + 25}},
        ]
        if FakeSerpApi.separate:
            options.insert(
                0,
                {
                    "separate_tickets": True,
                    "together": {"book_with": f"{airline}, Otherair", "airline": True, "price": price},
                    "departing": {"book_with": airline, "airline": True, "price": price - 200},
                    "returning": {"book_with": "Otherair", "airline": True, "price": 200},
                },
            )
        insights = {"lowest_price": price, "price_level": "typical", "typical_price_range": [500, 800]}
        return {"search_metadata": metadata, "booking_options": options, "price_insights": insights}
    if query.departure_token:
        _, price, airline = query.departure_token.split(":")
        if airline == "Phantom Air":
            return NO_FLIGHTS
        price = int(price) + FakeSerpApi.return_surcharge
        code = "TA" if airline == "Testair" else "OA"
        return {
            "search_metadata": metadata,
            "other_flights": [
                {
                    "flights": [flight(airline, f"{code} 2", destination, origin, query.return_date, "09:00", "12:00")],
                    "total_duration": 480,
                    "price": price + extra,
                    "booking_token": f"book:{price + extra}:{airline}",
                }
                for extra in (60, 0)
            ],
        }
    price = fare(query) - FakeSerpApi.market_savings.get(query.market, 0) + 35 * query.bags
    fares = [("Testair", "TA 1", price), ("Otherair", "OA 5", price + 30)]
    fares = [("Phantom Air", f"PH {n}", price - 40 + n) for n in range(FakeSerpApi.phantoms)] + fares
    token = "booking_token" if query.return_date is None else "departure_token"
    kind = "book" if query.return_date is None else "dep"
    body = {
        "search_metadata": metadata,
        "other_flights": [
            {
                "flights": [flight(airline, number, origin, destination, query.depart_date)],
                "total_duration": 430,
                "price": amount,
                token: f"{kind}:{amount}:{airline}",
            }
            for airline, number, amount in fares
        ],
        "airports": [
            {
                "departure": [{"airport": {"id": origin}, "country_code": FakeSerpApi.countries[0]}],
                "arrival": [{"airport": {"id": destination}, "country_code": FakeSerpApi.countries[1]}],
            }
        ],
    }
    if not query.by_price:
        # Google's own order drops the phantom fares and adds its rating, as the recorded reply did.
        body["other_flights"] = body["other_flights"][FakeSerpApi.phantoms :]
        body["price_insights"] = {"lowest_price": price, "price_level": "typical", "typical_price_range": [500, 800]}
    return body


class FakeSerpApi:
    """Replaces the real client: counts searches and can run out of quota part-way."""

    searches = []
    account_reply = {}
    fail_after = None
    fare = None  # a function of the query, to replace the March 2030 fares
    rich = False  # reply with tokens, flight numbers and countries, as real replies do
    phantoms = 0  # in rich replies: cheaper fares, listed first, whose return flights do not exist
    separate = False  # in rich replies: the cheapest seller sells two separate tickets
    return_surcharge = 0  # in rich replies: how much dearer the return lookup makes the fare
    market_savings = {}  # in rich replies: how much cheaper each other market is
    countries = ("US", "GB")
    explore_reply = {"search_metadata": {"status": "Success"}, "destinations": []}

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
    FakeSerpApi.fare = None
    FakeSerpApi.rich = False
    FakeSerpApi.phantoms = 0
    FakeSerpApi.separate = False
    FakeSerpApi.return_surcharge = 0
    FakeSerpApi.market_savings = {}
    FakeSerpApi.countries = ("US", "GB")
    FakeSerpApi.explore_reply = {"search_metadata": {"status": "Success"}, "destinations": []}
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
