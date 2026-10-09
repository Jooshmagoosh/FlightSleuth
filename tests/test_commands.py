import contextlib
import io
import json
import os
import re
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from flightsleuth import cli, config, links, providers
from flightsleuth.models import Offer, Query, SearchResult, Segment
from flightsleuth.providers import Capability, DataClass, Provider
from tests.fakes import FakeSerpApi, install, rich_reply
from tests.test_window import valley

GATE = ("search", "JFK", "LHR", "--window", "2030-03-01", "2030-05-30", "--nights", "5-9")
FIXED = ("search", "JFK", "LHR", "--depart", "2030-03-12", "--return", "2030-03-19")
STAMP = r"\d\d \w{3} \d\d:\d\d"  # a fetch time, such as "08 Oct 14:02"
EMPTY = {"search_metadata": {"status": "Success"}, "error": "Google Flights hasn't returned any results."}


class CommandTest(unittest.TestCase):
    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class GateCommandTest(CommandTest):
    """The search that costs 455 calls as a full grid, through the real command line."""

    def test_it_dry_runs_at_25_with_no_key_and_spends_nothing(self):
        with mock.patch.dict(os.environ, {config.KEY_VAR: ""}):
            code, out, err = self.run_cli(*GATE, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            err,
            "Up to 25 searches needed: 12 to sample the window, up to 7 to refine around the best dates, "
            "up to 6 to look closer at the best fare. 0 saved, up to 25 live.\n",
        )
        self.assertEqual((out, FakeSerpApi.searches), ("", []))

    def test_thorough_dry_runs_at_40(self):
        code, _, err = self.run_cli(*GATE, "--thorough", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Up to 40 searches needed: 18 to sample the window, up to 10 to refine", err)
        self.assertIn("up to 12 to look closer at the 2 best fares. 0 saved, up to 40 live.", err)

    def test_the_full_grid_is_still_refused(self):
        code, _, err = self.run_cli(*GATE, "--strategy", "grid", "--no-check")
        self.assertEqual(code, 1)
        self.assertIn("455 searches needed: 0 saved, 455 live.", err)
        self.assertIn("over the limit of 25 live searches", err)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_the_search_itself_stays_inside_the_cap_and_confirms_what_it_calls_cheapest(self):
        FakeSerpApi.fare = valley
        code, out, err = self.run_cli(*GATE)
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(FakeSerpApi.searches), 25)
        self.assertIn("Cheapest: $410 round trip, Wed 17 Apr - Wed 24 Apr", out)
        self.assertIn("Wed 17 Apr - Wed 24 Apr: $410 confirmed", out)
        self.assertIn("  back  LHR->JFK  Wed 24 Apr 09:00 -> 12:00  nonstop  8h 00m  Testair (TA 2)", out)
        self.assertRegex(out, rf"Return flights fetched {STAMP}\.")
        self.assertIn(f"About {250 - len(FakeSerpApi.searches)} SerpApi searches left this month.", out)

    def test_a_search_needs_a_day_or_a_window_and_not_both(self):
        for argv in (
            ("search", "JFK", "LHR", "--nights", "7"),
            ("search", "JFK", "LHR", "--nights", "7", "--depart", "2030-03-12", "--window", "2030-03-01", "2030-03-09"),
        ):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 1)
            self.assertIn("give either --depart or --window", err)

    def test_a_one_way_window_works(self):
        code, out, err = self.run_cli("search", "JFK", "LHR", "--window", "2030-03-01", "2030-05-30", "--one-way")
        self.assertEqual(code, 0, err)
        self.assertIn("to sample the window", err)
        self.assertIn("Cheapest: $200 one way", out)
        self.assertLessEqual(len(FakeSerpApi.searches), 25)


