import sqlite3
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from flightsleuth import config, render, scan, watch
from flightsleuth.history import History
from flightsleuth.models import Insights, Query
from flightsleuth.providers import Allowance
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
            self.history.rows("watches"),
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
        self.assertIsNone(self.history.rows("watches")[1]["below"])

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
        (row,) = self.history.rows("watch_checks")
        self.assertEqual((number, row["id"], row["watch_id"]), (1, 1, 1))
        self.assertEqual((row["finished"], row["outcome"], row["price"], row["confirmed_price"]), (1, "no return flights", 480, None))
        self.assertEqual((row["median"], row["typical_low"], row["typical_high"]), (640, 500, 800))
        self.assertEqual((row["reasons"], row["no_returns"]), ("below,typical", '[["PH0@2030-03-12T18:30", 480]]'))
        self.assertEqual((row["searches"], row["day"], row["fetched_at"]), (3, "2030-01-10", "2030-01-10T12:00:00+00:00"))

    def test_a_check_that_failed_needs_only_what_went_wrong(self):
        self.history.add_watch_check(1, checked_at=NOW, day=TODAY, finished=False, outcome="failed", detail="network error")
        (row,) = self.history.rows("watch_checks")
        self.assertEqual((row["finished"], row["price"], row["reasons"], row["no_returns"], row["searches"]), (0, None, "", "[]", 0))

    def test_an_alert_and_an_event_are_kept_too(self):
        self.history.add_watch_event(1, "removed", "by the owner", NOW)
        self.history.add_watch_alert(
            1, 7, price=455, title="t", message="m", click="https://example.org", sent=False, error="no network", fetched_at=NOW, at=NOW
        )
        (event,), (alert,) = self.history.rows("watch_events"), self.history.rows("watch_alerts")
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
        self.assertEqual((reopened.rows("watches"), len(reopened.rows("price_points"))), ([], 1))

    def test_what_has_been_spent_is_the_cost_of_the_live_calls(self):
        self.assertEqual(self.history.spent(), 0)
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 12), flex=1)))
        self.assertEqual(self.history.spent(), 3)
        scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 12), flex=1)))  # replayed: free
        self.assertEqual(self.history.spent(), 3)


def a_watch(**changes):
    options = {
        "id": 1,
        "trip": watch.trip("JFK", "LHR", date(2030, 3, 12), date(2030, 3, 19)),
        "below": None,
        "drop_percent": 15.0,
        "typical": True,
        "every_days": 1,
        "created_day": TODAY,
        **changes,
    }
    return watch.Watch(**options)


def lows(*prices, last=TODAY):
    """Google's daily lows, one a day, the last of them on `last`."""
    start = datetime(last.year, last.month, last.day, 4, tzinfo=timezone.utc) - timedelta(days=len(prices) - 1)
    return tuple((start + timedelta(days=n), price) for n, price in enumerate(prices))


def insights(history=(), typical=(500, 800)):
    return Insights(lowest_price=None, level="typical", typical_range=typical, price_history=history)


class TripTest(unittest.TestCase):
    def test_a_round_trip_is_checked_in_googles_own_order_and_a_one_way_cheapest_first(self):
        self.assertFalse(watch.trip("JFK", "LHR", date(2030, 3, 12), date(2030, 3, 19)).by_price)
        self.assertTrue(watch.trip("JFK", "LHR", date(2030, 3, 12)).by_price)

    def test_a_one_way_check_is_an_ordinary_search_and_a_round_trip_check_is_not(self):
        # So a one-way check adds to price_points, as any plain search does, and a round trip's cannot.
        self.assertTrue(watch.trip("JFK", "LHR", date(2030, 3, 12)).plain)
        self.assertFalse(watch.trip("JFK", "LHR", date(2030, 3, 12), date(2030, 3, 19)).plain)


