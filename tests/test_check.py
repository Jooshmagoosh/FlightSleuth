import json
import sqlite3
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from flightsleuth import config, providers, scan
from flightsleuth.errors import UserError
from flightsleuth.models import Offer, SearchResult, Segment
from flightsleuth.providers import Capability, DataClass, Provider
from tests.fakes import FakeSerpApi, history_rows, install, rich_reply

TODAY = date(2030, 1, 1)
DEPART, BACK = date(2030, 3, 12), date(2030, 3, 19)  # the fake's round-trip fare for the 12th is 520


def request(**changes):
    options = {"return_date": BACK, "check": True, **changes}
    return scan.Request("JFK", "LHR", DEPART, **options)


def look(**changes):
    """Run one fixed-date search with the closer look on, and return the plan, the outcome and its one check."""
    plan = scan.plan(request(**changes), today=TODAY)
    outcome = scan.run(plan)
    (check,) = outcome.checks
    return plan, outcome, check


def endpoints():
    return [query.endpoint + (f":{query.market}" if query.market else "") for query in FakeSerpApi.searches]


def age_the_cache(hours):
    db = sqlite3.connect(config.cache_path())
    with db:
        db.execute("UPDATE responses SET fetched_at = fetched_at - ?", (hours * 3600,))
    db.close()