class FetchTimeTest(CommandTest):
    """Every price has its fetch time beside it, and a saved one says it is saved."""

    def test_a_live_price_shows_when_it_was_fetched(self):
        _, out, _ = self.run_cli(*FIXED, "--flex", "1")
        self.assertRegex(out, rf"Mon 11 Mar  Mon 18 Mar  \$455        <- cheapest  {STAMP}\n")
        self.assertRegex(out, rf"\$455   JFK->LHR  Mon 11 Mar 18:30 -> 23:10  Mon 18 Mar  nonstop  7h 10m  Testair   {STAMP}\n")
        self.assertNotIn("(saved)", out)

    def test_a_replayed_price_is_marked_saved_wherever_it_appears(self):
        self.run_cli(*FIXED, "--check")
        _, out, _ = self.run_cli(*FIXED, "--check")
        # Two itineraries, the return lookup, and four other ways to fly: all seven prices say so.
        self.assertEqual(out.count("(saved)"), 7)
        self.assertEqual(len(re.findall(STAMP + r" \(saved\)", out)), 7)
        self.assertRegex(out, rf"Return flights fetched {STAMP} \(saved\)\.")

    def test_a_row_with_two_prices_shows_the_older_fetch(self):
        self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        _, out, _ = self.run_cli(*FIXED, "--split")
        # The outbound one-way was saved by the first command, so the whole row counts as saved.
        self.assertRegex(out, rf"Tue 12 Mar  Tue 19 Mar  \$520        \$400          <- cheapest  {STAMP} \(saved\)")

    def test_explore_prices_show_it_too(self):
        FakeSerpApi.explore_reply = {"destinations": [{"name": "London", "start_date": "2030-03-03", "flight_price": 436}]}
        _, out, _ = self.run_cli("explore", "MCO")
        self.assertRegex(out, rf"\$436   London .* {STAMP}\n")


