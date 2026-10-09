import contextlib
import io
import os
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from flightsleuth import bakeoff, cli, config
from flightsleuth.errors import UserError
from flightsleuth.history import History
from tests.fakes import FakeSerpApi, install
from tests.test_window import valley

TODAY = date(2030, 1, 1)
# The owner's file, with the dates moved to where the fake's fares live.
ROUTES = """\
# FlightSleuth measurement routes. Most important first; a short run uses the top six.
# origin airports to destination airports | departure window | trip length


MCO,SFB to JFK,EWR,LGA | depart between 2030-03-10 and 2030-04-20 | 2 to 4 nights
BOS to WAW | depart between 2030-03-10 and 2030-04-28 | 7 to 14 nights
MCO,SFB to PVG,SHA | depart between 2030-03-01 and 2030-04-20 | 10 to 16 nights
MCO,SFB to DCA,IAD,BWI | depart between 2030-03-05 and 2030-03-30 | 2 to 4 nights
BOS to LAX,BUR,SNA,LGB,ONT | depart between 2030-03-15 and 2030-05-15 | 4 to 7 nights
MCO,SFB to WAW | depart between 2030-04-01 and 2030-05-15 | 7 to 14 nights
jfk to lhr | depart between 2030-04-01 and 2030-04-30 | 7 nights
JFK to LHR | depart between 2030-04-01 and 2030-04-30 | one way
"""


def write(text, name="routes.txt"):
    path = Path(name)
    path.write_text(text, encoding="utf-8")
    return path


def one_route(length="5 to 9 nights"):
    return bakeoff.read_routes(write(f"JFK to LHR | depart between 2030-03-01 and 2030-05-30 | {length}\n"))[0]


def rows(table):
    history = History(config.history_path())
    try:
        return getattr(history, table)()
    finally:
        history.close()


class RouteFileTest(unittest.TestCase):
    def setUp(self):
        install(self)

    def test_the_owners_format_reads_in_file_order(self):
        routes = bakeoff.read_routes(write(ROUTES))
        self.assertEqual(len(routes), 8)
        first = routes[0]
        self.assertEqual((first.origin, first.destination), ("MCO,SFB", "JFK,EWR,LGA"))
        self.assertEqual((first.earliest, first.latest, first.nights), (date(2030, 3, 10), date(2030, 4, 20), (2, 4)))
        self.assertEqual(first.label, "MCO,SFB to JFK,EWR,LGA")
        self.assertEqual(routes[4].destination, "LAX,BUR,SNA,LGB,ONT")

    def test_a_single_length_and_a_one_way_are_understood(self):
        routes = bakeoff.read_routes(write(ROUTES))
        self.assertEqual((routes[6].origin, routes[6].nights), ("JFK", (7, 7)))
        self.assertIsNone(routes[7].nights)

    def test_a_route_becomes_a_window_search_with_the_closer_look_on(self):
        request = bakeoff.read_routes(write(ROUTES))[1].request()
        self.assertEqual((request.window, request.nights, request.check), ((date(2030, 3, 10), date(2030, 4, 28)), (7, 14), True))

    def test_a_line_that_does_not_fit_is_reported_with_its_number(self):
        for bad, why in (
            ("JFK LHR | depart between 2030-03-01 and 2030-03-09 | 7 nights", "expected"),
            ("JFK to London | depart between 2030-03-01 and 2030-03-09 | 7 nights", "not an airport list"),
            ("JFK to LHR | depart between 2030-03-01 and March 9 | 7 nights", "expected"),
            ("JFK to LHR | depart between 2030-03-01 and 2030-02-30 | 7 nights", "day is out of range"),
            ("JFK to LHR | depart between 2030-03-01 and 2030-03-09 | a week", "not a trip length"),
            ("JFK to LHR | depart between 2030-03-01 and 2030-03-09 | 9 to 5 nights", "runs backwards"),
        ):
            with self.assertRaisesRegex(UserError, rf"routes.txt line 3: .*{why}", msg=bad):
                bakeoff.read_routes(write(f"# header\n\n{bad}\n"))

    def test_a_missing_or_empty_file_is_said_plainly(self):
        with self.assertRaisesRegex(UserError, "cannot read the route file"):
            bakeoff.read_routes(Path("nowhere.txt"))
        with self.assertRaisesRegex(UserError, "routes.txt has no routes in it"):
            bakeoff.read_routes(write("# nothing yet\n"))