class ConfirmTest(unittest.TestCase):
    """No fare is called the cheapest until its return flights have been fetched."""

    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def test_the_closer_look_is_counted_in_the_plan_before_anything_is_spent(self):
        plan = scan.plan(request(), today=TODAY)
        # The search, then at most: two return lookups, the sellers, one other market, two one-ways.
        self.assertEqual((len(plan.calls), plan.check_calls, plan.live), (1, 6, 7))
        self.assertEqual(FakeSerpApi.searches, [])

    def test_it_is_off_for_a_fixed_date_search_unless_asked_for(self):
        plan = scan.plan(scan.Request("JFK", "LHR", DEPART, return_date=BACK), today=TODAY)
        self.assertEqual((plan.checks, plan.live), (0, 1))
        outcome = scan.run(plan)
        self.assertEqual((outcome.checks, len(FakeSerpApi.searches)), ([], 1))

    def test_the_cheapest_fare_gets_its_return_flights_and_its_sellers(self):
        plan, outcome, check = look()
        self.assertEqual(endpoints(), ["search", "return_flights", "booking_options", "search:gb", "search", "search"])
        self.assertEqual(outcome.live, 6)  # the spare return lookup was not needed
        confirmed = check.confirmed
        self.assertEqual((confirmed.price, confirmed.airlines), (520, ("Testair",)))
        self.assertEqual([s.flight_number for s in confirmed.return_segments], ["TA 2"])
        self.assertEqual((confirmed.return_segments[0].origin, confirmed.return_segments[0].destination), ("LHR", "JFK"))
        self.assertEqual(confirmed.itinerary_key, "TA1@2030-03-12T18:30|TA2@2030-03-19T09:00")
        self.assertEqual((check.failed, check.unchecked, check.stale), ((), (), False))
        self.assertTrue(confirmed.verified_live)

    def test_the_confirmed_fare_takes_its_listings_place_in_the_results(self):
        _, outcome, check = look()
        offers = outcome.results[check.trip].offers
        self.assertIs(offers[0], check.confirmed)
        self.assertEqual([offer.airlines for offer in offers], [("Testair",), ("Otherair",)])

    def test_a_fare_with_no_return_flights_is_dropped_and_the_next_one_tried(self):
        FakeSerpApi.phantoms = 1
        _, outcome, check = look()
        self.assertEqual(endpoints()[:3], ["search", "return_flights", "return_flights"])
        self.assertEqual(outcome.live, 7)
        self.assertEqual([(offer.price, offer.airlines) for offer in check.failed], [(480, ("Phantom Air",))])
        self.assertEqual((check.confirmed.price, check.confirmed.airlines), (520, ("Testair",)))
        # The fare that was not real is no longer listed, so nothing shows it as the cheapest.
        self.assertEqual(outcome.results[check.trip].cheapest.price, 520)
        self.assertNotIn(("Phantom Air",), [offer.airlines for offer in outcome.results[check.trip].offers])

    def test_the_spare_lookup_goes_to_a_different_airline_and_the_rest_are_reported_unchecked(self):
        FakeSerpApi.phantoms = 3
        _, outcome, check = look()
        self.assertEqual(len(check.failed), 1)
        self.assertEqual(check.confirmed.airlines, ("Testair",))
        self.assertEqual([offer.price for offer in check.unchecked], [481, 482])
        self.assertEqual(outcome.results[check.trip].cheapest.price, 481)

    def test_when_every_fare_tried_comes_back_empty_nothing_is_confirmed(self):
        FakeSerpApi.phantoms = 1
        empty = {"search_metadata": {"status": "Success"}, "error": "Google Flights hasn't returned any results."}
        real = FakeSerpApi.search

        def no_returns(self, query):
            FakeSerpApi.searches.append(query)
            return empty if query.departure_token else rich_reply(query)

        with mock.patch.object(FakeSerpApi, "search", no_returns):
            _, outcome, check = look()
        self.assertIsNone(check.confirmed)
        self.assertEqual(len(check.failed), 2)
        self.assertNotIn("booking_options", endpoints())
        self.assertEqual([offer.airlines for offer in outcome.results[check.trip].offers], [("Otherair",)])
        self.assertIs(real, FakeSerpApi.search)

    def test_the_return_lookups_price_replaces_the_listed_one(self):
        FakeSerpApi.return_surcharge = 45
        _, outcome, check = look()
        self.assertEqual(check.confirmed.price, 565)
        # Otherair still lists 550, cheaper than what was confirmed, and it was not looked up.
        self.assertEqual([(offer.price, offer.airlines) for offer in check.unchecked], [(550, ("Otherair",))])
        self.assertEqual([offer.price for offer in outcome.results[check.trip].offers], [550, 565])

    def test_a_listing_with_no_token_cannot_be_confirmed(self):
        FakeSerpApi.rich = False
        _, outcome, check = look()
        self.assertIsNone(check.confirmed)
        self.assertEqual(check.failed, ())
        self.assertEqual(endpoints(), ["search", "search", "search"])  # only the two one-ways were added
        self.assertEqual(outcome.results[check.trip].cheapest.price, 520)

    def test_a_one_way_needs_no_return_lookup(self):
        plan = scan.plan(scan.Request("JFK", "LHR", DEPART, check=True), today=TODAY)
        self.assertEqual((plan.check_calls, plan.live), (2, 3))
        outcome = scan.run(plan)
        (check,) = outcome.checks
        self.assertEqual(endpoints(), ["search", "booking_options", "search:gb"])
        self.assertEqual((check.confirmed.price, check.confirmed.return_segments), (200, None))
        self.assertEqual([a.kind for a in check.alternatives], ["seller", "market", "seller"])