class CloserLookOutputTest(CommandTest):
    def test_without_it_a_round_trip_price_is_called_listed_and_the_way_to_confirm_it_is_given(self):
        code, out, _ = self.run_cli(*FIXED)
        self.assertIn("Cheapest listed: $520 round trip, Tue 12 Mar - Tue 19 Mar (not yet confirmed)\n", out)
        self.assertNotIn("Cheapest: ", out)
        self.assertIn(
            "This is the listed price: its return flights have not been looked up. "
            "Add --check to look them up, for up to 6 more searches.",
            out,
        )
        self.assertEqual(len(FakeSerpApi.searches), 1)

    def test_a_one_way_price_needs_no_such_warning(self):
        _, out, _ = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        self.assertIn("Cheapest: $200 one way, Tue 12 Mar\n", out)
        self.assertNotIn("listed", out)

    def test_two_one_ways_winning_needs_no_such_warning(self):
        _, out, _ = self.run_cli(*FIXED, "--split")
        self.assertIn("Cheapest: $400 as two one-way tickets", out)
        self.assertNotIn("listed", out)

    def test_a_window_searched_without_the_closer_look_is_listed_too(self):
        _, out, _ = self.run_cli(*GATE, "--no-check")
        self.assertRegex(out, r"Cheapest listed: \$\d+ round trip, .* \(not yet confirmed\)\n")
        self.assertNotIn("Cheapest: ", out)

    def test_not_yet_confirmed_and_not_confirmed_are_different_things(self):
        # Nobody looked, against somebody looked and the fare did not hold up.
        _, before, _ = self.run_cli(*FIXED)
        FakeSerpApi.phantoms = 1
        with mock.patch.object(FakeSerpApi, "search", lambda self, q: EMPTY if q.departure_token else rich_reply(q)):
            _, after, _ = self.run_cli(*FIXED, "--check", "--fresh")
        self.assertIn("(not yet confirmed)\n", before)
        self.assertIn("(not confirmed)\n", after)
        self.assertNotIn("not yet", after)

    def test_with_it_the_fare_is_confirmed_and_set_beside_the_other_ways_to_fly(self):
        FakeSerpApi.market_savings = {"gb": 30}
        code, out, err = self.run_cli(*FIXED, "--check")
        self.assertEqual(code, 0, err)
        self.assertIn("Up to 7 searches needed: 1 to price the dates, up to 6 to look closer at the best fare.", err)
        self.assertIn("Cheapest: $520 round trip, Tue 12 Mar - Tue 19 Mar\n", out)
        self.assertNotIn("listed price", out)
        self.assertIn("Google rates $520 as typical for those dates (typical range $500 to $800).", out)
        self.assertIn("  out   JFK->LHR  Tue 12 Mar 18:30 -> 23:10  nonstop  7h 10m  Testair (TA 1)", out)
        self.assertIn("  Other ways to fly these dates:", out)
        self.assertRegex(out, r"\$400   Two one-way tickets +different itinerary +two separate tickets \(\$200 \+ \$200\)")
        self.assertRegex(out, r"\$490   As sold in GB +same outbound flights +listed; return flights not looked up")
        self.assertRegex(out, r"\$520   Buy from Testair +same flights +one ticket ")
        self.assertRegex(out, r"\$545   Buy from Cheapo Travel +same flights +one ticket; an agency, not the airline")

    def test_a_fare_with_no_return_flights_is_named_and_not_called_the_cheapest(self):
        FakeSerpApi.phantoms = 1
        _, out, _ = self.run_cli(*FIXED, "--check")
        self.assertIn(
            "The $480 fare listed for these dates (Phantom Air (PH 0)) had no return flights, so it was left out.", out
        )
        self.assertIn("Cheapest: $520 round trip", out)
        self.assertNotIn("$480   JFK->LHR", out)

    def test_cheaper_fares_left_unchecked_change_the_headline(self):
        FakeSerpApi.phantoms = 3
        _, out, _ = self.run_cli(*FIXED, "--check")
        self.assertIn("Cheapest confirmed: $520 round trip, Tue 12 Mar - Tue 19 Mar", out)
        self.assertIn("2 cheaper fares are listed for these dates, from $481, but their return flights were not looked up.", out)

    def test_nothing_confirmed_is_said_in_the_headline(self):
        FakeSerpApi.phantoms = 1
        with mock.patch.object(FakeSerpApi, "search", lambda self, q: EMPTY if q.departure_token else rich_reply(q)):
            _, out, _ = self.run_cli(*FIXED, "--check")
        # Phantom Air and Testair both came back empty; Otherair is left, and nobody looked it up.
        self.assertIn("Cheapest listed: $550 round trip, Tue 12 Mar - Tue 19 Mar (not confirmed)", out)
        self.assertIn("Tue 12 Mar - Tue 19 Mar: not confirmed", out)
        self.assertEqual(out.count("had no return flights, so it was left out."), 2)
        self.assertNotIn("Cheapest: ", out)

    def test_when_every_listed_fare_fails_the_reason_is_given_with_no_fares(self):
        with mock.patch.object(FakeSerpApi, "search", lambda self, q: EMPTY if q.departure_token else rich_reply(q)):
            _, out, _ = self.run_cli(*FIXED, "--check")
        self.assertIn("No fares found.", out)
        self.assertEqual(out.count("had no return flights, so it was left out."), 2)
        self.assertNotIn("Cheapest", out)

    def test_a_source_with_no_follow_up_says_why_nothing_was_confirmed(self):
        FakeSerpApi.rich = False
        _, out, _ = self.run_cli(*FIXED, "--check")
        self.assertIn("Cheapest listed: $520 round trip, Tue 12 Mar - Tue 19 Mar (not confirmed)", out)
        self.assertIn("The source gave no way to look up the return flights of these fares.", out)

    def test_separate_tickets_are_labelled_with_the_one_ticket_price_beside_them(self):
        FakeSerpApi.separate = True
        _, out, _ = self.run_cli(*FIXED, "--check")
        self.assertRegex(
            out, r"\$520   Buy from Testair, Otherair +same flights +two separate tickets \(Testair \$320 \+ Otherair \$200\)"
        )
        self.assertRegex(out, r"\$623   Buy from Testair +same flights +one ticket ")

    def test_the_one_ticket_price_is_shown_even_when_many_sellers_are_cheaper(self):
        FakeSerpApi.separate = True

        def crowded(self, query):
            body = rich_reply(query)
            if query.booking_token:
                agencies = [{"together": {"book_with": f"Agency {n}", "price": 530 + n}} for n in range(5)]
                body["booking_options"] = body["booking_options"][:1] + agencies + body["booking_options"][1:]
            return body

        with mock.patch.object(FakeSerpApi, "search", crowded):
            _, out, _ = self.run_cli(*FIXED, "--check")
        self.assertIn("Buy from Agency 1", out)
        self.assertNotIn("Buy from Agency 2", out)
        self.assertRegex(out, r"\$623   Buy from Testair +same flights +one ticket")

    def test_bags_and_market_are_stated_in_the_title(self):
        _, out, _ = self.run_cli(*FIXED, "--bags", "1", "--market", "GB")
        self.assertIn("JFK -> LHR, round trip, 1 adult, economy, 1 carry-on bag in every price, priced for GB", out)
        self.assertEqual((FakeSerpApi.searches[0].bags, FakeSerpApi.searches[0].market), (1, "gb"))
        self.assertIn("Cheapest listed: $555 round trip", out)  # the fake adds 35 a bag

    def test_a_long_layover_is_called_out(self):
        def slow(self, query):
            body = rich_reply(query)
            if query.departure_token:
                for option in body["other_flights"]:
                    first = option["flights"][0]
                    first["arrival_airport"] = {"id": "ZRH", "time": f"{query.return_date} 10:00"}
                    onward = dict(first, departure_airport={"id": "ZRH", "time": f"{query.return_date} 17:15"})
                    onward["arrival_airport"] = {"id": "JFK", "time": f"{query.return_date} 20:30"}
                    option["flights"] = [first, onward]
            return body

        with mock.patch.object(FakeSerpApi, "search", slow):
            _, out, _ = self.run_cli(*FIXED, "--check")
        self.assertIn("back  LHR->JFK  Tue 19 Mar 09:00 -> 20:30  1 stop", out)
        self.assertRegex(out, r"Buy from Testair +same flights +one ticket; 7h 15m layover")