class BudgetTest(unittest.TestCase):
    def setUp(self):
        install(self)
        self.routes = bakeoff.read_routes(write(ROUTES))[:6]

    def test_six_routes_fit_the_150_searches_set_aside(self):
        plans = bakeoff.plan(self.routes, today=TODAY)
        self.assertEqual([plan.live for _, plan in plans], [24] * 6)
        self.assertEqual(bakeoff.ceiling(plans), 150)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_a_smaller_budget_is_shared_out_evenly(self):
        plans = bakeoff.plan(self.routes, budget=90, today=TODAY)
        self.assertTrue(all(plan.live <= 14 for _, plan in plans))
        self.assertLessEqual(bakeoff.ceiling(plans), 90)

    def test_a_budget_too_small_to_share_is_refused(self):
        with self.assertRaisesRegex(UserError, "a budget of 6 live searches is too small for 6 routes"):
            bakeoff.plan(self.routes, budget=6, today=TODAY)

    def test_no_route_gets_more_than_one_runs_cap_however_big_the_budget(self):
        plans = bakeoff.plan(self.routes[:2], budget=1000, today=TODAY)
        self.assertEqual([plan.live for _, plan in plans], [24, 24])


class RecordTest(unittest.TestCase):
    def setUp(self):
        install(self)
        FakeSerpApi.rich = True
        FakeSerpApi.fare = valley
        self.said = []

    def run_route(self, route=None, **options):
        route = route or one_route()
        ((_, search),) = bakeoff.plan([route], budget=25, today=TODAY)
        return bakeoff.run_route("run-1", route, search, say=self.said.append, **options)

    def fares(self):
        return {row["measure"]: row for row in rows("bakeoff_fares")}

    def test_the_confirmed_fare_is_the_baseline(self):
        outcome = self.run_route()
        base = self.fares()["baseline"]
        self.assertEqual((base["run"], base["route"], base["price"], base["currency"]), ("run-1", "JFK to LHR", 420, "USD"))
        self.assertEqual((base["depart_date"], base["return_date"]), ("2030-04-18", "2030-04-25"))
        self.assertEqual((base["how"], base["detail"], base["same_flights"]), ("live", "TA 1 / TA 2", None))
        self.assertEqual(base["price"], outcome.checks[0].confirmed.price)
        self.assertLessEqual(len(FakeSerpApi.searches), 24)

    def test_two_one_ways_and_the_other_market_are_measured_against_it(self):
        FakeSerpApi.market_savings = {"gb": 12}
        self.run_route()
        fares = self.fares()
        self.assertEqual(set(fares), {"baseline", "split", "market:gb"})
        self.assertEqual((fares["split"]["price"], fares["split"]["same_flights"]), (900, 0))
        self.assertEqual((fares["market:gb"]["price"], fares["market:gb"]["same_flights"]), (408, 1))
        self.assertEqual(fares["split"]["detail"], "TA 1 + TA 1")

    def test_sellers_are_not_measures_but_a_separate_ticket_fare_records_its_one_ticket_price(self):
        self.run_route()
        self.assertNotIn("one ticket", self.fares())
        FakeSerpApi.separate = True
        self.run_route(bakeoff.read_routes(write("BOS to LHR | depart between 2030-03-01 and 2030-05-30 | 5 to 9 nights\n"))[0])
        whole = [row for row in rows("bakeoff_fares") if row["measure"] == "one ticket"]
        self.assertEqual([(row["route"], row["price"], row["same_flights"], row["detail"]) for row in whole], [("BOS to LHR", 445, 1, "sold by Cheapo Travel")])

    def test_a_good_return_lookup_is_counted(self):
        self.run_route()
        (lookup,) = rows("bakeoff_lookups")
        self.assertEqual((lookup["found"], lookup["cheapest"], lookup["in_unsorted"], lookup["price"]), (1, 1, None, 420))

    def test_a_failed_lookup_is_counted_and_googles_own_order_is_asked_whether_it_lists_that_fare(self):
        FakeSerpApi.phantoms = 1
        self.run_route()
        missed, held = rows("bakeoff_lookups")
        self.assertEqual((missed["found"], missed["cheapest"], missed["in_unsorted"], missed["itinerary_key"][:3]), (0, 1, 0, "PH0"))
        self.assertEqual((held["found"], held["cheapest"]), (1, 0))
        probe = FakeSerpApi.searches[-1]
        self.assertFalse(probe.by_price)
        self.assertEqual((probe.depart_date.isoformat(), probe.return_date.isoformat()), (missed["depart_date"], missed["return_date"]))
        # The route's own searches and the one probe together stay inside the route's share.
        self.assertLessEqual(len(FakeSerpApi.searches), 25)

    def test_the_probe_is_only_made_when_a_lookup_fails(self):
        self.run_route()
        self.assertTrue(all(query.by_price for query in FakeSerpApi.searches))

    def test_a_one_way_route_has_a_baseline_and_no_return_lookup(self):
        self.run_route(one_route("one way"))
        self.assertEqual(self.fares()["baseline"]["return_date"], None)
        self.assertEqual(rows("bakeoff_lookups"), [])

    def test_each_route_is_reported_as_it_finishes_with_its_fetch_time(self):
        self.run_route()
        self.assertRegex(self.said[0], r"^JFK to LHR: \$420 confirmed, Thu 18 Apr - Thu 25 Apr, TA 1 / TA 2, fetched \d\d \w{3} \d\d:\d\d\n")
        self.assertIn("Two one-way tickets", self.said[0])

    def test_prices_read_off_a_site_by_eye_are_recorded_as_typed(self):
        answers = iter(["abc", "$1,234.50", "y", "", "300", "n", "250", ""])
        opened = []
        self.run_route(ask=lambda prompt: next(answers), open_link=opened.append)
        fares = self.fares()
        self.assertEqual((fares["Momondo"]["price"], fares["Momondo"]["same_flights"], fares["Momondo"]["how"]), (1234.5, 1, "typed"))
        self.assertNotIn("Skyscanner", fares)
        self.assertEqual((fares["Skiplagged"]["price"], fares["Skiplagged"]["same_flights"]), (300, 0))
        self.assertEqual((fares["Kiwi.com"]["price"], fares["Kiwi.com"]["same_flights"]), (250, None))
        self.assertEqual(len(opened), 4)
        self.assertTrue(opened[0].startswith("https://www.momondo.com/flight-search/JFK-LHR/2030-04-18/2030-04-25"))
        self.assertIn("  'abc' is not a price. Type a number such as 412, or press Enter to skip.", self.said)
        self.assertIn("  Skiplagged (lists hidden-city fares): https://skiplagged.com/flights/JFK/LHR/2030-04-18/2030-04-25", self.said)

    def test_without_someone_to_ask_no_site_is_opened(self):
        with mock.patch("webbrowser.open") as opened:
            self.run_route()
        opened.assert_not_called()