class AlternativesTest(unittest.TestCase):
    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def by_kind(self, check, kind):
        return [alternative for alternative in check.alternatives if alternative.kind == kind]

    def test_sellers_are_the_same_flights_and_say_whether_an_airline_sells_them(self):
        _, _, check = look()
        sellers = self.by_kind(check, "seller")
        self.assertEqual([(s.name, s.price, s.same_flights) for s in sellers], [("Testair", 520, True), ("Cheapo Travel", 545, True)])
        self.assertEqual([s.offers[0].sold_by_airline for s in sellers], [True, False])
        self.assertEqual([s.offers[0].seller for s in sellers], ["Testair", "Cheapo Travel"])
        self.assertEqual(sellers[0].offers[0].itinerary_key, check.confirmed.itinerary_key)

    def test_a_fare_sold_as_separate_tickets_is_flagged_with_the_one_ticket_price_beside_it(self):
        FakeSerpApi.separate = True
        _, _, check = look()
        cheapest, agency, one_ticket = self.by_kind(check, "seller")
        self.assertEqual((cheapest.name, cheapest.price), ("Testair, Otherair", 520))
        self.assertTrue(cheapest.offers[0].separate_tickets)
        self.assertEqual(cheapest.parts, (("Testair", 320), ("Otherair", 200)))
        self.assertEqual((one_ticket.name, one_ticket.price, one_ticket.offers[0].separate_tickets), ("Testair", 623, False))
        self.assertEqual(agency.price, 545)

    def test_the_booking_reply_supplies_the_price_rating_a_sorted_search_lacks(self):
        _, outcome, check = look()
        insights = outcome.results[check.trip].insights
        self.assertEqual((insights.lowest_price, insights.level), (520, "typical"))

    def test_the_destinations_market_is_priced_and_compared_by_its_flights(self):
        FakeSerpApi.market_savings = {"gb": 30}
        _, _, check = look()
        (market,) = self.by_kind(check, "market")
        self.assertEqual((market.name, market.price, market.same_flights), ("gb", 490, True))
        self.assertEqual(market.offers[0].query.market, "gb")
        self.assertIsNone(market.offers[0].return_segments)  # listed there, not confirmed
        self.assertEqual(check.alternatives[0].kind, "split")  # cheapest first: 400, then 490

    def test_a_domestic_route_has_no_other_market_to_try(self):
        FakeSerpApi.countries = ("US", "US")
        _, outcome, check = look()
        self.assertEqual(self.by_kind(check, "market"), [])
        self.assertEqual(outcome.live, 5)

    def test_another_market_can_be_named(self):
        FakeSerpApi.countries = ("US", "US")
        _, _, check = look(other_market="pl")
        self.assertEqual([m.name for m in self.by_kind(check, "market")], ["pl"])

    def test_the_market_already_asked_for_is_not_asked_again(self):
        _, _, check = look(market="gb")
        self.assertEqual(self.by_kind(check, "market"), [])
        self.assertNotIn("search:gb:gb", endpoints())

    def test_two_one_ways_are_set_beside_the_round_trip_as_a_different_itinerary(self):
        _, _, check = look()
        (split,) = self.by_kind(check, "split")
        self.assertEqual((split.price, split.same_flights), (400, False))
        self.assertEqual([(leg.origin, leg.destination, leg.price) for leg in split.offers], [("JFK", "LHR", 200), ("LHR", "JFK", 200)])

    def test_a_split_search_does_not_pay_for_the_one_ways_twice(self):
        plan, outcome, check = look(split=True)
        self.assertEqual((len(plan.calls), plan.check_calls), (3, 4))
        self.assertEqual(outcome.live, 6)
        self.assertEqual(len(self.by_kind(check, "split")), 1)

    def test_another_sources_fare_is_compared_by_its_flights(self):
        class Rival(Provider):
            name = "rival"
            capabilities = frozenset({Capability.LIVE_SEARCH})
            cost_per_call = 0
            data_class = DataClass.SANCTIONED

            def search(self, query):
                return {}

            def parse(self, query, raw):
                when = datetime(2030, 3, 12, 18, 30)
                flight = Segment("Testair", "TA 1", "JFK", "LHR", when, when)
                return SearchResult(query, (Offer(query, 505, (flight,), duration_min=430),))

        providers.register(Rival)
        self.addCleanup(providers.unregister, "rival")
        _, _, check = look()
        self.assertEqual(check.confirmed.source, "serpapi")
        (rival,) = self.by_kind(check, "source")
        self.assertEqual((rival.name, rival.price, rival.same_flights), ("rival", 505, True))
        # Its fare is cheaper than the confirmed one and nobody fetched its return flights.
        self.assertEqual([offer.source for offer in check.unchecked], ["rival"])