class RiskyOutputTest(CommandTest):
    def setUp(self):
        super().setUp()

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
                return SearchResult(query, (Offer(query, 90, (flight,), duration_min=400, hidden_city=True),))

        providers.register(Skipper)
        self.addCleanup(providers.unregister, "skipper")

    def test_a_hidden_city_fare_is_absent_by_default(self):
        _, out, _ = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        self.assertNotIn("Hopair", out)
        self.assertIn("Cheapest: $200 one way", out)

    def test_when_asked_for_it_is_listed_with_its_catch_beside_it(self):
        _, out, _ = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way", "--risky")
        self.assertRegex(out, r"\$90 .*Hopair .*hidden-city: you leave the trip at a stop")


class OtherSitesTest(CommandTest):
    def test_the_best_dates_come_with_a_link_to_each_other_site(self):
        _, out, _ = self.run_cli("search", "jfk,ewr", "LHR,LGW", "--depart", "2030-03-12", "--return", "2030-03-19")
        self.assertIn("Check and book: https://www.google.com/travel/flights?tfs=X", out)
        # Since 2026-10-09 a round trip's Momondo link leads the others; tests/test_next_step.py has the rest.
        self.assertIn(
            "Check this fare on Momondo: https://www.momondo.com/flight-search/JFK,EWR-LHR,LGW/2030-03-12/2030-03-19?sort=price_a",
            out,
        )
        self.assertIn("Same dates on Skyscanner: https://www.skyscanner.com/transport/flights/jfk/lhr/300312/300319/", out)
        self.assertIn(
            "Same dates on Skiplagged (lists hidden-city fares): https://skiplagged.com/flights/JFK/LHR/2030-03-12/2030-03-19",
            out,
        )
        self.assertIn(
            "Same dates on Kiwi.com (lists self-transfer fares): "
            "https://www.kiwi.com/deep?from=JFK&to=LHR&departure=2030-03-12&return=2030-03-19",
            out,
        )

    def test_one_way_links_leave_the_return_out(self):
        found = dict(links.other_sites(Query("JFK", "LHR", date(2030, 3, 12))))
        self.assertEqual(found["Momondo"], "https://www.momondo.com/flight-search/JFK-LHR/2030-03-12?sort=price_a")
        self.assertEqual(found["Skyscanner"], "https://www.skyscanner.com/transport/flights/jfk/lhr/300312/")
        self.assertEqual(found["Skiplagged (lists hidden-city fares)"], "https://skiplagged.com/flights/JFK/LHR/2030-03-12")
        self.assertEqual(
            found["Kiwi.com (lists self-transfer fares)"], "https://www.kiwi.com/deep?from=JFK&to=LHR&departure=2030-03-12"
        )

    def test_every_pattern_is_a_web_address_and_says_when_it_was_checked(self):
        self.assertEqual([site["name"] for site in links.sites()], ["Momondo", "Skyscanner", "Skiplagged", "Kiwi.com"])
        for site in links.sites():
            for kind in ("round_trip", "one_way"):
                self.assertTrue(site[kind].startswith("https://"), site["name"])
            self.assertIn("{return}", site["round_trip"])
            self.assertNotIn("{return}", site["one_way"])
            self.assertRegex(site["checked"], r"^\d{4}-\d\d-\d\d: ")

    def test_no_link_carries_a_logo_or_anything_but_the_route_and_dates(self):
        for label, url in links.other_sites(Query("JFK", "LHR", date(2030, 3, 12), date(2030, 3, 19), adults=3)):
            self.assertNotRegex(url, r"\.(png|svg|jpg|gif)")