class ReportTest(unittest.TestCase):
    def setUp(self):
        install(self)
        FakeSerpApi.rich = True
        FakeSerpApi.fare = valley

    def bake(self, run, text, **options):
        routes = bakeoff.read_routes(write(text))
        for route, search in bakeoff.plan(routes, budget=25 * len(routes), today=TODAY):
            bakeoff.run_route(run, route, search, say=lambda line: None, **options)

    def test_nothing_recorded_is_said_plainly(self):
        self.assertEqual(bakeoff.report(), "No bake-off has been recorded yet.")

    def test_it_counts_how_often_and_by_how_much_each_measure_beat_serpapi(self):
        FakeSerpApi.market_savings = {"gb": 42}
        answers = iter(["399", "y", "", "350", "n", "500", ""])
        self.bake("run-1", "JFK to LHR | depart between 2030-03-01 and 2030-05-30 | 5 to 9 nights\n", ask=lambda p: next(answers))
        text = bakeoff.report()
        self.assertIn("Bake-off report: 1 route\n", text)
        self.assertRegex(text, r"JFK to LHR  Thu 18 Apr - Thu 25 Apr  \$420     TA 1 / TA 2  \d\d \w{3} \d\d:\d\d")
        self.assertIn("Against SerpApi's fare for the same dates:", text)
        self.assertRegex(text, r"As sold in GB +1 +1 +1, saving \$42 \(10%\) on average +- +-")
        self.assertRegex(text, r"Two one-way tickets +1 +0 +- +- +-")
        self.assertRegex(text, r"Momondo \(typed\) +1 +1 +1, saving \$21 \(5%\) on average +- +-")
        self.assertRegex(text, r"Skiplagged \(typed\) +1 +1 +- +1, saving \$70 \(17%\) on average +-")
        self.assertRegex(text, r"Kiwi.com \(typed\) +1 +0 +- +- +-")
        self.assertIn("Return lookups: the cheapest fare listed had no return flights on 0 of 1 routes.", text)
        self.assertNotIn("Google's own order", text)

    def test_it_reports_failed_lookups_and_whether_dropping_sort_by_would_have_avoided_them(self):
        FakeSerpApi.phantoms = 1
        self.bake(
            "run-1",
            "JFK to LHR | depart between 2030-03-01 and 2030-05-30 | 5 to 9 nights\n"
            "BOS to LHR | depart between 2030-03-01 and 2030-05-30 | 5 to 9 nights\n",
        )
        text = bakeoff.report()
        self.assertIn("Return lookups: the cheapest fare listed had no return flights on 2 of 2 routes.", text)
        self.assertIn("Google's own order (no sort_by) left that fare out 2 of 2 times, so it would have avoided 2 of those misses.", text)

    def test_it_reports_how_often_the_cheapest_fare_was_two_tickets_and_what_one_ticket_costs(self):
        FakeSerpApi.separate = True
        self.bake("run-1", "JFK to LHR | depart between 2030-03-01 and 2030-05-30 | 5 to 9 nights\n")
        text = bakeoff.report()
        self.assertIn(
            "Separate tickets: SerpApi's cheapest fare was two separate tickets on 1 of 1 routes. "
            "The same flights on one ticket cost $25 more on average.",
            text,
        )
        self.assertNotIn("one ticket ", text.split("Separate tickets")[0])

    def test_only_each_routes_latest_run_is_reported(self):
        line = "JFK to LHR | depart between 2030-03-01 and 2030-05-30 | 5 to 9 nights\n"
        FakeSerpApi.market_savings = {"gb": 42}
        self.bake("run-1", line)
        FakeSerpApi.market_savings = {}
        routes = bakeoff.read_routes(write(line))
        for route, search in bakeoff.plan(routes, budget=25, today=TODAY, fresh=True):
            bakeoff.run_route("run-2", route, search, say=lambda text: None)
        self.assertEqual({row["run"] for row in rows("bakeoff_fares")}, {"run-1", "run-2"})
        text = bakeoff.report()
        self.assertIn("Bake-off report: 1 route\n", text)
        self.assertRegex(text, r"As sold in GB +1 +0 ")