class RecordedChainTest(unittest.TestCase):
    """The closer look, run over the real replies in tests/fixtures rather than the fake's."""

    def setUp(self):
        install(self)
        fixtures = Path(__file__).parent / "fixtures"

        def recorded(name):
            return json.loads((fixtures / name).read_text(encoding="utf-8"))

        outbound = recorded("roundtrip_outbound.json")
        dead_end = recorded("roundtrip_return_empty.json")["search_parameters"]["departure_token"]

        def replay(self, query):
            FakeSerpApi.searches.append(query)
            if query.booking_token:
                return recorded("roundtrip_booking.json")
            if query.departure_token:
                # Only the cheapest outbound's token was recorded as leading nowhere.
                return recorded("roundtrip_return_empty.json" if query.departure_token == dead_end else "roundtrip_return.json")
            return outbound if query.return_date else recorded("oneway.json")

        self.enterContext(mock.patch.object(FakeSerpApi, "search", replay))
        request = scan.Request("JFK,EWR", "LHR", date(2026, 12, 8), return_date=date(2026, 12, 15), check=True)
        self.outcome = scan.run(scan.plan(request, today=date(2026, 10, 9)))
        (self.check,) = self.outcome.checks

    def test_the_646_fare_is_dropped_and_the_spare_lookup_goes_to_another_airline(self):
        (missed,) = self.check.failed
        self.assertEqual((missed.price, missed.airlines), (646, ("Scandinavian Airlines",)))
        self.assertNotIn("Scandinavian Airlines", self.check.confirmed.airlines)
        self.assertEqual(endpoints()[:4], ["search", "return_flights", "return_flights", "booking_options"])

    def test_the_confirmed_fare_carries_real_return_flights_at_the_return_replys_price(self):
        confirmed = self.check.confirmed
        self.assertEqual(confirmed.price, 695)
        self.assertEqual([segment.flight_number for segment in confirmed.return_segments], ["LX 345", "LX 18"])
        self.assertEqual(confirmed.return_duration_min, 1170)
        self.assertEqual(confirmed.longest_layover_min, 515)
        self.assertTrue(confirmed.booking_token)

    def test_the_other_cheap_fares_of_that_airline_are_reported_as_not_looked_up(self):
        unchecked = self.check.unchecked
        self.assertEqual(len(unchecked), 12)
        self.assertEqual(len([offer for offer in unchecked if offer.airlines == ("Scandinavian Airlines",)]), 10)
        self.assertEqual(unchecked[0].price, 647)
        self.assertEqual(self.outcome.results[self.check.trip].cheapest.price, 647)

    def test_the_real_sellers_show_the_separate_tickets_and_the_one_ticket_price(self):
        sellers = [found for found in self.check.alternatives if found.kind == "seller"]
        self.assertEqual(len(sellers), 12)
        cheapest = sellers[0]
        self.assertEqual((cheapest.name, cheapest.price, cheapest.parts), ("United, SWISS", 694, (("United", 295), ("SWISS", 399))))
        self.assertTrue(cheapest.offers[0].separate_tickets)
        whole = next(found for found in sellers if not found.offers[0].separate_tickets)
        self.assertEqual((whole.name, whole.price, whole.offers[0].sold_by_airline), ("United", 797, True))

    def test_the_destinations_country_is_read_from_the_reply_and_priced(self):
        self.assertIn("search:gb", endpoints())
        self.assertEqual([found.name for found in self.check.alternatives if found.kind == "market"], ["gb"])

    def test_it_all_renders(self):
        from flightsleuth.render import render_text, summarise

        outcome = self.outcome
        text = render_text(summarise(outcome.trips, outcome.results, checks=outcome.checks))
        self.assertIn("Cheapest confirmed: $695 round trip, Tue 08 Dec - Tue 15 Dec", text)
        self.assertIn("The $646 fare listed for these dates (Scandinavian Airlines (SK 904, SK 491, SK 815)) had no return flights", text)
        self.assertRegex(text, r"back  LHR->EWR  Tue 15 Dec 06:00 -> 20:30 +1 stop  19h 30m  SWISS \(LX 345, LX 18\)")
        self.assertIn("12 cheaper fares are listed for these dates, from $647, but their return flights were not looked up.", text)
        self.assertRegex(text, r"\$694 +Buy from United, SWISS +same flights +two separate tickets \(United \$295 \+ SWISS \$399\); 8h 35m layover")
        self.assertRegex(text, r"\$797 +Buy from United +same flights +one ticket; 8h 35m layover")
        self.assertIn("Google rates $694 as typical for those dates (typical range $540 to $1,850).", text)