class ExploreCommandTest(CommandTest):
    def setUp(self):
        super().setUp()
        FakeSerpApi.explore_reply = {
            "destinations": [
                {
                    "name": "Paris",
                    "country": "France",
                    "destination_airport": {"code": "CDG"},
                    "start_date": "2030-03-05",
                    "end_date": "2030-03-12",
                    "flight_price": 458,
                    "number_of_stops": 1,
                    "flight_duration": 615,
                    "airline": "Testair",
                },
                {"name": "London", "country": "United Kingdom", "destination_airport": {"code": "LGW"}, "start_date": "2030-03-03", "flight_price": 436},
            ]
        }

    def test_it_dry_runs_for_nothing_without_a_key(self):
        with mock.patch.dict(os.environ, {config.KEY_VAR: ""}):
            code, out, err = self.run_cli("explore", "MCO,SFB", "--area", "europe", "--month", "3", "--dry-run")
        self.assertEqual((code, out, err), (0, "", "1 search needed: 0 saved, 1 live.\n"))
        self.assertEqual(FakeSerpApi.searches, [])

    def test_it_lists_places_cheapest_first_and_says_they_are_suggestions(self):
        code, out, err = self.run_cli("explore", "mco,sfb", "--area", "europe", "--month", "3", "--length", "two-weeks")
        self.assertEqual(code, 0, err)
        (sent,) = FakeSerpApi.searches
        self.assertEqual((sent.origin, sent.area, sent.month, sent.duration), ("MCO,SFB", "/m/02j9z", 3, "two weeks"))
        self.assertIn("MCO,SFB -> Europe, March, two weeks", out)
        self.assertLess(out.index("London, United Kingdom"), out.index("Paris, France"))
        self.assertRegex(out, r"\$436   London, United Kingdom  LGW      from Sun 03 Mar ")
        self.assertRegex(out, r"\$458   Paris, France           CDG      Tue 05 Mar - Tue 12 Mar  1 stop  10h 15m  Testair")
        self.assertIn("These are the prices explore suggests. Search the dates to confirm one", out)
        self.assertIn("About 249 SerpApi searches left this month.", out)

    def test_repeating_it_spends_nothing_and_marks_the_prices_saved(self):
        self.run_cli("explore", "MCO")
        code, out, err = self.run_cli("explore", "MCO")
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertIn("1 search needed: 1 saved, 0 live.", err)
        self.assertEqual(out.count("(saved)"), 2)
        self.assertIn("MCO -> anywhere, any time in the next six months, a week", out)

    def test_one_place_can_be_asked_about(self):
        self.run_cli("explore", "MCO", "--to", "waw", "--length", "weekend")
        self.assertEqual((FakeSerpApi.searches[0].destination, FakeSerpApi.searches[0].duration), ("WAW", "weekend"))

    def test_a_real_reply_for_one_place_lists_each_airlines_price_and_the_link(self):
        fixture = Path(__file__).parent / "fixtures" / "explore_one_place_january.json"
        FakeSerpApi.explore_reply = json.loads(fixture.read_text(encoding="utf-8"))
        code, out, err = self.run_cli("explore", "BOS", "--to", "WAW", "--month", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("BOS -> WAW, January, a week", out)
        self.assertRegex(
            out, r"\$611   Warsaw Chopin Airport  WAW      Mon 25 Jan - Wed 03 Feb  1 stop  36h 55m  Tap Air Portugal"
        )
        self.assertRegex(out, r"\$694   Warsaw Chopin Airport  WAW      Mon 25 Jan - Wed 03 Feb  1 stop  10h 40m  Delta, KLM")
        self.assertEqual(out.count("Check these dates: https://www.google.com/travel/flights?"), 1)
        # The reply says when SerpApi fetched it, and that is the time shown: 9 October, 02:53 UTC.
        self.assertRegex(out, STAMP)

    def test_a_list_of_places_is_refused(self):
        code, _, err = self.run_cli("explore", "MCO", "--to", "WAW,KRK")
        self.assertEqual(code, 1)
        self.assertIn("--to takes one airport", err)

    def test_an_empty_reply_is_said_plainly(self):
        FakeSerpApi.explore_reply = {"search_metadata": {"status": "Success"}}
        _, out, _ = self.run_cli("explore", "MCO")
        self.assertIn("No destinations found.", out)

    def test_it_refuses_when_the_month_cannot_cover_it(self):
        FakeSerpApi.account_reply["total_searches_left"] = 0
        code, _, err = self.run_cli("explore", "MCO")
        self.assertEqual(code, 1)
        self.assertIn("needs 1 live searches but only 0 are left this month", err)
        self.assertEqual(FakeSerpApi.searches, [])


if __name__ == "__main__":
    unittest.main()