class BakeoffCommandTest(unittest.TestCase):
    def setUp(self):
        install(self)
        FakeSerpApi.rich = True
        FakeSerpApi.fare = valley
        write(ROUTES)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_dry_run_prices_the_top_six_routes_for_nothing(self):
        with mock.patch.dict(os.environ, {config.KEY_VAR: ""}):
            code, out, err = self.run_cli("bakeoff", "routes.txt", "--dry-run")
        self.assertEqual(code, 0, err)
        lines = err.splitlines()
        self.assertEqual(len(lines), 7)
        self.assertTrue(lines[0].startswith("MCO,SFB to JFK,EWR,LGA: Up to 24 searches needed: 12 to sample the window"))
        self.assertTrue(lines[5].startswith("MCO,SFB to WAW: "))
        self.assertIn("6 routes: up to 150 live searches in all.", lines[6])
        self.assertEqual((out, FakeSerpApi.searches), ("", []))
        self.assertFalse(config.history_path().exists())

    def test_every_route_in_the_file_can_be_asked_for(self):
        code, _, err = self.run_cli("bakeoff", "routes.txt", "--routes", "0", "--budget", "200", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("8 routes: up to ", err)

    def test_one_route_can_be_picked_out_of_the_file_by_its_position(self):
        with mock.patch.dict(os.environ, {config.KEY_VAR: ""}):
            code, out, err = self.run_cli("bakeoff", "routes.txt", "--only", "2", "--dry-run")
        self.assertEqual(code, 0, err)
        lines = err.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("BOS to WAW: Up to 24 searches needed"))
        self.assertTrue(lines[1].startswith("1 route: up to 25 live searches in all."))
        self.assertEqual(FakeSerpApi.searches, [])

    def test_that_one_route_runs_alone_and_stays_inside_one_runs_limit(self):
        code, out, err = self.run_cli("bakeoff", "routes.txt", "--only", "2", "--yes")
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(FakeSerpApi.searches), 25)
        self.assertEqual({(query.origin, query.destination) for query in FakeSerpApi.searches}, {("BOS", "WAW"), ("WAW", "BOS")})
        self.assertIn("BOS to WAW: $", out)
        self.assertIn("Bake-off report: 1 route\n", out)
        self.assertEqual({row["route"] for row in rows("bakeoff_fares")}, {"BOS to WAW"})

    def test_a_single_route_keeps_its_share_small_when_the_budget_is(self):
        code, _, err = self.run_cli("bakeoff", "routes.txt", "--only", "1", "--budget", "15", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("1 route: up to 15 live searches in all.", err)

    def test_a_position_that_is_not_in_the_file_is_refused(self):
        for position in ("0", "9"):
            code, _, err = self.run_cli("bakeoff", "routes.txt", "--only", position, "--dry-run")
            self.assertEqual(code, 1)
            self.assertIn("routes.txt has 8 routes, so --only takes a number from 1 to 8", err)

    def test_picking_one_route_and_taking_the_first_few_cannot_be_combined(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            cli.main(["bakeoff", "routes.txt", "--only", "2", "--routes", "3"])
        self.assertIn("not allowed with argument", err.getvalue())

    def test_it_will_not_spend_without_a_yes(self):
        code, out, err = self.run_cli("bakeoff", "routes.txt", "--routes", "1")
        self.assertEqual(code, 1)
        self.assertIn("add --yes to spend up to 25 live searches, or --dry-run to see the plan", err)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_a_typed_no_spends_nothing(self):
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch("sys.stdin", tty), mock.patch("builtins.input", return_value="no"):
            code, _, err = self.run_cli("bakeoff", "routes.txt", "--routes", "1")
        self.assertEqual(code, 1)
        self.assertIn("nothing was spent", err)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_with_a_yes_it_runs_each_route_inside_its_share_and_prints_the_report(self):
        code, out, err = self.run_cli("bakeoff", "routes.txt", "--routes", "2", "--yes")
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(FakeSerpApi.searches), 50)
        self.assertIn("MCO,SFB to JFK,EWR,LGA: $", out)
        self.assertIn("Bake-off report: 2 routes", out)
        self.assertIn("Two one-way tickets", out)
        # The last route's own count: each run asks the account afresh, and the fake's never goes down.
        self.assertRegex(out, r"About \d+ SerpApi searches left this month\.")

    def test_the_report_can_be_printed_again_for_nothing(self):
        self.run_cli("bakeoff", "routes.txt", "--routes", "1", "--yes")
        spent = len(FakeSerpApi.searches)
        code, out, _ = self.run_cli("bakeoff", "--report")
        self.assertEqual(code, 0)
        self.assertIn("Bake-off report: 1 route", out)
        self.assertEqual(len(FakeSerpApi.searches), spent)

    def test_running_it_again_within_the_hour_replays_for_nothing_and_needs_no_yes(self):
        self.run_cli("bakeoff", "routes.txt", "--routes", "1", "--yes")
        spent = len(FakeSerpApi.searches)
        code, out, err = self.run_cli("bakeoff", "routes.txt", "--routes", "1", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(FakeSerpApi.searches), spent)

    def test_a_route_tried_alone_replays_for_nothing_in_the_full_run_hours_later(self):
        FakeSerpApi.phantoms = 1  # so the unsorted probe is part of what has to replay
        self.run_cli("bakeoff", "routes.txt", "--only", "2", "--yes")
        alone = len(FakeSerpApi.searches)
        db = sqlite3.connect(config.cache_path())
        with db:
            db.execute("UPDATE responses SET fetched_at = fetched_at - 3 * 3600")
        db.close()

        code, out, err = self.run_cli("bakeoff", "routes.txt", "--routes", "3", "--yes")
        self.assertEqual(code, 0, err)
        again = FakeSerpApi.searches[alone:]
        self.assertEqual([query for query in again if "WAW" in (query.origin, query.destination)], [])
        self.assertRegex(out, r"BOS to WAW: \$\d+ confirmed, .* \(saved\)")
        self.assertIn("Bake-off report: 3 routes", out)

    def test_running_into_the_hourly_limit_keeps_what_finished_and_says_how_to_carry_on(self):
        FakeSerpApi.account_reply["account_rate_limit_per_hour"] = 30
        real = FakeSerpApi.account

        def counting(self):
            return {**real(self), "this_hour_searches": len(FakeSerpApi.searches)}

        with mock.patch.object(FakeSerpApi, "account", counting):
            code, out, err = self.run_cli("bakeoff", "routes.txt", "--routes", "3", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("stopped at BOS to WAW, after 1 of 3 routes: this scan needs 24 live searches but only", err)
        self.assertIn("Run the same command again to carry on", err)
        self.assertIn("Bake-off report: 1 route\n", out)
        spent = len(FakeSerpApi.searches)

        # An hour later the limit has reset: the finished route replays, and only the others are paid for.
        with mock.patch.object(FakeSerpApi, "account", lambda self: {**real(self), "account_rate_limit_per_hour": 100}):
            code, out, err = self.run_cli("bakeoff", "routes.txt", "--routes", "3", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("MCO,SFB to JFK,EWR,LGA: Up to 24 searches needed", err)
        self.assertIn("Bake-off report: 3 routes", out)
        self.assertRegex(out, r"MCO,SFB to JFK,EWR,LGA .* \(saved\)")
        self.assertLessEqual(len(FakeSerpApi.searches) - spent, 50)

    def test_the_route_file_has_to_be_named(self):
        code, _, err = self.run_cli("bakeoff")
        self.assertEqual(code, 1)
        self.assertIn("name the route file", err)


if __name__ == "__main__":
    unittest.main()
