import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from flightsleuth import cli, config
from tests.fakes import FakeSerpApi, install


class CliTest(unittest.TestCase):
    def setUp(self):
        self.home = install(self)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_flexible_scan_finds_the_cheapest_dates(self):
        code, out, err = self.run_cli(
            "search", "jfk,ewr", "LHR", "--depart", "2030-03-12", "--return", "2030-03-19", "--flex", "2"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(FakeSerpApi.searches), 5)
        self.assertIn("5 searches needed: 0 saved, 5 live.", err)
        self.assertIn("Searching .....\n", err)
        self.assertIn("JFK,EWR -> LHR, round trip, 1 adult, economy", out)
        self.assertIn("Mon 11 Mar  Mon 18 Mar  $455        <- cheapest", out)
        # Its return flights were never fetched, so the line may not call it the cheapest outright.
        self.assertIn("Cheapest listed: $455 round trip, Mon 11 Mar - Mon 18 Mar (not yet confirmed)\n", out)
        self.assertNotIn("Cheapest: ", out)
        self.assertIn("Google rates $455 as low for those dates (typical range $500 to $800).", out)
        self.assertIn("Check and book: https://www.google.com/travel/flights?tfs=X", out)
        self.assertIn("About 245 SerpApi searches left this month.", out)

    def test_repeating_a_scan_spends_nothing(self):
        argv = ("search", "JFK", "LHR", "--depart", "2030-03-12", "--nights", "7", "--flex", "1")
        self.run_cli(*argv)
        self.assertEqual(len(FakeSerpApi.searches), 3)
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(FakeSerpApi.searches), 3)
        self.assertIn("3 searches needed: 3 saved, 0 live.", err)
        self.assertIn("Cheapest listed: $455 round trip", out)

    def test_fresh_ignores_saved_results(self):
        argv = ("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        self.run_cli(*argv)
        self.run_cli(*argv, "--fresh")
        self.assertEqual(len(FakeSerpApi.searches), 2)

    def test_dry_run_needs_no_key_and_spends_nothing(self):
        with mock.patch.dict(os.environ, {config.KEY_VAR: ""}):
            code, out, err = self.run_cli(
                "search", "JFK", "LHR", "--depart", "2030-03-12", "--nights", "5-8", "--flex", "2", "--dry-run"
            )
        self.assertEqual(code, 0, err)
        self.assertIn("20 searches needed: 0 saved, 20 live.", err)
        self.assertEqual(FakeSerpApi.searches, [])
        self.assertEqual(out, "")

    def test_refuses_a_scan_over_the_per_run_limit(self):
        code, _, err = self.run_cli(
            "search", "JFK", "LHR", "--depart", "2030-03-12", "--nights", "5-8", "--flex", "3"
        )
        self.assertEqual(code, 1)
        self.assertIn("over the limit of 25 live searches", err)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_refuses_when_the_month_cannot_cover_it(self):
        FakeSerpApi.account_reply["total_searches_left"] = 2
        code, _, err = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way", "--flex", "1")
        self.assertEqual(code, 1)
        self.assertIn("needs 3 live searches but only 2 are left this month", err)
        self.assertNotIn("Searching", err)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_refuses_when_the_hour_cannot_cover_it(self):
        FakeSerpApi.account_reply["this_hour_searches"] = 49
        code, _, err = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way", "--flex", "1")
        self.assertEqual(code, 1)
        self.assertIn("only 1 more are allowed this hour", err)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_split_spots_two_one_ways_beating_the_round_trip(self):
        code, out, err = self.run_cli(
            "search", "JFK", "LHR", "--depart", "2030-03-12", "--return", "2030-03-19", "--split"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(FakeSerpApi.searches), 3)
        self.assertIn("Tue 12 Mar  Tue 19 Mar  $520        $400          <- cheapest", out)
        self.assertIn("Cheapest: $400 as two one-way tickets, Tue 12 Mar - Tue 19 Mar", out)
        self.assertIn("back  $200   LHR->JFK  Tue 19 Mar", out)
        # The round trip lost, so its price rating and link would describe the wrong ticket.
        self.assertNotIn("Google rates", out)
        self.assertIn("Check and book the flight out:", out)
        self.assertIn("Check and book the flight back:", out)

    def test_split_shares_one_way_searches_between_date_pairs(self):
        # Nights 6-7 from one departure: two round trips, one shared outbound, two returns.
        code, _, err = self.run_cli(
            "search", "JFK", "LHR", "--depart", "2030-03-12", "--nights", "6-7", "--split", "--dry-run"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("5 searches needed", err)

    def test_throttling_stops_the_run_and_keeps_what_finished(self):
        FakeSerpApi.fail_after = 2
        argv = ("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way", "--flex", "2")
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 1)
        self.assertIn("stopped after 2 of 5 live searches: Your account has run out of searches.", err)
        self.assertEqual(out, "")

        FakeSerpApi.fail_after = None
        code, _, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        self.assertIn("5 searches needed: 2 saved, 3 live.", err)

    def test_missing_key_is_explained(self):
        with mock.patch.dict(os.environ, {config.KEY_VAR: ""}):
            code, _, err = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        self.assertEqual(code, 1)
        self.assertIn("no API key found", err)

    def test_no_fares_is_said_plainly(self):
        empty = {"search_metadata": {"status": "Success"}, "error": "Google Flights hasn't returned any results."}
        with mock.patch.object(FakeSerpApi, "search", lambda self, query: empty):
            code, out, _ = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        self.assertEqual(code, 0)
        self.assertIn("No fares found.", out)

    def test_past_departure_is_rejected_before_any_search(self):
        code, _, err = self.run_cli("search", "JFK", "LHR", "--depart", "2020-01-01", "--one-way")
        self.assertEqual(code, 1)
        self.assertIn("the departure date is in the past", err)

    def test_return_before_departure_is_rejected(self):
        code, _, err = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--return", "2030-03-01")
        self.assertEqual(code, 1)
        self.assertIn("the return date is before the departure date", err)

    def test_airport_lists_are_tidied(self):
        code, out, err = self.run_cli("search", "jfk, ewr", "lhr", "--depart", "2030-03-12", "--one-way")
        self.assertEqual(code, 0, err)
        self.assertIn("JFK,EWR -> LHR", out)

    def test_gui_command_explains_a_python_without_tkinter(self):
        with mock.patch.dict(sys.modules, {"flightsleuth.gui": None}):
            code, _, err = self.run_cli("gui")
        self.assertEqual(code, 1)
        self.assertIn("the window needs tkinter", err)

    def test_cache_command_counts_and_clears(self):
        self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        _, out, _ = self.run_cli("cache")
        self.assertIn("1 saved search in", out)
        _, out, _ = self.run_cli("cache", "--clear")
        self.assertIn("Removed 1 saved search.", out)


if __name__ == "__main__":
    unittest.main()
