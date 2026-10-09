import sqlite3
import unittest
from datetime import date, datetime, timezone

from flightsleuth import config, scan
from flightsleuth.history import History
from flightsleuth.models import Query
from tests.fakes import install

NOW = datetime(2030, 1, 10, 12, 0, tzinfo=timezone.utc)  # the fake clock: a Thursday, at midday
TODAY = NOW.date()
ROUND_TRIP = Query("JFK", "LHR", date(2030, 3, 12), date(2030, 3, 19), by_price=False)


def history(test):
    """The test's own history file, closed when the test ends."""
    opened = History(config.history_path())
    test.addCleanup(opened.close)
    return opened


class WatchTableTest(unittest.TestCase):
    """history.db keeps each watch, and everything that happened to it, in tables nothing can rewrite."""

    def setUp(self):
        install(self)
        self.history = history(self)

    def keep(self, **changes):
        options = {"below": 450, "drop_percent": 15, "typical": True, "every_days": 4, **changes}
        return self.history.add_watch(ROUND_TRIP, created_at=NOW, created_day=TODAY, **options)

    def test_a_watch_is_kept_with_its_search_its_rules_and_its_interval(self):
        self.assertEqual(self.keep(), 1)
        self.assertEqual(
            self.history.watches(),
            [
                {
                    "id": 1,
                    "origin": "JFK",
                    "destination": "LHR",
                    "depart_date": "2030-03-12",
                    "return_date": "2030-03-19",
                    "adults": 1,
                    "cabin": "economy",
                    "stops": "any",
                    "currency": "USD",
                    "below": 450,
                    "drop_percent": 15,
                    "typical": 1,
                    "every_days": 4,
                    "created_at": "2030-01-10T12:00:00+00:00",
                    "created_day": "2030-01-10",
                }
            ],
        )

    def test_each_watch_gets_the_next_number(self):
        self.assertEqual([self.keep(), self.keep(below=None)], [1, 2])
        self.assertIsNone(self.history.watches()[1]["below"])

    def test_a_check_is_kept_with_what_it_found_and_what_it_cost(self):
        number = self.history.add_watch_check(
            self.keep(),
            checked_at=NOW,
            day=TODAY,
            finished=True,
            outcome="no return flights",
            detail="$480 listed; its return flights could not be found",
            price=480,
            itinerary_key="PH0@2030-03-12T18:30",
            median=640,
            typical_range=(500, 800),
            reasons=("below", "typical"),
            no_returns=(("PH0@2030-03-12T18:30", 480),),
            searches=3,
            fetched_at=NOW,
        )
        (row,) = self.history.watch_checks()
        self.assertEqual((number, row["id"], row["watch_id"]), (1, 1, 1))
        self.assertEqual((row["finished"], row["outcome"], row["price"], row["confirmed_price"]), (1, "no return flights", 480, None))
        self.assertEqual((row["median"], row["typical_low"], row["typical_high"]), (640, 500, 800))
        self.assertEqual((row["reasons"], row["no_returns"]), ("below,typical", '[["PH0@2030-03-12T18:30", 480]]'))
        self.assertEqual((row["searches"], row["day"], row["fetched_at"]), (3, "2030-01-10", "2030-01-10T12:00:00+00:00"))

    def test_a_check_that_failed_needs_only_what_went_wrong(self):
        self.history.add_watch_check(1, checked_at=NOW, day=TODAY, finished=False, outcome="failed", detail="network error")
        (row,) = self.history.watch_checks()
        self.assertEqual((row["finished"], row["price"], row["reasons"], row["no_returns"], row["searches"]), (0, None, "", "[]", 0))

    def test_an_alert_and_an_event_are_kept_too(self):
        self.history.add_watch_event(1, "removed", "by the owner", NOW)
        self.history.add_watch_alert(
            1, 7, price=455, title="t", message="m", click="https://example.org", sent=False, error="no network", fetched_at=NOW, at=NOW
        )
        (event,), (alert,) = self.history.watch_events(), self.history.watch_alerts()
        self.assertEqual((event["watch_id"], event["event"], event["detail"]), (1, "removed", "by the owner"))
        self.assertEqual((alert["check_id"], alert["price"], alert["sent"], alert["error"]), (7, 455, 0, "no network"))

    def test_none_of_it_can_be_changed_or_deleted(self):
        self.history.add_watch_event(self.keep(), "removed", "", NOW)
        self.history.add_watch_check(1, checked_at=NOW, day=TODAY, finished=True, outcome="quiet", detail="")
        self.history.add_watch_alert(1, 1, price=1, title="", message="", click="", sent=True, error="", fetched_at=NOW, at=NOW)
        db = sqlite3.connect(config.history_path())
        self.addCleanup(db.close)
        for table in ("watches", "watch_events", "watch_checks", "watch_alerts"):
            for statement in (f"UPDATE {table} SET id = 9", f"DELETE FROM {table}"):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only", msg=statement):
                    db.execute(statement)
        db.rollback()

    def test_a_history_file_from_before_these_tables_gains_them_and_keeps_its_rows(self):
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 12))))
        self.history.close()
        db = sqlite3.connect(config.history_path())
        for table in ("watches", "watch_events", "watch_checks", "watch_alerts"):
            db.executescript(f"DROP TRIGGER {table}_no_update; DROP TRIGGER {table}_no_delete; DROP TABLE {table};")
        db.close()
        reopened = history(self)
        self.assertEqual((reopened.watches(), len(reopened.price_points())), ([], 1))

    def test_what_has_been_spent_is_the_cost_of_the_live_calls(self):
        self.assertEqual(self.history.spent(), 0)
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 12), flex=1)))
        self.assertEqual(self.history.spent(), 3)
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 12), flex=1)))  # replayed: free
        self.assertEqual(self.history.spent(), 3)


if __name__ == "__main__":
    unittest.main()