PHANTOM = ("PH0@2030-03-12T18:30", 480)  # the fake's first phantom fare on the 12th: its flights and its price


class ConfirmOnlyTest(unittest.TestCase):
    """The lighter closer look a price watch uses: the return flights and the sellers, and nothing else."""

    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def test_it_is_counted_as_two_return_lookups_and_the_sellers(self):
        plan = scan.plan(request(look="confirm"), today=TODAY)
        self.assertEqual((len(plan.calls), plan.check_calls, plan.live), (1, 3, 4))

    def test_it_stops_after_the_sellers(self):
        _, outcome, check = look(look="confirm")
        self.assertEqual(endpoints(), ["search", "return_flights", "booking_options"])
        self.assertEqual(outcome.live, 3)
        self.assertEqual([s.flight_number for s in check.confirmed.return_segments], ["TA 2"])
        self.assertEqual({found.kind for found in check.alternatives}, {"seller"})

    def test_a_one_way_only_needs_its_sellers(self):
        plan = scan.plan(scan.Request("JFK", "LHR", DEPART, check=True, look="confirm"), today=TODAY)
        self.assertEqual((plan.check_calls, plan.live), (1, 2))
        scan.run(plan)
        self.assertEqual(endpoints(), ["search", "booking_options"])

    def test_it_follows_a_listing_fetched_a_moment_ago_without_paying_for_it_again(self):
        # How a watch does it: the listing first, and the closer look only if the price is worth one.
        scan.run(scan.plan(request(check=False), today=TODAY, fresh=True))
        plan = scan.plan(request(look="confirm"), today=TODAY)
        self.assertEqual((plan.saved, plan.live), (1, 3))
        outcome = scan.run(plan)
        self.assertEqual(endpoints(), ["search", "return_flights", "booking_options"])
        self.assertTrue(outcome.checks[0].confirmed.verified_live)

    def test_an_unknown_kind_of_look_is_refused(self):
        with self.assertRaisesRegex(UserError, "'quick' is not a kind of closer look"):
            scan.plan(request(look="quick"), today=TODAY)

    def test_a_caller_with_one_search_to_spare_gets_the_return_flights_and_no_sellers(self):
        plan = scan.plan(request(look="confirm"), today=TODAY, reserve=1)
        self.assertEqual((plan.check_calls, plan.live), (1, 2))
        outcome = scan.run(plan, max_searches=2)
        (check,) = outcome.checks
        self.assertEqual(endpoints(), ["search", "return_flights"])
        self.assertEqual((check.confirmed.price, check.alternatives), (520, ()))

    def test_a_caller_with_none_to_spare_confirms_nothing_and_spends_nothing_more(self):
        plan = scan.plan(request(look="confirm"), today=TODAY, reserve=0)
        self.assertEqual((plan.reserved, plan.live), ({}, 1))
        (check,) = scan.run(plan).checks
        self.assertEqual((check.confirmed, check.failed, endpoints()), (None, (), ["search"]))

    def test_a_reserve_larger_than_the_look_needs_changes_nothing(self):
        self.assertEqual(scan.plan(request(look="confirm"), today=TODAY, reserve=9).check_calls, 3)

    def test_a_fare_known_to_have_no_return_flights_is_not_asked_about_again(self):
        FakeSerpApi.phantoms = 1
        _, outcome, check = look(look="confirm", known_empty=(PHANTOM,))
        # One return lookup, for Testair: the phantom fare's was not repeated.
        self.assertEqual(endpoints(), ["search", "return_flights", "booking_options"])
        self.assertEqual([query.departure_token for query in FakeSerpApi.searches[1:2]], ["dep:520:Testair"])
        self.assertEqual([(offer.price, offer.airlines) for offer in check.failed], [(480, ("Phantom Air",))])
        self.assertEqual(check.confirmed.airlines, ("Testair",))
        self.assertEqual(outcome.results[check.trip].cheapest.price, 520)

    def test_it_is_asked_about_again_once_its_price_changes(self):
        FakeSerpApi.phantoms = 1
        look(look="confirm", known_empty=((PHANTOM[0], 475),))
        self.assertEqual(endpoints(), ["search", "return_flights", "return_flights", "booking_options"])

    def test_the_fare_a_look_would_try_first_can_be_asked_for_without_looking(self):
        FakeSerpApi.phantoms = 1
        outcome = scan.run(scan.plan(request(check=False), today=TODAY))
        (result,) = outcome.results.values()
        self.assertEqual(scan.first_to_try(result).price, 480)
        self.assertEqual(scan.first_to_try(result, (PHANTOM,)).price, 520)
        self.assertEqual(len(FakeSerpApi.searches), 1)


