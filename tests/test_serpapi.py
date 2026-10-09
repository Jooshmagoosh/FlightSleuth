import io
import json
import unittest
import urllib.error
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

from flightsleuth.models import Query
from flightsleuth.serpapi import RateLimited, SerpApi, SerpApiError, cache_key, fetched_at, parse, search_params

FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def options(reply):
    return (reply.get("best_flights") or []) + (reply.get("other_flights") or [])


# Hand-written from the documentation. The recorded_* tests below use real replies.
FIXTURE = recorded("synthetic_roundtrip.json")
TRIP = Query("JFK,EWR", "LHR", date(2030, 3, 12), date(2030, 3, 19))
REAL_TRIP = Query("JFK,EWR", "LHR", date(2026, 12, 8), date(2026, 12, 15))
REAL_ONE_WAY = Query("LAX", "JFK", date(2026, 12, 8))


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

    def test_price_history_is_kept(self):
        history = parse(TRIP, FIXTURE).insights.price_history
        self.assertEqual(
            history,
            ((datetime(2030, 1, 1, tzinfo=timezone.utc), 610), (datetime(2030, 1, 2, tzinfo=timezone.utc), 438)),
        )

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


class RecordedReplyTest(unittest.TestCase):
    """What real SerpApi replies look like. See tests/fixtures/README.md for how they were recorded."""

    def test_real_request_matches_what_the_code_sends(self):
        sent = recorded("roundtrip_outbound.json")["search_parameters"]
        for name, value in search_params(REAL_TRIP).items():
            self.assertEqual(str(sent[name]).lower(), value.lower(), name)

    def test_outbound_reply_parses(self):
        result = parse(REAL_TRIP, recorded("roundtrip_outbound.json"))
        self.assertEqual(len(result.offers), 90)
        cheapest = result.cheapest
        self.assertEqual(cheapest.price, 646)
        self.assertEqual((cheapest.origin, cheapest.destination, cheapest.stops), ("EWR", "LHR", 2))
        self.assertEqual(cheapest.airlines, ("Scandinavian Airlines",))
        self.assertTrue(result.url.startswith("https://www.google.com/travel/flights"))

    def test_offers_keep_every_flight_of_a_connection(self):
        cheapest = parse(REAL_TRIP, recorded("roundtrip_outbound.json")).cheapest
        self.assertEqual([segment.flight_number for segment in cheapest.segments], ["SK 904", "SK 491", "SK 815"])
        self.assertEqual([(s.origin, s.destination) for s in cheapest.segments], [("EWR", "ARN"), ("ARN", "OSL"), ("OSL", "LHR")])
        self.assertEqual(cheapest.segments[0].depart_at, datetime(2026, 12, 8, 17, 40))
        self.assertEqual(cheapest.segments[-1].arrive_at, datetime(2026, 12, 9, 21, 35))
        self.assertEqual(
            cheapest.itinerary_key, "SK904@2026-12-08T17:40>SK491@2026-12-09T16:20>SK815@2026-12-09T20:10"
        )
        self.assertEqual(cheapest.duration_min, 1375)

    def test_every_real_offer_has_flight_numbers_and_a_key_of_its_own(self):
        offers = parse(REAL_TRIP, recorded("roundtrip_outbound.json")).offers
        self.assertTrue(all(segment.flight_number for offer in offers for segment in offer.segments))
        self.assertEqual(len({offer.itinerary_key for offer in offers}), len(offers))

    def test_nothing_in_a_search_reply_marks_a_catch_or_a_seller(self):
        for offer in parse(REAL_TRIP, recorded("roundtrip_outbound.json")).offers:
            self.assertIsNone(offer.seller)
            self.assertIsNone(offer.return_segments)
            self.assertFalse(offer.self_transfer or offer.hidden_city or offer.separate_tickets or offer.basic_economy)

    def test_reply_says_when_serpapi_fetched_it(self):
        when = fetched_at(recorded("roundtrip_outbound.json"))
        self.assertEqual(when, datetime(2026, 10, 9, 0, 35, 57, tzinfo=timezone.utc))
        self.assertIsNone(fetched_at({"search_metadata": {}}))
        self.assertIsNone(fetched_at({"search_metadata": {"processed_at": "yesterday"}}))

    def test_sorting_by_price_brings_no_best_flights_and_no_price_insights(self):
        reply = recorded("roundtrip_outbound.json")
        self.assertNotIn("best_flights", reply)
        self.assertNotIn("price_insights", reply)
        self.assertIsNone(parse(REAL_TRIP, reply).insights)

    def test_outbound_options_carry_a_departure_token_and_no_booking_token(self):
        for option in options(recorded("roundtrip_outbound.json")):
            self.assertTrue(option["departure_token"])
            self.assertNotIn("booking_token", option)

    def test_a_departure_token_can_return_no_flights(self):
        reply = recorded("roundtrip_return_empty.json")
        self.assertEqual(reply["search_metadata"]["status"], "Success")
        self.assertEqual(reply["error"], "Google Flights hasn't returned any results for this query.")
        self.assertEqual(parse(REAL_TRIP, reply).offers, ())

    def test_return_options_carry_a_booking_token_and_the_round_trip_total(self):
        returns = options(recorded("roundtrip_return.json"))
        self.assertEqual(len(returns), 54)
        for option in returns:
            self.assertTrue(option["booking_token"])
            self.assertNotIn("departure_token", option)
            self.assertEqual(option["flights"][0]["departure_airport"]["id"], "LHR")
        # The outbound, UA 14, was listed at 695: the cheapest return completes it at that price.
        self.assertEqual(min(option["price"] for option in returns), 695)

    def test_booking_options_name_each_seller_and_flag_separate_tickets(self):
        reply = recorded("roundtrip_booking.json")
        sellers = reply["booking_options"]
        self.assertEqual(len(sellers), 12)
        cheapest = min(sellers, key=lambda seller: seller["together"]["price"])
        self.assertEqual(cheapest["together"]["price"], 694)
        self.assertTrue(cheapest["separate_tickets"])
        self.assertEqual({"departing", "returning"} <= set(cheapest), True)
        one_ticket = [seller["together"] for seller in sellers if not seller.get("separate_tickets")]
        best = min(one_ticket, key=lambda together: together["price"])
        self.assertEqual((best["book_with"], best["price"], best["airline"]), ("United", 797, True))
        self.assertEqual([leg["flights"][0]["flight_number"] for leg in reply["selected_flights"]], ["UA 14", "LX 345"])

    def test_one_way_request_matches_what_the_code_sends(self):
        sent = recorded("oneway.json")["search_parameters"]
        for name, value in search_params(REAL_ONE_WAY).items():
            self.assertEqual(str(sent[name]).lower(), value.lower(), name)
        self.assertNotIn("return_date", sent)

    def test_one_way_options_carry_a_booking_token_straight_away(self):
        found = options(recorded("oneway.json"))
        self.assertEqual(len(found), 126)
        for option in found:
            self.assertTrue(option["booking_token"])
            self.assertNotIn("departure_token", option)
            self.assertEqual(option["type"], "One way")
            self.assertEqual(option["extensions"], ["Checked baggage for a fee"])

    def test_one_way_reply_parses_with_price_insights_despite_sorting_by_price(self):
        result = parse(REAL_ONE_WAY, recorded("oneway.json"))
        self.assertEqual(len(result.offers), 126)
        cheapest = result.cheapest
        self.assertEqual((cheapest.price, cheapest.origin, cheapest.destination, cheapest.stops), (129, "LAX", "JFK", 1))
        self.assertEqual(cheapest.itinerary_key, "B6900@2026-12-08T23:35>B61702@2026-12-09T09:30")
        self.assertEqual(len({offer.itinerary_key for offer in result.offers}), 126)
        self.assertEqual((result.insights.lowest_price, result.insights.level), (129, "typical"))
        self.assertEqual(result.insights.typical_range, (70, 205))
        self.assertEqual(len(result.insights.price_history), 61)

    def test_real_price_history_is_one_point_a_day_for_two_months(self):
        history = parse(REAL_TRIP, recorded("roundtrip_outbound_unsorted.json")).insights.price_history
        days = [when.date() for when, _ in history]
        self.assertEqual((len(days), days[0], days[-1]), (61, date(2026, 8, 9), date(2026, 10, 8)))
        self.assertEqual({(later - earlier).days for earlier, later in zip(days, days[1:])}, {1})
        self.assertEqual((min(price for _, price in history), max(price for _, price in history)), (516, 706))

    def test_leaving_out_sort_by_gains_the_price_rating_but_loses_the_cheapest_fares(self):
        by_price = parse(REAL_TRIP, recorded("roundtrip_outbound.json"))
        default = parse(REAL_TRIP, recorded("roundtrip_outbound_unsorted.json"))
        self.assertIsNone(by_price.insights)
        self.assertEqual((default.insights.lowest_price, default.insights.level), (693, "typical"))

        listed = {offer.itinerary_key for offer in default.offers}
        dropped = [offer for offer in by_price.offers if offer.itinerary_key not in listed]
        self.assertEqual(len(dropped), 14)
        self.assertEqual({offer.airlines for offer in dropped}, {("Scandinavian Airlines",)})
        self.assertEqual(dropped[:10], list(by_price.offers[:10]))  # every one of the ten cheapest
        self.assertEqual((by_price.cheapest.price, default.cheapest.price), (646, 693))
        # Apart from those, both replies agree on the cheapest fare.
        self.assertEqual(min(offer.price for offer in by_price.offers if offer not in dropped), 693)

    def test_no_recorded_reply_holds_an_api_key_or_an_account_link(self):
        for path in FIXTURES.glob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("api_key", text, path.name)
            for link in ("json_endpoint", "raw_html_file"):
                if f'"{link}"' in text:
                    self.assertIn("https://serpapi.com/searches/REDACTED/", text, path.name)


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