class RulesTest(unittest.TestCase):
    """The three reasons to alert: under a set price, well under the recent median, under Google's typical range."""

    def rules(self, price, found=None, **changes):
        return [fired.rule for fired in watch.fired(a_watch(**changes), price, found or insights(), TODAY)]

    def test_a_price_under_the_one_that_was_set_fires(self):
        self.assertEqual(self.rules(599, below=600, typical=False), ["below"])

    def test_a_price_at_the_one_that_was_set_does_not(self):
        self.assertEqual(self.rules(600, below=600, typical=False), [])

    def test_with_no_price_set_that_rule_is_off(self):
        self.assertEqual(self.rules(1, typical=False), [])

    def test_the_median_is_of_the_last_thirty_daily_lows(self):
        # Sixty-one days, as Google sends: the older half was dear, the last thirty were not.
        history = lows(*[900] * 31, *[600] * 30)
        self.assertEqual(watch.median(insights(history), TODAY), 600)
        self.assertEqual(watch.median(insights(lows(*[900] * 30, *[600] * 31)), TODAY), 600)
        self.assertEqual(watch.median(insights(lows(100, 200, 300, 400, 500, 600, 700, 800, 900, 1000)), TODAY), 550)

    def test_days_after_today_are_left_out_of_it(self):
        history = lows(*[600] * 12, *[100] * 5, last=TODAY + timedelta(days=5))
        self.assertEqual(watch.median(insights(history), TODAY), 600)

    def test_too_few_daily_lows_make_no_median(self):
        self.assertIsNone(watch.median(insights(lows(*[600] * 9)), TODAY))
        self.assertIsNone(watch.median(insights(lows(*[600] * 40, last=TODAY - timedelta(days=25))), TODAY))
        self.assertIsNone(watch.median(None, TODAY))

    def test_a_price_the_set_percentage_under_the_median_fires(self):
        found = insights(lows(*[600] * 30), typical=None)
        self.assertEqual(self.rules(510, found), ["median"])  # exactly 15% under
        self.assertEqual(self.rules(511, found), [])
        self.assertEqual(self.rules(540, found, drop_percent=10), ["median"])

    def test_a_drop_of_nought_turns_that_rule_off(self):
        self.assertEqual(self.rules(1, insights(lows(*[600] * 30), typical=None), drop_percent=0), [])

    def test_a_price_under_googles_typical_range_fires(self):
        self.assertEqual(self.rules(499), ["typical"])
        self.assertEqual(self.rules(500), [])
        self.assertEqual(self.rules(499, typical=False), [])

    def test_a_reply_with_no_rating_can_only_fire_the_set_price(self):
        self.assertEqual([f.rule for f in watch.fired(a_watch(below=600), 1, None, TODAY)], ["below"])
        self.assertEqual([f.rule for f in watch.fired(a_watch(below=600), 1, insights(typical=None), TODAY)], ["below"])

    def test_every_rule_that_fires_is_named_with_the_number_it_fell_under(self):
        found = watch.fired(a_watch(below=600), 450, insights(lows(*[640] * 30)), TODAY)
        self.assertEqual(
            found,
            [watch.Fired("below", 600), watch.Fired("median", 640, days=30), watch.Fired("typical", 500, top=800)],
        )
        self.assertEqual(
            [render.why(fired, 450, "USD") for fired in found],
            [
                "below the $600 you set",
                "30% under the 30-day median of $640",
                "below Google's typical range of $500 to $800",
            ],
        )


class NewsTest(unittest.TestCase):
    """Never the same price twice: a second alert needs a further drop of at least $5 and at least 2%."""

    def test_the_first_price_that_fires_is_news(self):
        self.assertTrue(watch.is_news(455, None))

    def test_the_same_price_again_is_not(self):
        self.assertFalse(watch.is_news(455, 455))

    def test_a_higher_price_is_not_even_after_the_fare_rose_and_fell_back(self):
        self.assertFalse(watch.is_news(470, 455))

    def test_on_a_cheap_fare_the_five_dollars_decides(self):
        # 2% of $100 is $2, so $5 is the bar.
        self.assertFalse(watch.is_news(95.01, 100))
        self.assertTrue(watch.is_news(95, 100))

    def test_on_a_dear_fare_the_two_percent_decides(self):
        # 2% of $1,000 is $20.
        self.assertFalse(watch.is_news(981, 1000))
        self.assertTrue(watch.is_news(980, 1000))
        self.assertEqual(watch.news_below(1000), 980)

    def test_at_250_the_two_bars_are_the_same(self):
        self.assertEqual(watch.news_below(250), 245)