class StaleTokenTest(unittest.TestCase):
    """A token is only followed while the listing it came from is fresh."""

    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def test_an_old_listing_is_fetched_again_before_its_tokens_are_followed(self):
        scan.run(scan.plan(request(check=False), today=TODAY))
        age_the_cache(hours=2)
        FakeSerpApi.searches.clear()

        plan = scan.plan(request(), today=TODAY)
        self.assertEqual((plan.saved, plan.check_calls, plan.live), (1, 7, 7))
        outcome = scan.run(plan)
        self.assertEqual(endpoints()[:3], ["search", "return_flights", "booking_options"])
        self.assertEqual(outcome.live, 6)
        (check,) = outcome.checks
        self.assertTrue(check.confirmed.verified_live)
        self.assertFalse(check.stale)

    def test_a_fresh_saved_listing_is_followed_as_it_is(self):
        scan.run(scan.plan(request(check=False), today=TODAY))
        FakeSerpApi.searches.clear()
        plan = scan.plan(request(), today=TODAY)
        self.assertEqual(plan.check_calls, 6)
        scan.run(plan)
        self.assertEqual(endpoints()[:2], ["return_flights", "booking_options"])

    def test_an_old_run_that_is_wholly_saved_replays_for_nothing(self):
        _, first, _ = look()
        age_the_cache(hours=2)
        FakeSerpApi.searches.clear()
        _, again, check = look()
        self.assertEqual((FakeSerpApi.searches, again.live), ([], 0))
        self.assertEqual(check.confirmed.price, first.checks[0].confirmed.price)
        self.assertFalse(check.confirmed.verified_live)
        self.assertLess(check.confirmed.fetched_at, first.checks[0].confirmed.fetched_at)  # two hours ago, and it says so

    def test_with_no_room_to_refresh_the_check_says_the_listing_is_stale(self):
        scan.run(scan.plan(request(check=False), today=TODAY))
        age_the_cache(hours=2)
        FakeSerpApi.searches.clear()
        plan = scan.plan(request(), today=TODAY)
        plan.reserved["serpapi"] = 0
        outcome = scan.run(plan)
        (check,) = outcome.checks
        self.assertEqual((check.confirmed, check.stale, FakeSerpApi.searches), (None, True, []))


