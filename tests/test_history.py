import contextlib
import io
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from flightsleuth import cli, config, providers, scan
from flightsleuth.errors import UserError
from tests.fakes import FakeSerpApi, history_rows, install, reply
from tests.test_providers import CheapSource


def request(**changes):
    return scan.Request("JFK,EWR", "LHR", date(2030, 3, 12), **changes)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        install(self)

    def search(self, **changes):
        return scan.run(scan.plan(request(**changes)))

    def test_history_lives_beside_the_cache_in_its_own_file(self):
        self.search()
        self.assertEqual(config.history_path().parent, config.cache_path().parent)
        self.assertEqual(config.history_path().name, "history.db")
        self.assertTrue(config.history_path().is_file())

    def test_a_live_reply_records_its_cheapest_fare(self):
        before = datetime.now(timezone.utc) - timedelta(seconds=2)
        self.search(return_date=date(2030, 3, 19), adults=2, cabin="business", stops="nonstop")
        (point,) = history_rows("price_points")
        when = datetime.fromisoformat(point.pop("fetched_at"))
        self.assertTrue(before <= when <= datetime.now(timezone.utc) + timedelta(seconds=2))
        self.assertEqual(
            point,
            {
                "id": 1,
                "origin": "JFK,EWR",
                "destination": "LHR",
                "depart_date": "2030-03-12",
                "return_date": "2030-03-19",
                "adults": 2,
                "cabin": "business",
                "stops": "nonstop",
                "price": 520,
                "currency": "USD",
                "source": "serpapi",
                "itinerary_key": "@2030-03-12T18:30",
            },
        )

    def test_replaying_a_saved_reply_adds_no_price_point(self):
        self.search(flex=1)
        self.assertEqual(len(history_rows("price_points")), 3)
        self.search(flex=1)
        self.search(flex=1)
        self.assertEqual(len(FakeSerpApi.searches), 3)
        self.assertEqual(len(history_rows("price_points")), 3)

    def test_only_the_new_dates_of_a_wider_scan_add_price_points(self):
        self.search()
        self.search(flex=1)
        self.assertEqual(
            [point["depart_date"] for point in history_rows("price_points")], ["2030-03-12", "2030-03-11", "2030-03-13"]
        )

    def test_price_point_carries_the_time_the_source_really_fetched(self):
        def stamped(self, query):
            metadata = {"status": "Success", "processed_at": "2030-01-01 12:00:00 UTC"}
            return {**reply(query), "search_metadata": metadata}

        with mock.patch.object(FakeSerpApi, "search", stamped):
            self.search()
        self.assertEqual(history_rows("price_points")[0]["fetched_at"], "2030-01-01T12:00:00+00:00")

    def test_one_way_legs_of_a_split_search_are_recorded_too(self):
        self.search(return_date=date(2030, 3, 19), split=True)
        points = {(p["origin"], p["destination"], p["depart_date"], p["return_date"]): p["price"] for p in history_rows("price_points")}
        self.assertEqual(
            points,
            {
                ("JFK,EWR", "LHR", "2030-03-12", "2030-03-19"): 520,
                ("JFK,EWR", "LHR", "2030-03-12", None): 200,
                ("LHR", "JFK,EWR", "2030-03-19", None): 200,
            },
        )

    def test_a_reply_with_no_fares_records_the_call_but_no_price(self):
        empty = {"search_metadata": {"status": "Success"}, "error": "Google Flights hasn't returned any results."}
        with mock.patch.object(FakeSerpApi, "search", lambda self, query: empty):
            self.search()
        self.assertEqual(history_rows("price_points"), [])
        self.assertEqual([(call["live"], call["status"]) for call in history_rows("provider_calls")], [(1, "ok")])

    def test_every_call_is_logged_as_live_or_cached(self):
        self.search()
        self.search()
        live, cached = history_rows("provider_calls")
        self.assertEqual(
            (live["provider"], live["endpoint"], live["live"], live["cost"], live["status"]),
            ("serpapi", "search", 1, 1, "ok"),
        )
        self.assertIsInstance(live["latency_ms"], int)
        self.assertGreaterEqual(live["latency_ms"], 0)
        self.assertEqual(
            (cached["provider"], cached["endpoint"], cached["live"], cached["cost"], cached["latency_ms"], cached["status"]),
            ("serpapi", "search", 0, 0, None, "ok"),
        )
        self.assertTrue(datetime.fromisoformat(live["called_at"]) <= datetime.fromisoformat(cached["called_at"]))

    def test_a_failed_call_is_logged_free_and_earlier_fares_are_kept(self):
        FakeSerpApi.fail_after = 2
        with self.assertRaisesRegex(UserError, "stopped after 2 of 5"):
            self.search(flex=2)
        calls = history_rows("provider_calls")
        self.assertEqual([call["status"] for call in calls], ["ok", "ok", "RateLimited"])
        self.assertEqual([call["cost"] for call in calls], [1, 1, 0])
        self.assertEqual(len(history_rows("price_points")), 2)

        # The two finished replies now come from the cache: no second price point for either.
        FakeSerpApi.fail_after = None
        self.search(flex=2)
        self.assertEqual(len(history_rows("price_points")), 5)
        self.assertEqual(len({point["depart_date"] for point in history_rows("price_points")}), 5)

    def test_each_source_is_recorded_under_its_own_name_and_cost(self):
        CheapSource.searches = []
        providers.register(CheapSource)
        self.addCleanup(providers.unregister, CheapSource.name)
        self.search()
        self.assertEqual(
            {(point["source"], point["price"], point["itinerary_key"]) for point in history_rows("price_points")},
            {("serpapi", 200, "@2030-03-12T18:30"), ("cheapsource", 99, "ZZ1@2030-03-12T06:00")},
        )
        self.assertEqual(
            {(call["provider"], call["cost"]) for call in history_rows("provider_calls")},
            {("serpapi", 1), ("cheapsource", 0)},
        )

    def test_planning_and_refused_runs_write_nothing(self):
        scan.plan(request(flex=2))
        FakeSerpApi.account_reply["total_searches_left"] = 0
        with self.assertRaises(UserError):
            self.search()
        with self.assertRaises(scan.OverCap):
            self.search(flex=20)
        self.assertFalse(config.history_path().exists())

    def test_clearing_the_cache_leaves_the_history_alone(self):
        self.search(flex=1)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["cache", "--clear"])
        self.assertIn("Removed 3 saved searches.", out.getvalue())
        self.assertEqual(len(history_rows("price_points")), 3)
        self.assertEqual(len(history_rows("provider_calls")), 3)

    def test_rows_cannot_be_changed_or_deleted(self):
        self.search()
        db = sqlite3.connect(config.history_path())
        self.addCleanup(db.close)
        for statement in (
            "UPDATE price_points SET price = 1",
            "DELETE FROM price_points",
            "UPDATE provider_calls SET cost = 0",
            "DELETE FROM provider_calls",
        ):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only", msg=statement):
                db.execute(statement)
        db.rollback()
        self.assertEqual(history_rows("price_points")[0]["price"], 200)


if __name__ == "__main__":
    unittest.main()
