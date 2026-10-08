import io
import json
import unittest
import urllib.error
import urllib.parse
from datetime import date, datetime
from pathlib import Path

from flightsleuth.models import Query
from flightsleuth.serpapi import RateLimited, SerpApi, SerpApiError, cache_key, parse, search_params

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "roundtrip.json").read_text(encoding="utf-8"))
TRIP = Query("JFK,EWR", "LHR", date(2030, 3, 12), date(2030, 3, 19))


class FakeOpener:
    """Stands in for urlopen: replays queued replies and records the URLs asked for."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.urls = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())


def http_error(code, body=None):
    payload = io.BytesIO(json.dumps(body).encode() if body is not None else b"")
    return urllib.error.HTTPError("https://serpapi.com/search.json", code, "status text", {}, payload)


def client(*replies):
    opener = FakeOpener(*replies)
    waits = []
    return SerpApi("secret-key", opener=opener, sleep=waits.append), opener, waits


class SearchParamsTest(unittest.TestCase):
    def test_round_trip(self):
        params = search_params(TRIP)
        self.assertEqual(params["engine"], "google_flights")
        self.assertEqual(params["departure_id"], "JFK,EWR")
        self.assertEqual(params["outbound_date"], "2030-03-12")
        self.assertEqual(params["return_date"], "2030-03-19")
        self.assertEqual(params["type"], "1")
        self.assertEqual(params["sort_by"], "2")

    def test_one_way_sends_no_return_date(self):
        params = search_params(Query("JFK", "LHR", date(2030, 3, 12)))
        self.assertEqual(params["type"], "2")
        self.assertNotIn("return_date", params)

    def test_filters_use_serpapi_codes(self):
        params = search_params(Query("JFK", "LHR", date(2030, 3, 12), cabin="business", stops="nonstop", deep=True))
        self.assertEqual(params["travel_class"], "3")
        self.assertEqual(params["stops"], "1")
        self.assertEqual(params["deep_search"], "true")

    def test_cache_key_changes_with_the_search_and_never_holds_the_key(self):
        self.assertNotIn("api_key", search_params(TRIP))
        self.assertEqual(cache_key(TRIP), cache_key(Query("JFK,EWR", "LHR", date(2030, 3, 12), date(2030, 3, 19))))
        self.assertNotEqual(cache_key(TRIP), cache_key(Query("JFK,EWR", "LHR", date(2030, 3, 13), date(2030, 3, 19))))


class ParseTest(unittest.TestCase):
    def test_offers_come_back_cheapest_first(self):
        result = parse(TRIP, FIXTURE)
        self.assertEqual([offer.price for offer in result.offers], [438, 512])

    def test_connecting_itinerary_is_summarised_end_to_end(self):
        cheapest = parse(TRIP, FIXTURE).cheapest
        self.assertEqual((cheapest.origin, cheapest.destination), ("JFK", "LHR"))
        self.assertEqual(cheapest.stops, 1)
        self.assertEqual(cheapest.airlines, ("Icelandair",))
        self.assertEqual(cheapest.depart_at, datetime(2030, 3, 12, 20, 35))
        self.assertEqual(cheapest.arrive_at, datetime(2030, 3, 13, 11, 45))
        self.assertEqual(cheapest.duration_min, 610)

    def test_options_without_a_price_are_left_out(self):
        airlines = {offer.airlines for offer in parse(TRIP, FIXTURE).offers}
        self.assertNotIn(("Virgin Atlantic",), airlines)

    def test_insights_and_link(self):
        result = parse(TRIP, FIXTURE)
        self.assertEqual(result.insights.level, "low")
        self.assertEqual(result.insights.typical_range, (520, 760))
        self.assertTrue(result.url.startswith("https://www.google.com/travel/flights"))

    def test_empty_search_is_an_empty_result(self):
        raw = {
            "search_metadata": {"status": "Success"},
            "error": "Google Flights hasn't returned any results for this query.",
        }
        result = parse(TRIP, raw)
        self.assertEqual(result.offers, ())
        self.assertIsNone(result.cheapest)

    def test_unfamiliar_shape_is_reported_not_crashed_on(self):
        with self.assertRaises(SerpApiError):
            parse(TRIP, {"other_flights": [{"price": 400, "flights": [{"airline": "X"}]}]})


class HttpTest(unittest.TestCase):
    def test_search_sends_the_key_and_returns_the_body(self):
        api, opener, _ = client(FIXTURE)
        self.assertEqual(api.search(TRIP), FIXTURE)
        sent = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.urls[0]).query)
        self.assertEqual(sent["api_key"], ["secret-key"])
        self.assertEqual(sent["departure_id"], ["JFK,EWR"])

    def test_server_errors_are_retried_with_growing_waits(self):
        api, opener, waits = client(http_error(503), http_error(500), FIXTURE)
        self.assertEqual(api.search(TRIP), FIXTURE)
        self.assertEqual(len(opener.urls), 3)
        self.assertEqual(len(waits), 2)
        self.assertLess(waits[0], waits[1])

    def test_gives_up_after_the_retries_run_out(self):
        api, opener, _ = client(http_error(503), http_error(503), http_error(503))
        with self.assertRaises(SerpApiError):
            api.search(TRIP)
        self.assertEqual(len(opener.urls), 3)

    def test_throttling_is_never_retried(self):
        api, opener, waits = client(http_error(429, {"error": "Your account has run out of searches."}))
        with self.assertRaises(RateLimited) as caught:
            api.search(TRIP)
        self.assertEqual(str(caught.exception), "Your account has run out of searches.")
        self.assertEqual(len(opener.urls), 1)
        self.assertEqual(waits, [])

    def test_bad_key_is_reported_without_retrying_or_leaking_it(self):
        api, opener, _ = client(http_error(401, {"error": "Invalid API key."}))
        with self.assertRaises(SerpApiError) as caught:
            api.account()
        self.assertEqual(str(caught.exception), "Invalid API key.")
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertEqual(len(opener.urls), 1)

    def test_network_failure_is_retried_then_reported(self):
        down = urllib.error.URLError("no route to host")
        api, opener, _ = client(down, down, down)
        with self.assertRaises(SerpApiError) as caught:
            api.search(TRIP)
        self.assertIn("no route to host", str(caught.exception))
        self.assertEqual(len(opener.urls), 3)

    def test_failed_search_is_an_error_but_an_empty_one_is_not(self):
        failed = {"search_metadata": {"status": "Error"}, "error": "Search timed out."}
        empty = {"search_metadata": {"status": "Success"}, "error": "Google Flights hasn't returned any results."}
        api, _, _ = client(failed, empty)
        with self.assertRaises(SerpApiError):
            api.search(TRIP)
        self.assertEqual(api.search(TRIP), empty)


if __name__ == "__main__":
    unittest.main()