class RecordTest(unittest.TestCase):
    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def test_every_follow_up_is_logged_under_its_own_endpoint(self):
        look()
        self.assertEqual(
            [(call["endpoint"], call["live"], call["cost"]) for call in history_rows("provider_calls")],
            [("search", 1, 1), ("return_flights", 1, 1), ("booking_options", 1, 1), ("search", 1, 1), ("search", 1, 1), ("search", 1, 1)],
        )

    def test_a_replayed_follow_up_is_logged_as_saved(self):
        look()
        look()
        self.assertEqual([call["live"] for call in history_rows("provider_calls")], [1] * 6 + [0] * 6)

    def test_only_plain_searches_add_price_points(self):
        look()
        points = [(p["origin"], p["return_date"], p["price"]) for p in history_rows("price_points")]
        # The round trip and the two one-ways. Not the return lookup, and not the other market's price.
        self.assertEqual(points, [("JFK", "2030-03-19", 520), ("JFK", None, 200), ("LHR", None, 200)])

    def test_a_search_priced_with_bags_adds_no_price_point(self):
        scan.run(scan.plan(request(check=False, bags=1), today=TODAY))
        self.assertEqual(history_rows("price_points"), [])

    def test_googles_price_history_is_kept_in_its_own_table_one_row_a_day(self):
        days = [[int(datetime(2030, 1, day, tzinfo=timezone.utc).timestamp()), 500 + day] for day in (1, 2, 3)]

        def with_history(self, query):
            FakeSerpApi.searches.append(query)
            body = rich_reply(query)
            body["price_insights"] = {"lowest_price": 520, "price_level": "low", "price_history": days}
            return body

        with mock.patch.object(FakeSerpApi, "search", with_history):
            scan.run(scan.plan(request(check=False), today=TODAY))
            scan.run(scan.plan(request(check=False), today=TODAY, fresh=True))
        rows = history_rows("google_price_history")
        self.assertEqual([(row["day"], row["price"]) for row in rows], [("2030-01-01", 501), ("2030-01-02", 502), ("2030-01-03", 503)])
        self.assertEqual({(row["origin"], row["depart_date"], row["return_date"], row["source"]) for row in rows}, {("JFK", "2030-03-12", "2030-03-19", "serpapi")})
        self.assertEqual(len(history_rows("price_points")), 2)  # none of Google's points went in there

    def test_the_new_tables_are_append_only_too(self):
        look()
        db = sqlite3.connect(config.history_path())
        self.addCleanup(db.close)
        guards = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        for table in ("google_price_history", "bakeoff_fares", "bakeoff_lookups"):
            self.assertTrue({f"{table}_no_update", f"{table}_no_delete"} <= guards, table)

    def test_a_run_clears_expired_replies_out_of_the_cache(self):
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 10)), today=TODAY))
        age_the_cache(hours=7)
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 11)), today=TODAY))
        db = sqlite3.connect(config.cache_path())
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 1)

    def test_a_run_that_fetches_nothing_leaves_the_cache_alone(self):
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 10)), today=TODAY))
        age_the_cache(hours=5)
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 10)), today=TODAY))
        self.assertEqual(len(FakeSerpApi.searches), 1)


class RiskyFareTest(unittest.TestCase):
    """Hidden-city and self-transfer fares stay out unless they are asked for."""

    def setUp(self):
        install(self)

        class Skipper(Provider):
            name = "skipper"
            capabilities = frozenset({Capability.LIVE_SEARCH})
            cost_per_call = 0
            data_class = DataClass.SANCTIONED

            def search(self, query):
                return {}

            def parse(self, query, raw):
                when = datetime(2030, 3, 12, 6, 0)
                flight = Segment("Hopair", "HP 9", "JFK", "LHR", when, when)
                offers = (
                    Offer(query, 90, (flight,), duration_min=400, hidden_city=True),
                    Offer(query, 95, (flight,), duration_min=400, self_transfer=True),
                    Offer(query, 150, (flight,), duration_min=400),
                )
                return SearchResult(query, offers)

        providers.register(Skipper)
        self.addCleanup(providers.unregister, "skipper")

    def prices(self, **changes):
        outcome = scan.run(scan.plan(scan.Request("JFK", "LHR", DEPART, **changes), today=TODAY))
        (result,) = outcome.results.values()
        return [offer.price for offer in result.offers]

    def test_they_are_left_out_by_default(self):
        self.assertEqual(self.prices(), [150, 200])

    def test_they_are_listed_when_asked_for(self):
        self.assertEqual(self.prices(risky=True), [90, 95, 150, 200])


if __name__ == "__main__":
    unittest.main()