class StandingTest(unittest.TestCase):
    """Where a watch stands is read from the tables each time. Nothing else remembers it."""

    def setUp(self):
        install(self)
        self.history = history(self)
        self.id = self.history.add_watch(
            ROUND_TRIP, below=450, drop_percent=15, typical=True, every_days=4, created_at=NOW, created_day=TODAY
        )

    def check(self, days=0, **found):
        options = {"finished": True, "outcome": "quiet", "detail": "", **found}
        when = NOW + timedelta(days=days)
        return self.history.add_watch_check(self.id, checked_at=when, day=when.date(), **options)

    def alert(self, check_id, price, sent=True, days=0):
        self.history.add_watch_alert(
            self.id, check_id, price=price, title="t", message="m", click="", sent=sent, error="" if sent else "no network",
            fetched_at=NOW, at=NOW + timedelta(days=days),
        )  # fmt: skip

    def standing(self):
        (found,) = watch.standings(self.history)
        return found

    def test_a_watch_is_read_back_as_it_was_made(self):
        found = self.standing().watch
        self.assertEqual((found.id, found.trip, found.below, found.drop_percent), (1, ROUND_TRIP, 450, 15))
        self.assertEqual((found.typical, found.every_days, found.created_day, found.rules), (True, 4, TODAY, 3))

    def test_a_new_watch_is_due_the_day_it_was_made(self):
        found = self.standing()
        self.assertEqual((found.due, found.last, found.done, found.searches, found.ended), (TODAY, None, None, 0, ""))

    def test_after_a_check_it_is_due_its_interval_later(self):
        self.check(searches=1, price=520)
        found = self.standing()
        self.assertEqual((found.due, found.searches), (TODAY + timedelta(days=4), 1))

    def test_a_check_that_failed_leaves_it_due_and_still_counts_what_it_spent(self):
        self.check(searches=1, price=520)
        self.check(days=4, finished=False, outcome="failed", detail="network error", searches=1)
        found = self.standing()
        self.assertEqual(found.due, TODAY + timedelta(days=4))
        self.assertEqual((found.last["outcome"], found.done["outcome"], found.searches), ("failed", "quiet", 2))

    def test_the_last_price_is_from_the_last_check_that_found_one(self):
        self.check(price=520)
        self.check(days=4, finished=False, outcome="failed")
        self.assertEqual(self.standing().priced["price"], 520)

    def test_the_lowest_price_alerted_counts_only_alerts_that_were_sent(self):
        self.alert(self.check(price=440), 440)
        self.alert(self.check(days=4, price=430), 430, sent=False)
        self.assertEqual(self.standing().lowest_alerted, 440)

    def test_an_alert_that_never_went_is_still_waiting_until_a_try_gets_through(self):
        number = self.check(price=440)
        self.alert(number, 440, sent=False)
        self.alert(number, 440, sent=False, days=1)
        found = self.standing()
        self.assertEqual((found.unsent["price"], found.unsent_since, found.lowest_alerted), (440, NOW, None))
        self.alert(number, 440, days=2)
        self.assertEqual((self.standing().unsent, self.standing().lowest_alerted), (None, 440))

    def test_an_older_alert_that_never_went_is_let_go_once_there_is_a_newer_one(self):
        self.alert(self.check(price=440), 440, sent=False)
        self.alert(self.check(days=4, price=430), 430)
        self.assertIsNone(self.standing().unsent)

    def test_fares_with_no_return_flights_are_remembered_from_every_check(self):
        self.check(no_returns=(("PH0@2030-03-12T18:30", 480),))
        self.check(days=4, no_returns=(("PH0@2030-03-12T18:30", 480), ("PH1@2030-03-12T18:30", 481)))
        self.assertEqual(self.standing().empties, (("PH0@2030-03-12T18:30", 480), ("PH1@2030-03-12T18:30", 481)))

    def test_removing_or_retiring_ends_it(self):
        self.history.add_watch_event(self.id, "outage", "", NOW)
        self.assertEqual(self.standing().ended, "")
        self.history.add_watch_event(self.id, "retired", "", NOW)
        self.assertEqual(self.standing().ended, "retired")

    def test_it_counts_as_having_missed_two_intervals_eight_days_after_its_last_finished_check(self):
        self.assertEqual(self.standing().overdue_since, TODAY + timedelta(days=8))
        self.check(days=1)
        self.assertEqual(self.standing().overdue_since, TODAY + timedelta(days=9))

    def test_being_told_about_an_outage_lasts_until_the_next_check_that_finishes(self):
        self.check()
        self.history.add_watch_event(self.id, "outage", "", NOW + timedelta(days=8))
        self.assertTrue(self.standing().outage_told)
        self.check(days=9)
        self.assertFalse(self.standing().outage_told)


OCT_10 = date(2026, 10, 10)
FREE_PLAN = Allowance(left=26, per_month=250, resets_on=date(2026, 11, 8))
LONDON = watch.trip("JFK", "LHR", date(2026, 12, 8), date(2026, 12, 15))
WARSAW = watch.trip("BOS", "WAW", date(2027, 1, 24), date(2027, 2, 2))


def two(every):
    return [(a_watch(id=n, trip=trip, every_days=every, created_day=OCT_10), OCT_10) for n, trip in enumerate((LONDON, WARSAW), 1)]


def sums(watches, allowance=FREE_PLAN, *, today=OCT_10, ceiling=150, used=0, per_check=1):
    return watch.budget(watches, allowance, today=today, ceiling=ceiling, used=used, per_check=per_check)


class BudgetTest(unittest.TestCase):
    """A watch the searches cannot cover is refused, and the sums are shown either way."""

    def test_the_owners_two_watches_every_three_days_use_all_26_and_leave_nothing_spare(self):
        found = sums(two(3))
        self.assertEqual([count for _, count in found.now.checks], [10, 10])
        self.assertEqual((found.now.needed, found.now.available, found.now.spare), (26, 26, 0))
        self.assertTrue(found.fits)

    def test_every_four_days_they_leave_four_spare(self):
        found = sums(two(4))
        self.assertEqual((found.now.count, found.now.held, found.now.needed, found.now.spare), (16, 6, 22, 4))
        self.assertEqual((found.now.start, found.now.end), (OCT_10, date(2026, 11, 8)))

    def test_every_two_days_they_do_not_fit(self):
        found = sums(two(2))
        self.assertEqual((found.now.needed, found.now.spare, found.fits), (36, -10, False))

    def test_the_sums_are_written_out(self):
        self.assertEqual(
            render.budget_lines(sums(two(4))),
            [
                "Until the searches reset on Sun 08 Nov:",
                "  JFK -> LHR, Tue 08 Dec - Tue 15 Dec: 8 checks, one every 4 days",
                "  BOS -> WAW, Sun 24 Jan - Tue 02 Feb: 8 checks, one every 4 days",
                "  16 checks at 1 search each, and 3 held back for each of 2 watches to confirm an alert: 16 + 6 = 22",
                "  26 searches are left, so 4 would be spare.",
                "In the month after that, to Tue 08 Dec:",
                "  JFK -> LHR, Tue 08 Dec - Tue 15 Dec: 7 checks, one every 4 days",
                "  BOS -> WAW, Sun 24 Jan - Tue 02 Feb: 7 checks, one every 4 days",
                "  14 checks at 1 search each, and 3 held back for each of 2 watches to confirm an alert: 14 + 6 = 20",
                "  Watches may use 150 of the plan's 250 searches a month, so 130 would be spare.",
            ],
        )

    def test_a_refusal_says_how_many_too_many(self):
        self.assertEqual(render.budget_lines(sums(two(2)))[4], "  26 searches are left, so that is 10 too many.")

    def test_the_shortest_interval_that_fits_can_be_asked_for(self):
        (first, _), (second, _) = two(1)
        fit = watch.shortest_fit(second, [(replace(first, every_days=3), OCT_10)], FREE_PLAN, today=OCT_10, ceiling=150, used=0)
        self.assertEqual(fit, 3)
        fit = watch.shortest_fit(second, [(replace(first, every_days=4), OCT_10)], FREE_PLAN, today=OCT_10, ceiling=150, used=0)
        self.assertEqual(fit, 3)  # 8 checks and 3 held beside it leave 15: ten checks and 3 held is 13

    def test_beside_a_watch_that_already_takes_everything_no_interval_fits(self):
        (first, _), (second, _) = two(1)  # checked daily, the first needs 29 of the 26 by itself
        self.assertIsNone(watch.shortest_fit(second, [(first, OCT_10)], FREE_PLAN, today=OCT_10, ceiling=150, used=0))

    def test_a_daily_watch_after_the_reset_is_held_to_the_owners_ceiling(self):
        three = [(a_watch(id=n, every_days=1, created_day=OCT_10, trip=WARSAW), OCT_10) for n in (1, 2, 3)]
        found = sums(three, Allowance(left=250, per_month=250, resets_on=date(2026, 11, 8)))
        # Thirty days of November's month, three watches: 90 checks and 9 held is 99 of the 150.
        self.assertEqual((found.after.count, found.after.needed, found.after.available, found.after.spare), (90, 99, 150, 51))
        self.assertTrue(found.fits)
        self.assertFalse(sums(three, Allowance(250, 250, date(2026, 11, 8)), ceiling=90).fits)

    def test_before_the_reset_the_ceiling_counts_what_watches_have_already_used(self):
        found = sums(two(4), Allowance(left=200, per_month=250, resets_on=date(2026, 11, 8)), used=140)
        self.assertEqual((found.now.available, found.now.spare, found.fits), (10, -12, False))
        self.assertEqual(
            render.budget_lines(found)[4],
            "  Watches have used 140 of the 150 searches a month they may, which leaves 10, so that is 12 too many.",
        )

    def test_a_bigger_plan_changes_the_sums_by_itself(self):
        paid = Allowance(left=4000, per_month=5000, resets_on=date(2026, 11, 8))
        self.assertFalse(sums(two(1), FREE_PLAN).fits)
        found = sums(two(1), paid, ceiling=400)
        self.assertTrue(found.fits)
        # Two daily watches: 58 checks and 6 held until the reset, 60 and 6 in the month after.
        self.assertEqual(
            render.budget_lines(found)[4],
            "  Watches have used 0 of the 400 searches a month they may, which leaves 400, so 336 would be spare.",
        )
        self.assertEqual(render.budget_lines(found)[-1], "  Watches may use 400 of the plan's 5000 searches a month, so 334 would be spare.")
        self.assertEqual(render.budget_lines(sums(two(1), paid, ceiling=9000))[4], "  4000 searches are left, so 3936 would be spare.")

    def test_a_plan_smaller_than_the_ceiling_is_the_limit_after_the_reset(self):
        found = sums(two(4), Allowance(left=26, per_month=100, resets_on=date(2026, 11, 8)))
        self.assertEqual(found.after.available, 100)
        self.assertEqual(render.budget_lines(found)[-1], "  The plan gives 100 searches a month, so 80 would be spare.")

    def test_an_account_that_names_no_reset_is_budgeted_thirty_days_ahead_and_says_so(self):
        found = sums(two(4), Allowance(left=26, per_month=None, resets_on=None))
        self.assertEqual((found.reset_known, found.now.end, found.after.end), (False, date(2026, 11, 9), date(2026, 12, 9)))
        lines = render.budget_lines(found)
        self.assertEqual(lines[0], "In the next 30 days (the account did not say when its searches reset):")
        self.assertEqual(lines[-1], "  Watches may use 150 searches a month, so 130 would be spare.")

    def test_a_reset_day_already_past_is_not_believed(self):
        self.assertFalse(sums(two(4), Allowance(26, 250, date(2026, 10, 10))).reset_known)

    def test_a_watch_is_not_checked_after_it_departs_and_holds_nothing_once_it_is_gone(self):
        soon = a_watch(trip=watch.trip("JFK", "LHR", date(2026, 10, 20), date(2026, 10, 27)), every_days=4, created_day=OCT_10)
        found = sums([(soon, OCT_10)])
        # 10, 14 and 18 October; the 22nd is after it leaves.
        self.assertEqual((found.now.count, found.now.held, found.after.count, found.after.held), (3, 3, 0, 0))

    def test_a_watch_checked_on_the_day_it_departs_is_counted(self):
        leaves = a_watch(trip=watch.trip("JFK", "LHR", date(2026, 10, 18)), every_days=4, created_day=OCT_10)
        self.assertEqual(sums([(leaves, OCT_10)]).now.count, 3)

    def test_an_overdue_watch_is_counted_from_today(self):
        late = a_watch(trip=LONDON, every_days=4, created_day=date(2026, 10, 1))
        self.assertEqual(sums([(late, date(2026, 10, 7))]).now.count, 8)

    def test_a_watch_not_due_yet_is_counted_from_the_day_it_is(self):
        later = a_watch(trip=LONDON, every_days=4, created_day=OCT_10)
        found = sums([(later, date(2026, 10, 13))])
        self.assertEqual((found.now.count, found.after.count), (7, 7))  # 13 Oct to 6 Nov, then 10 Nov to 4 Dec

    def test_a_check_that_costs_more_than_one_search_is_counted_so(self):
        found = sums(two(4), per_check=2)
        self.assertEqual(found.now.needed, 38)
        self.assertIn("16 checks at 2 searches each", render.budget_lines(found)[3])

    def test_one_of_each_is_worded_in_the_singular(self):
        once = a_watch(trip=watch.trip("JFK", "LHR", OCT_10), every_days=1, created_day=OCT_10)
        lines = render.budget_lines(sums([(once, OCT_10)], Allowance(1, 250, date(2026, 11, 8))))
        self.assertEqual(
            lines[1:4],
            [
                "  JFK -> LHR, Sat 10 Oct: 1 check, one every 1 day",
                "  1 check at 1 search each, and 3 held back for each of 1 watch to confirm an alert: 1 + 3 = 4",
                "  1 search is left, so that is 3 too many.",
            ],
        )


class MonthTest(unittest.TestCase):
    def test_the_plans_month_starts_a_month_before_the_reset(self):
        self.assertEqual(watch.month_start(FREE_PLAN, OCT_10), date(2026, 10, 8))
        self.assertEqual(watch.month_start(Allowance(1, 1, date(2027, 1, 31)), date(2027, 1, 5)), date(2026, 12, 31))
        self.assertEqual(watch.month_start(Allowance(1, 1, date(2027, 3, 31)), date(2027, 3, 5)), date(2027, 2, 28))

    def test_with_no_reset_day_it_is_thirty_days_back(self):
        self.assertEqual(watch.month_start(Allowance(1, None, None), OCT_10), date(2026, 9, 10))

    def test_what_watches_have_used_this_month_counts_ended_watches_too(self):
        install(self)
        opened = history(self)
        for day, searches in ((date(2026, 10, 7), 4), (date(2026, 10, 8), 1), (date(2026, 10, 9), 3)):
            opened.add_watch_check(9, checked_at=NOW, day=day, finished=True, outcome="quiet", detail="", searches=searches)
        self.assertEqual(watch.used_since(opened, date(2026, 10, 8)), 4)


if __name__ == "__main__":
    unittest.main()
