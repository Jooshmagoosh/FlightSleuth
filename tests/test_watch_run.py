"""The run that checks the watches, on a fake clock and the fake price source. Nothing here reaches a network."""
import json
import os
import statistics
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from flightsleuth import config, notify, render, scan, watch
from flightsleuth.errors import UserError
from flightsleuth.history import History
from flightsleuth.serpapi import SerpApiError
from tests.fakes import NO_FLIGHTS, STAMP, FakeNtfy, FakeSerpApi, age_the_cache, endpoints, install, rich_reply

NOW = datetime(2030, 1, 10, 12, 0, tzinfo=timezone.utc)  # the fake clock starts on a Thursday, at midday
TODAY = NOW.date()
DEPART, BACK = date(2030, 3, 12), date(2030, 3, 19)  # the fake's round-trip fare for the 12th is 520
LONDON = watch.trip("JFK", "LHR", DEPART, BACK)
TITLE = "JFK -> LHR, Tue 12 Mar - Tue 19 Mar"
MOMONDO = "https://www.momondo.com/flight-search/JFK-LHR/2030-03-12/2030-03-19?sort=price_a"
TOPIC = "flightsleuth-test-topic"
NO_NETWORK = urllib.error.URLError("no route to host")


def daily_lows(*prices, last=TODAY):
    """Google's price history as SerpApi sends it: [seconds, lowest price], one a day, the last of them on `last`."""
    start = datetime(last.year, last.month, last.day, 4, tzinfo=timezone.utc) - timedelta(days=len(prices) - 1)
    return [[int((start + timedelta(days=n)).timestamp()), price] for n, price in enumerate(prices)]




class RunCase(unittest.TestCase):
    """A fake clock, the fake source replying as real replies do, a topic, and a plan with plenty left."""

    def setUp(self):
        install(self)
        FakeSerpApi.rich = True
        FakeSerpApi.account_reply.update(total_searches_left=250, searches_per_month=250, plan_renewal_date="2030-02-01")
        self.enterContext(mock.patch.dict(os.environ, {config.TOPIC_VAR: TOPIC}))
        self.history = History(config.history_path())
        self.addCleanup(self.history.close)

    def add(self, wanted=LONDON, days=0, **rules):
        return watch.add(wanted, now=NOW + timedelta(days=days), **rules).watch

    def run_at(self, days=0, **options):
        return watch.run_due(now=NOW + timedelta(days=days), **options)

    def checks(self):
        return self.history.rows("watch_checks")

    def last(self, *columns):
        row = self.checks()[-1]
        return tuple(row[column] for column in columns)

    def price(self, amount):
        FakeSerpApi.fare = lambda query: amount


class CheckTest(RunCase):
    """A check is one search. It is only followed up when the price is worth an alert."""

    def test_a_new_watch_is_checked_at_the_first_run_with_one_search_in_googles_own_order(self):
        self.add()
        report = self.run_at()
        self.assertEqual(report.lines, [f"{TITLE}: $520 listed. No rule fired. 1 search."])
        self.assertEqual((report.spent, report.failed), (1, False))
        (query,) = FakeSerpApi.searches
        self.assertEqual((query.endpoint, query.by_price, query.return_date), ("search", False, BACK))
        self.assertEqual(FakeNtfy.sent, [])

    def test_the_check_is_kept_with_its_price_and_what_google_said_about_it(self):
        FakeSerpApi.insights = {"price_history": daily_lows(*[560] * 30)}
        self.add()
        self.run_at()
        (row,) = self.checks()
        fetched = datetime.fromisoformat(row.pop("fetched_at"))
        self.assertLess(abs((datetime.now(timezone.utc) - fetched).total_seconds()), 60)
        self.assertEqual(
            row,
            {
                "id": 1,
                "watch_id": 1,
                "checked_at": "2030-01-10T12:00:00+00:00",
                "day": "2030-01-10",
                "finished": 1,
                "outcome": "quiet",
                "detail": "$520 listed. No rule fired.",
                "price": 520,
                "confirmed_price": None,
                "itinerary_key": "TA1@2030-03-12T18:30",
                "median": 560,
                "typical_low": 500,
                "typical_high": 800,
                "reasons": "",
                "no_returns": "[]",
                "searches": 1,
            },
        )

    def test_a_round_trip_check_adds_no_price_point_because_it_is_not_sorted_by_price(self):
        self.add()
        self.run_at()
        self.assertEqual(self.history.rows("price_points"), [])
        self.assertEqual([(call["endpoint"], call["live"], call["cost"]) for call in self.history.rows("provider_calls")], [("search", 1, 1)])

    def test_a_one_way_check_is_the_ordinary_sorted_search_and_adds_one(self):
        self.add(watch.trip("JFK", "LHR", DEPART))
        report = self.run_at()
        self.assertEqual(report.lines, ["JFK -> LHR, Tue 12 Mar: $200 listed. No rule fired. 1 search."])
        self.assertTrue(FakeSerpApi.searches[0].by_price)
        self.assertEqual([point["price"] for point in self.history.rows("price_points")], [200])

    def test_it_is_not_checked_again_until_its_interval_has_passed(self):
        self.add(every_days=4)
        self.run_at()
        for days in (1, 2, 3):
            self.assertEqual(self.run_at(days).lines, [f"{TITLE}: not due until Mon 14 Jan."])
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertEqual(self.run_at(4).spent, 1)
        self.assertEqual(len(self.checks()), 2)

    def test_a_run_missed_while_the_machine_was_off_is_made_at_the_next_one(self):
        self.add(every_days=4)
        self.run_at()
        # Nothing ran on the 14th, 15th or 16th. The 17th checks it, and the interval counts from there.
        self.assertEqual(self.run_at(7).spent, 1)
        self.assertEqual(self.run_at(10).lines, [f"{TITLE}: not due until Mon 21 Jan."])
        self.assertEqual(self.run_at(11).spent, 1)

    def test_a_second_run_on_the_same_day_checks_nothing_twice(self):
        self.add()
        self.run_at()
        self.assertEqual(self.run_at().lines, [f"{TITLE}: not due until Fri 11 Jan."])
        self.assertEqual(len(FakeSerpApi.searches), 1)

    def test_each_check_asks_afresh_even_when_a_reply_is_saved(self):
        scan.run(scan.plan(watch.request(self.add()), today=TODAY))
        self.assertEqual(self.run_at().spent, 1)
        self.assertEqual(len(FakeSerpApi.searches), 2)

    def test_a_reply_with_no_fares_is_a_finished_check(self):
        self.add()
        with mock.patch.object(FakeSerpApi, "search", lambda self, query: NO_FLIGHTS):
            report = self.run_at()
        self.assertEqual(report.lines, [f"{TITLE}: No fares were listed. 1 search."])
        self.assertEqual(self.last("finished", "outcome", "price"), (1, "no fares", None))

    def test_two_watches_on_the_same_dates_share_one_search(self):
        self.add(below=400)
        self.add(below=450, every_days=2)
        report = self.run_at()
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertEqual([row["searches"] for row in self.checks()], [1, 0])
        self.assertEqual(report.lines, [f"{TITLE}: $520 listed. No rule fired. 1 search.", f"{TITLE}: $520 listed. No rule fired."])

    def test_with_no_watches_it_says_so_and_asks_nothing(self):
        self.assertEqual(self.run_at().lines, ["There are no watches to check."])
        self.assertEqual((FakeSerpApi.searches, FakeNtfy.posts), ([], []))


class AlertTest(RunCase):
    """A price that fires a rule has its return flights and its sellers fetched, and then the phone is told."""

    def test_a_fare_under_the_price_that_was_set_is_confirmed_and_then_alerted(self):
        self.add(below=600)
        report = self.run_at()
        self.assertEqual(endpoints(), ["search", "return_flights", "booking_options"])
        self.assertEqual(report.lines, [f"{TITLE}: $520 confirmed. Alert sent: below the $600 you set. 3 searches."])
        self.assertEqual(report.spent, 3)
        self.assertEqual(self.last("outcome", "price", "confirmed_price", "reasons", "searches"), ("alerted", 520, 520, "below", 3))
        self.assertEqual(self.last("itinerary_key"), ("TA1@2030-03-12T18:30|TA2@2030-03-19T09:00",))

    def test_the_alert_names_the_route_the_dates_the_price_why_and_the_flights_and_opens_momondo(self):
        self.add(below=600)
        self.run_at()
        (sent,) = FakeNtfy.sent
        self.assertEqual((sent["topic"], sent["title"], sent["click"]), (TOPIC, "JFK to LHR: $520 round trip", MOMONDO))
        self.assertRegex(
            sent["message"],
            r"^Tue 12 Mar - Tue 19 Mar\. Why: below the \$600 you set\.\n"
            r"Out: JFK->LHR Tue 12 Mar 18:30 -> 23:10, nonstop, 7h 10m, Testair \(TA 1\)\.\n"
            r"Back: LHR->JFK Tue 19 Mar 09:00 -> 12:00, nonstop, 8h 00m, Testair \(TA 2\)\.\n"
            rf"Return flights fetched {STAMP}\.\n"
            r"Sold on one ticket by Testair at \$520\.\n"
            r"Tap to check it on Momondo\.$",
        )

    def test_the_alert_is_kept_with_the_check_that_found_the_price(self):
        self.add(below=600)
        self.run_at()
        (alert,) = self.history.rows("watch_alerts")
        self.assertEqual((alert["watch_id"], alert["check_id"], alert["price"], alert["sent"], alert["error"]), (1, 1, 520, 1, ""))
        self.assertEqual((alert["title"], alert["click"], alert["at"]), ("JFK to LHR: $520 round trip", MOMONDO, "2030-01-10T12:00:00+00:00"))

    def test_a_fare_well_under_its_recent_median_fires(self):
        FakeSerpApi.insights = {"price_history": daily_lows(*[900] * 31, *[640] * 30)}
        self.add()
        self.run_at()
        self.assertEqual(self.last("outcome", "median", "reasons"), ("alerted", 640, "median"))
        self.assertIn("Why: 19% under the 30-day median of $640.", FakeNtfy.sent[0]["message"])

    def test_a_fare_not_far_enough_under_it_does_not(self):
        FakeSerpApi.insights = {"price_history": daily_lows(*[600] * 30)}  # 15% under 600 is 510
        self.add()
        self.run_at()
        self.assertEqual((self.last("outcome"), endpoints()), (("quiet",), ["search"]))

    def test_a_fare_under_googles_typical_range_fires(self):
        self.price(480)
        self.add()
        self.run_at()
        self.assertEqual(self.last("outcome", "reasons"), ("alerted", "typical"))
        self.assertIn("Why: below Google's typical range of $500 to $800.", FakeNtfy.sent[0]["message"])

    def test_every_rule_that_fired_is_given_as_a_reason(self):
        self.price(480)
        FakeSerpApi.insights = {"price_history": daily_lows(*[640] * 30)}
        self.add(below=500)
        self.run_at()
        self.assertEqual(self.last("reasons"), ("below,median,typical",))
        self.assertIn(
            "Why: below the $500 you set; 25% under the 30-day median of $640; below Google's typical range of $500 to $800.",
            FakeNtfy.sent[0]["message"],
        )

    def test_a_rule_that_was_turned_off_does_not_fire(self):
        self.price(480)
        self.add(below=700, typical=False, drop_percent=0)
        self.run_at()
        self.assertEqual(self.last("reasons"), ("below",))

    def test_what_the_link_cannot_carry_is_named_in_the_alert(self):
        self.add(watch.trip("JFK", "LHR", DEPART, BACK, adults=2, stops="nonstop"), below=2000)
        self.run_at()
        self.assertTrue(
            FakeNtfy.sent[0]["message"].endswith(
                "Tap to check it on Momondo.\nThe link carries only the airports and dates. "
                "Set the page to 2 adults, nonstop only before comparing prices."
            )
        )

    def test_a_listing_with_no_rating_does_not_borrow_the_typical_range_of_the_sellers_reply(self):
        # The fake's one-way listing says nothing about its price. Its sellers' reply has a range that
        # $200 is well under, but in the real replies that range was not the listing's.
        self.add(watch.trip("JFK", "LHR", DEPART), below=250)
        self.run_at()
        self.assertEqual(self.last("outcome", "reasons", "typical_low", "typical_high"), ("alerted", "below", None, None))
        self.assertNotIn("typical range", FakeNtfy.sent[0]["message"])

    def test_a_one_way_alert_needs_no_return_lookup_and_opens_the_one_way_page(self):
        self.add(watch.trip("JFK", "LHR", DEPART), below=250)
        report = self.run_at()
        self.assertEqual(endpoints(), ["search", "booking_options"])
        self.assertEqual(report.lines, ["JFK -> LHR, Tue 12 Mar: $200 confirmed. Alert sent: below the $250 you set. 2 searches."])
        (sent,) = FakeNtfy.sent
        self.assertEqual(sent["title"], "JFK to LHR: $200 one way")
        self.assertEqual(sent["click"], "https://www.momondo.com/flight-search/JFK-LHR/2030-03-12?sort=price_a")
        self.assertRegex(
            sent["message"],
            r"^Tue 12 Mar\. Why: below the \$250 you set\.\n"
            r"Out: JFK->LHR Tue 12 Mar 18:30 -> 23:10, nonstop, 7h 10m, Testair \(TA 1\)\.\n"
            rf"Fare fetched {STAMP}\.\n"
            r"Sold on one ticket by Testair at \$200\.\n"
            r"Tap to check it on Momondo\.$",
        )

    def test_two_separate_tickets_are_said_to_be_that_with_the_one_ticket_price_beside_them(self):
        FakeSerpApi.separate = True
        self.add(below=600)
        self.run_at()
        self.assertIn(
            "\n$520 is two separate tickets (Testair $320 + Otherair $200). "
            "On one ticket it is $545 from Cheapo Travel, an agency, not the airline.\n",
            FakeNtfy.sent[0]["message"],
        )

    def test_separate_tickets_nobody_sells_whole_say_so(self):
        def only_separate(self, query):
            body = rich_reply(query)
            if query.booking_token:
                body["booking_options"] = body["booking_options"][:1]
            FakeSerpApi.searches.append(query)
            return body

        FakeSerpApi.separate = True
        self.add(below=600)
        with mock.patch.object(FakeSerpApi, "search", only_separate):
            self.run_at()
        self.assertIn("(Testair $320 + Otherair $200). No seller listed it on one ticket.\n", FakeNtfy.sent[0]["message"])

    def test_an_agency_selling_it_cheapest_is_called_an_agency(self):
        def agency_first(self, query):
            body = rich_reply(query)
            if query.booking_token:
                body["booking_options"][1]["together"]["price"] = 510
            FakeSerpApi.searches.append(query)
            return body

        self.add(below=600)
        with mock.patch.object(FakeSerpApi, "search", agency_first):
            self.run_at()
        self.assertIn("\nSold on one ticket by Cheapo Travel, an agency, not the airline at $510.\n", FakeNtfy.sent[0]["message"])


class NoRepeatTest(RunCase):
    """Never twice for the same price. Again only on a further drop: at least $5 and at least 2%."""

    def setUp(self):
        super().setUp()
        self.add(below=600)
        self.run_at()  # alerted at $520
        FakeSerpApi.searches.clear()

    def test_the_same_price_at_the_next_check_costs_one_search_and_sends_nothing(self):
        report = self.run_at(1)
        self.assertEqual(report.lines, [f"{TITLE}: $520 listed. Already alerted at $520; a new alert needs $509.60 or less. 1 search."])
        self.assertEqual((endpoints(), len(FakeNtfy.sent)), (["search"], 1))
        self.assertEqual(self.last("outcome", "reasons", "confirmed_price"), ("repeat", "below", None))

    def test_a_small_drop_is_not_news(self):
        self.price(510)
        self.run_at(1)
        self.assertEqual((self.last("outcome"), len(FakeNtfy.sent)), (("repeat",), 1))

    def test_a_further_drop_is_confirmed_and_alerted(self):
        self.price(505)
        report = self.run_at(1)
        self.assertEqual(report.lines, [f"{TITLE}: $505 confirmed. Alert sent: below the $600 you set. 3 searches."])
        self.assertEqual([sent["title"] for sent in FakeNtfy.sent], ["JFK to LHR: $520 round trip", "JFK to LHR: $505 round trip"])

    def test_after_that_the_bar_is_the_lower_price(self):
        self.price(505)
        self.run_at(1)
        self.price(500)
        report = self.run_at(2)
        self.assertEqual(report.lines, [f"{TITLE}: $500 listed. Already alerted at $505; a new alert needs $494.90 or less. 1 search."])

    def test_a_fare_that_rose_and_came_back_to_the_same_price_is_not_alerted_again(self):
        self.price(650)
        self.assertEqual(self.run_at(1).lines, [f"{TITLE}: $650 listed. No rule fired. 1 search."])
        self.price(520)
        self.run_at(2)
        self.assertEqual((self.last("outcome"), len(FakeNtfy.sent)), (("repeat",), 1))

    def test_a_drop_that_shrinks_once_the_return_flights_are_fetched_is_not_news_either(self):
        self.price(505)
        FakeSerpApi.return_surcharge = 10  # listed at 505, but 515 with its return flights
        report = self.run_at(1)
        self.assertEqual(
            report.lines, [f"{TITLE}: $515 confirmed. Already alerted at $520; a new alert needs $509.60 or less. 3 searches."]
        )
        self.assertEqual(len(FakeNtfy.sent), 1)


class ReturnFlightsTest(RunCase):
    """No round trip is alerted until its return flights have been fetched."""

    def empty_for(self, *airlines):
        def search(fake, query):
            FakeSerpApi.searches.append(query)
            if query.departure_token and query.departure_token.split(":")[2] in airlines:
                return NO_FLIGHTS
            return rich_reply(query)

        return mock.patch.object(FakeSerpApi, "search", search)

    def test_when_the_cheapest_fare_has_none_the_spare_lookup_confirms_another_and_the_alert_says_so(self):
        self.add(below=600)
        with self.empty_for("Testair"):
            report = self.run_at()
        self.assertEqual(endpoints(), ["search", "return_flights", "return_flights", "booking_options"])
        self.assertEqual(report.lines, [f"{TITLE}: $550 confirmed. Alert sent: below the $600 you set. 4 searches."])
        self.assertEqual(self.last("price", "confirmed_price", "no_returns"), (520, 550, '[["TA1@2030-03-12T18:30", 520]]'))
        (sent,) = FakeNtfy.sent
        self.assertEqual(sent["title"], "JFK to LHR: $550 round trip")
        self.assertIn("\nThe $520 fare listed for these dates (Testair (TA 1)) had no return flights, so it was left out.\n", sent["message"])
        self.assertIn("Out: JFK->LHR Tue 12 Mar 18:30 -> 23:10, nonstop, 7h 10m, Otherair (OA 5).", sent["message"])

    def test_when_no_fare_tried_has_any_nothing_is_alerted(self):
        self.add(below=600)
        with self.empty_for("Testair", "Otherair"):
            report = self.run_at()
        self.assertEqual(endpoints(), ["search", "return_flights", "return_flights"])
        self.assertEqual(
            report.lines,
            [
                f"{TITLE}: $520 listed (below the $600 you set), but its return flights could not be found, so no alert. "
                f"It will not be asked about again at that price. 3 searches."
            ],
        )
        self.assertEqual(FakeNtfy.sent, [])
        self.assertEqual(self.last("finished", "outcome", "confirmed_price"), (1, "no return flights", None))

    def test_a_fare_that_came_back_empty_is_not_asked_about_again_at_the_same_price(self):
        self.add(below=600)
        with self.empty_for("Testair", "Otherair"):
            self.run_at()
            FakeSerpApi.searches.clear()
            report = self.run_at(1)
        self.assertEqual(endpoints(), ["search"])
        self.assertEqual(
            report.lines,
            [
                f"{TITLE}: $520 listed, but every fare listed is one whose return flights could not be found before "
                f"at the same price, so none was asked about again. 1 search."
            ],
        )

    def test_the_fare_behind_it_is_still_followed_when_the_empty_one_is_still_listed(self):
        self.add(below=530)
        with self.empty_for("Testair"):
            self.run_at()  # Testair's $520 is empty; Otherair's $550 is real but fires nothing
            self.assertEqual(self.last("outcome"), ("rose",))
            FakeSerpApi.searches.clear()
            # Next time only Otherair has dropped. Testair's $520 is still listed, and is not asked about.
            real = rich_reply

            def cheaper_otherair(fake, query):
                body = real(query)
                if not query.departure_token and not query.booking_token:
                    body["other_flights"][1]["price"] = 525
                    body["other_flights"][1]["departure_token"] = "dep:525:Otherair"
                FakeSerpApi.searches.append(query)
                return body

            with mock.patch.object(FakeSerpApi, "search", cheaper_otherair):
                report = self.run_at(1)
        self.assertEqual([query.departure_token for query in FakeSerpApi.searches[1:2]], ["dep:525:Otherair"])
        self.assertEqual(report.lines, [f"{TITLE}: $525 confirmed. Alert sent: below the $530 you set. 3 searches."])

    def test_it_is_asked_about_again_once_its_price_changes(self):
        self.add(below=600)
        with self.empty_for("Testair", "Otherair"):
            self.run_at()
        FakeSerpApi.searches.clear()
        self.price(515)
        report = self.run_at(1)
        self.assertEqual(endpoints(), ["search", "return_flights", "booking_options"])
        self.assertEqual(report.lines, [f"{TITLE}: $515 confirmed. Alert sent: below the $600 you set. 3 searches."])

    def test_a_fare_dearer_with_its_return_flights_than_any_rule_allows_is_not_alerted(self):
        FakeSerpApi.return_surcharge = 45
        self.add(below=540)
        report = self.run_at()
        self.assertEqual(
            report.lines, [f"{TITLE}: $520 listed, but with its return flights it came to $565, which fires no rule. 3 searches."]
        )
        self.assertEqual((FakeNtfy.sent, self.last("outcome", "confirmed_price", "reasons")), ([], ("rose", 565, "")))

    def test_return_flights_saved_more_than_a_few_minutes_ago_are_fetched_again_before_an_alert(self):
        self.add(below=600)
        with mock.patch.object(FakeNtfy, "fail", NO_NETWORK):
            self.run_at()  # confirmed at $520, but the alert never went
        FakeSerpApi.searches.clear()
        age_the_cache(hours=0.2)
        watch.remove(1, now=NOW)
        self.add(below=600)
        self.run_at()
        # Nothing saved twelve minutes ago is believed: the listing and both follow-ups are asked for again.
        self.assertEqual(endpoints(), ["search", "return_flights", "booking_options"])
        self.assertEqual(self.last("outcome", "searches"), ("alerted", 3))

    def test_the_alert_gives_the_price_with_the_return_flights_not_the_listed_one(self):
        FakeSerpApi.return_surcharge = 45
        self.add(below=600)
        self.run_at()
        (sent,) = FakeNtfy.sent
        self.assertEqual(sent["title"], "JFK to LHR: $565 round trip")
        # Otherair still lists $550, and nobody looked that up. The alert does not hide it.
        self.assertIn("1 cheaper fare is listed for these dates, from $550, but its return flights were not looked up.", sent["message"])


class BudgetRunTest(RunCase):
    """A run never spends what the account, the monthly ceiling or its own limit does not allow."""

    def test_with_no_searches_left_the_watch_is_not_checked_and_stays_due(self):
        self.add()
        FakeSerpApi.account_reply["total_searches_left"] = 0
        report = self.run_at()
        self.assertEqual(report.lines, [f"{TITLE}: Not checked: the account has no searches left this month. It stays due."])
        self.assertEqual((FakeSerpApi.searches, report.spent, report.failed), ([], 0, False))
        self.assertEqual(self.last("finished", "outcome", "searches"), (0, "skipped", 0))
        FakeSerpApi.account_reply["total_searches_left"] = 250
        self.assertEqual(self.run_at(1).spent, 1)

    def test_with_one_left_the_price_is_fetched_but_cannot_be_confirmed_so_nobody_is_told(self):
        self.add(below=600)
        FakeSerpApi.account_reply["total_searches_left"] = 1
        report = self.run_at()
        self.assertEqual(
            report.lines,
            [
                f"{TITLE}: $520 listed (below the $600 you set), but it could not be confirmed, so no alert: "
                f"the account has no searches left this month. 1 search."
            ],
        )
        self.assertEqual((endpoints(), FakeNtfy.sent), (["search"], []))

    def test_with_two_left_the_return_flights_are_fetched_and_the_alert_says_the_sellers_were_not(self):
        self.add(below=600)
        FakeSerpApi.account_reply["total_searches_left"] = 2
        self.run_at()
        self.assertEqual(endpoints(), ["search", "return_flights"])
        self.assertIn("\nWho sells it was not looked up.\n", FakeNtfy.sent[0]["message"])

    def test_watches_stop_at_the_monthly_ceiling_whatever_the_plan_has_left(self):
        self.add(below=600)
        config.save_setting(config.WATCH_MONTHLY_VAR, "40")
        self.run_at()  # 3 searches
        config.save_setting(config.WATCH_MONTHLY_VAR, "3")
        report = self.run_at(1)
        self.assertEqual(report.lines, [f"{TITLE}: Not checked: watches have used the 3 searches a month they may. It stays due."])
        self.assertEqual(len(FakeSerpApi.searches), 3)

    def test_what_was_used_before_the_plans_month_began_does_not_count(self):
        self.add(below=600)
        self.run_at()
        config.save_setting(config.WATCH_MONTHLY_VAR, "3")
        FakeSerpApi.account_reply["plan_renewal_date"] = "2030-03-01"  # the month now began on 1 February
        self.assertEqual(self.run_at(25).spent, 1)

    def test_a_run_told_to_stop_at_one_search_leaves_the_second_watch_due(self):
        self.add()
        self.add(watch.trip("BOS", "WAW", DEPART, BACK))
        report = self.run_at(max_searches=1)
        self.assertEqual(report.lines[1], "BOS -> WAW, Tue 12 Mar - Tue 19 Mar: Not checked: this run's limit of 1 is used up. It stays due.")
        self.assertEqual((report.spent, len(FakeSerpApi.searches)), (1, 1))

    def test_a_run_limited_to_three_confirms_one_alert_and_no_more(self):
        self.add(below=600)
        self.add(watch.trip("BOS", "WAW", DEPART, BACK), below=600)
        report = self.run_at(max_searches=3)
        self.assertEqual((report.spent, len(FakeSerpApi.searches), len(FakeNtfy.sent)), (3, 3, 1))


class FailureTest(RunCase):
    """A check that could not be made stays due, is logged, and after two missed intervals the owner is told once."""

    def test_a_search_that_fails_leaves_the_watch_due_and_is_tried_at_the_next_run(self):
        self.add(every_days=4)
        FakeSerpApi.fail_after = 0
        report = self.run_at()
        self.assertTrue(report.failed)
        self.assertRegex(report.lines[0], rf"^{TITLE}: Check failed: stopped after 0 of 1 live searches: .*\. It stays due\.$")
        self.assertEqual(self.last("finished", "outcome", "searches"), (0, "failed", 0))
        FakeSerpApi.fail_after = None
        self.assertEqual(self.run_at(1).lines, [f"{TITLE}: $520 listed. No rule fired. 1 search."])

    def test_a_follow_up_that_fails_counts_what_was_spent_and_leaves_it_due(self):
        self.add(below=600)
        FakeSerpApi.fail_after = 1
        report = self.run_at()
        self.assertTrue(report.failed)
        self.assertEqual(self.last("finished", "outcome", "price", "searches"), (0, "failed", 520, 1))
        self.assertEqual(FakeNtfy.sent, [])

    def test_an_account_that_cannot_be_read_means_nothing_is_spent(self):
        self.add()
        with mock.patch.object(FakeSerpApi, "account", side_effect=SerpApiError("network error: no route to host")):
            report = self.run_at()
        self.assertEqual(
            report.lines,
            [f"{TITLE}: Check failed: the account could not be read: network error: no route to host. It stays due."],
        )
        self.assertEqual((report.failed, FakeSerpApi.searches), (True, []))

    def test_without_an_alert_topic_nothing_is_checked_or_spent(self):
        self.add(below=600)
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}):
            with self.assertRaisesRegex(UserError, "no alert topic is set up, so nothing was checked. Run: py -m flightsleuth watch setup"):
                self.run_at()
        self.assertEqual((FakeSerpApi.searches, self.checks()), ([], []))

    def test_the_owner_is_told_once_when_a_watch_has_missed_two_intervals(self):
        self.add()
        FakeSerpApi.fail_after = 0
        for days in (0, 1):
            self.assertEqual(len(self.run_at(days).lines), 1)
        self.assertEqual(FakeNtfy.sent, [])
        report = self.run_at(2)
        self.assertEqual(report.lines[1], f"{TITLE}: you have been told that it has gone unchecked.")
        (sent,) = FakeNtfy.sent
        self.assertEqual(sent["title"], "FlightSleuth has not checked JFK to LHR")
        self.assertRegex(
            sent["message"],
            r"^Tue 12 Mar - Tue 19 Mar\. It has never been checked, and a check has been due since Thu 10 Jan\.\n"
            r"The last try: Check failed: stopped after 0 of 1 live searches: .* It stays due\.\n"
            r'See where it stands with "py -m flightsleuth watch list"\. '
            r"You will not be told again until it has been checked\.$",
        )
        self.assertNotIn("click", sent)
        for days in (3, 4, 5):
            self.run_at(days)
        self.assertEqual(len(FakeNtfy.sent), 1)

    def test_a_later_outage_is_told_of_again(self):
        self.add()
        FakeSerpApi.fail_after = 0
        for days in (0, 1, 2):
            self.run_at(days)
        FakeSerpApi.fail_after = None
        self.run_at(3)  # checked: the outage is over
        FakeSerpApi.fail_after = 1
        for days in (4, 5):
            self.run_at(days)
        self.assertEqual(len(FakeNtfy.sent), 2)
        self.assertIn("It was last checked on Sun 13 Jan, and a check has been due since Mon 14 Jan.", FakeNtfy.sent[1]["message"])

    def test_a_watch_checked_every_four_days_counts_eight_days_before_it_tells(self):
        self.add(every_days=4)
        FakeSerpApi.fail_after = 0
        for days in range(8):
            self.run_at(days)
        self.assertEqual(FakeNtfy.sent, [])
        self.run_at(8)
        self.assertEqual(len(FakeNtfy.sent), 1)

    def test_a_watch_skipped_for_want_of_searches_is_told_of_too(self):
        self.add()
        FakeSerpApi.account_reply["total_searches_left"] = 0
        for days in (0, 1, 2):
            self.run_at(days)
        self.assertIn("The last try: Not checked: the account has no searches left this month. It stays due.", FakeNtfy.sent[0]["message"])

    def test_when_the_outage_cannot_be_told_it_is_tried_again_at_the_next_run(self):
        self.add()
        FakeSerpApi.fail_after = 0
        FakeNtfy.fail = NO_NETWORK
        for days in (0, 1, 2):
            report = self.run_at(days)
        self.assertEqual(report.lines[1], f"{TITLE}: it has gone unchecked, and telling you so failed: the alert could not be sent: network error: no route to host.")
        FakeNtfy.fail = None
        self.run_at(3)
        self.assertEqual(len(FakeNtfy.sent), 1)


class UnsentAlertTest(RunCase):
    """An alert that could not reach ntfy is sent at the next run without another search, while its price is recent."""

    def setUp(self):
        super().setUp()
        self.add(below=600, every_days=4)
        FakeNtfy.fail = NO_NETWORK
        self.first = self.run_at()
        FakeNtfy.fail = None
        FakeSerpApi.searches.clear()

    def test_the_check_is_finished_and_says_the_alert_did_not_go(self):
        self.assertEqual(
            self.first.lines,
            [
                f"{TITLE}: $520 confirmed (below the $600 you set), but the alert did not go: "
                f"the alert could not be sent: network error: no route to host. The next run tries again. 3 searches."
            ],
        )
        self.assertEqual(self.last("finished", "outcome"), (1, "alert not sent"))
        self.assertFalse(self.first.failed)
        (alert,) = self.history.rows("watch_alerts")
        self.assertEqual((alert["sent"], alert["price"]), (0, 520))

    def test_the_next_run_sends_it_marked_late_and_spends_nothing(self):
        report = self.run_at(1)
        self.assertEqual(
            report.lines,
            [f"{TITLE}: not due until Mon 14 Jan.", f"{TITLE}: the alert about $520 that did not go before has now been sent."],
        )
        self.assertEqual(FakeSerpApi.searches, [])
        (sent,) = FakeNtfy.sent
        self.assertEqual((sent["title"], sent["click"]), ("JFK to LHR: $520 round trip", MOMONDO))
        self.assertTrue(sent["message"].startswith("Sent late: ntfy could not be reached when this price was fetched.\nTue 12 Mar"))
        self.assertRegex(sent["message"], rf"Return flights fetched {STAMP}\.")

    def test_once_sent_it_counts_as_the_price_alerted(self):
        self.run_at(1)
        self.run_at(4)
        self.assertEqual((self.last("outcome"), len(FakeNtfy.sent)), (("repeat",), 1))

    def test_it_is_not_sent_twice(self):
        self.run_at(1)
        self.run_at(2)
        self.assertEqual(len(FakeNtfy.sent), 1)

    def test_a_second_failure_is_kept_and_tried_once_more_inside_a_day(self):
        FakeNtfy.fail = NO_NETWORK
        report = watch.run_due(now=NOW + timedelta(hours=6))
        self.assertIn("the alert about $520 that did not go before still did not go: the alert could not be sent", report.lines[1])
        FakeNtfy.fail = None
        watch.run_due(now=NOW + timedelta(hours=20))
        self.assertEqual(len(FakeNtfy.sent), 1)
        self.assertEqual([alert["sent"] for alert in self.history.rows("watch_alerts")], [0, 0, 1])

    def test_after_a_day_it_is_let_go_and_the_next_check_looks_again(self):
        self.assertEqual(len(self.run_at(2).lines), 1)  # two days on: too old to send as it stands
        self.assertEqual(FakeNtfy.sent, [])
        age_the_cache(hours=96)
        report = self.run_at(4)
        self.assertEqual(report.lines, [f"{TITLE}: $520 confirmed. Alert sent: below the $600 you set. 3 searches."])
        self.assertFalse(FakeNtfy.sent[0]["message"].startswith("Sent late"))


class RetireTest(RunCase):
    def test_a_watch_is_still_checked_on_the_day_it_departs(self):
        self.add()
        self.assertEqual(self.run_at((DEPART - TODAY).days).spent, 1)

    def test_the_day_after_it_retires_itself_and_is_never_checked_again(self):
        self.add()
        days = (DEPART - TODAY).days + 1
        report = self.run_at(days)
        self.assertEqual(report.lines, [f"{TITLE}: retired. Its departure date has passed.", "There are no watches to check."])
        self.assertEqual(FakeSerpApi.searches, [])
        (event,) = self.history.rows("watch_events")
        self.assertEqual((event["event"], event["detail"]), ("retired", "its departure date has passed"))
        self.assertEqual(self.run_at(days + 1).lines, ["There are no watches to check."])
        self.assertEqual(watch.listed(), [])
        self.assertEqual([standing.ended for standing in watch.listed(ended=True)], ["retired"])

    def test_a_retired_watch_makes_room_for_another(self):
        for _ in range(3):
            self.add()
        self.run_at((DEPART - TODAY).days + 1)
        self.add(watch.trip("JFK", "LHR", date(2030, 6, 1)), days=(DEPART - TODAY).days + 1)


class DryRunTest(RunCase):
    def test_it_says_what_is_due_and_the_most_it_could_cost_and_does_nothing(self):
        self.add()
        self.add(watch.trip("JFK", "LHR", DEPART), every_days=2)
        with mock.patch.object(FakeSerpApi, "account", side_effect=AssertionError("a dry run needs no account")):
            report = self.run_at(dry_run=True)
        self.assertEqual(
            report.lines,
            [
                f"{TITLE}: due. 1 search, and up to 3 more if the price is worth an alert.",
                "JFK -> LHR, Tue 12 Mar: due. 1 search, and up to 1 more if the price is worth an alert.",
            ],
        )
        self.assertEqual((FakeSerpApi.searches, FakeNtfy.posts, self.checks(), self.history.rows("provider_calls")), ([], [], [], []))

    def test_it_retires_nothing(self):
        self.add()
        report = self.run_at((DEPART - TODAY).days + 1, dry_run=True)
        self.assertEqual(report.lines[0], f"{TITLE}: retired. Its departure date has passed.")
        self.assertEqual(self.history.rows("watch_events"), [])

    def test_it_needs_no_topic(self):
        self.add()
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}):
            self.assertEqual(len(self.run_at(dry_run=True).lines), 1)


class AddTest(RunCase):
    """At most three watches, and none the searches cannot cover."""

    def test_a_watch_is_kept_with_its_rules_and_given_the_next_number(self):
        added = watch.add(LONDON, below=450, drop_percent=20, typical=False, every_days=4, now=NOW)
        self.assertTrue(added.kept)
        self.assertEqual(added.watch, watch.Watch(1, LONDON, 450, 20.0, False, 4, TODAY))
        self.assertEqual([standing.watch for standing in watch.listed()], [added.watch])
        self.assertEqual(self.add().id, 2)

    def test_adding_spends_nothing(self):
        self.add()
        self.assertEqual((FakeSerpApi.searches, FakeNtfy.posts), ([], []))

    def test_the_sums_come_back_with_it(self):
        added = watch.add(LONDON, every_days=4, now=NOW)
        # 10 January to 1 February: the 10th, 14th, 18th, 22nd, 26th and 30th.
        self.assertEqual((added.budget.now.count, added.budget.now.needed, added.budget.now.available), (6, 9, 150))
        self.assertEqual(render.budget_lines(added.budget)[0], "Until the searches reset on Fri 01 Feb:")

    def test_a_dry_run_gives_the_sums_and_keeps_nothing(self):
        added = watch.add(LONDON, now=NOW, dry_run=True)
        self.assertEqual((added.kept, added.watch.id, added.budget.fits), (False, 0, True))
        self.assertEqual(watch.listed(), [])

    def test_a_fourth_watch_is_refused(self):
        for _ in range(3):
            self.add()
        with self.assertRaisesRegex(UserError, "there are already 3 watches, which is the most. Remove one first"):
            self.add()

    def test_a_watch_the_searches_cannot_cover_is_refused_with_the_sums_and_an_interval_that_would_fit(self):
        FakeSerpApi.account_reply["total_searches_left"] = 9
        with self.assertRaises(watch.OverBudget) as caught:
            self.add()  # daily: 22 checks and 3 held
        self.assertEqual(str(caught.exception), "the searches cannot cover this watch. Checked every 4 days it would fit.")
        self.assertEqual((caught.exception.fit, caught.exception.budget.now.needed, caught.exception.budget.now.spare), (4, 25, -16))
        self.assertEqual(watch.listed(), [])
        self.add(every_days=4)

    def test_a_second_watch_is_set_against_what_the_first_already_needs(self):
        FakeSerpApi.account_reply["total_searches_left"] = 12
        self.add(every_days=4)  # 6 checks and 3 held: 9 of the 12
        with self.assertRaises(watch.OverBudget) as caught:
            self.add(every_days=4)
        self.assertEqual((caught.exception.budget.now.needed, caught.exception.fit), (18, None))

    def test_the_owners_ceiling_refuses_what_the_plan_would_allow(self):
        config.save_setting(config.WATCH_MONTHLY_VAR, "20")
        with self.assertRaises(watch.OverBudget):
            self.add()
        config.save_setting(config.WATCH_MONTHLY_VAR, "40")
        self.add()

    def test_a_watch_keeps_googles_order_whatever_the_search_it_came_from_asked_for(self):
        sorted_search = scan.plan(scan.Request("JFK", "LHR", DEPART, return_date=BACK, bags=1), today=TODAY).trips[0]
        self.assertEqual(watch.add(sorted_search, now=NOW).watch.trip, LONDON)

    def test_what_makes_no_sense_is_refused(self):
        for wanted, rules, message in (
            (watch.trip("JFK", "LHR", date(2030, 1, 9)), {}, "the departure date is in the past"),
            (watch.trip("JFK", "LHR", DEPART, date(2030, 3, 1)), {}, "the return date is before the departure date"),
            (LONDON, {"below": 0}, "the price to alert below must be more than nothing"),
            (LONDON, {"drop_percent": 100}, "a percentage from 0 to 99"),
            (LONDON, {"every_days": 0}, "the days between checks must be from 1 to 60"),
            (LONDON, {"drop_percent": 0, "typical": False}, "a watch needs at least one rule"),
        ):
            with self.assertRaisesRegex(UserError, message):
                self.add(wanted, **rules)
        self.assertEqual(watch.listed(), [])

    def test_a_watch_leaving_today_can_still_be_added(self):
        self.add(watch.trip("JFK", "LHR", TODAY))


class RemoveTest(RunCase):
    def test_a_removed_watch_is_no_longer_checked_and_its_rows_stay(self):
        self.add()
        self.run_at()
        self.assertEqual(watch.remove(1, now=NOW).trip, LONDON)
        self.assertEqual(self.run_at(1).lines, ["There are no watches to check."])
        self.assertEqual((len(self.history.rows("watches")), len(self.checks()), watch.listed()), (1, 1, []))
        self.assertEqual([standing.ended for standing in watch.listed(ended=True)], ["removed"])

    def test_a_number_that_is_not_a_watch_is_said_plainly(self):
        with self.assertRaisesRegex(UserError, "there is no watch 7. See them with: py -m flightsleuth watch list"):
            watch.remove(7)

    def test_a_watch_cannot_be_removed_twice(self):
        self.add()
        watch.remove(1, now=NOW)
        with self.assertRaisesRegex(UserError, "watch 1 was already removed"):
            watch.remove(1)


class SetupTest(RunCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}))

    def test_the_first_setup_makes_a_topic_and_keeps_it_in_the_env_file(self):
        topic, new = watch.setup()
        self.assertTrue(new)
        self.assertRegex(topic, r"^flightsleuth-[-_A-Za-z0-9]{43}$")
        self.assertEqual(Path(".env").read_text(encoding="utf-8"), f"FLIGHTSLEUTH_NTFY_TOPIC={topic}\n")
        self.assertEqual(FakeNtfy.posts, [])

    def test_setting_up_again_gives_the_same_topic(self):
        topic, _ = watch.setup()
        self.assertEqual(watch.setup(), (topic, False))

    def test_a_new_topic_can_be_asked_for_and_replaces_the_old_one(self):
        old, _ = watch.setup()
        topic, new = watch.setup(new=True)
        self.assertTrue(new)
        self.assertNotEqual(topic, old)
        self.assertEqual(config.ntfy_topic(), topic)

    def test_the_key_in_the_same_file_is_left_alone(self):
        Path(".env").write_text("SERPAPI_API_KEY=k\n", encoding="utf-8")
        topic, _ = watch.setup()
        self.assertEqual(Path(".env").read_text(encoding="utf-8"), f"SERPAPI_API_KEY=k\nFLIGHTSLEUTH_NTFY_TOPIC={topic}\n")


class TestAlertTest(RunCase):
    def test_it_sends_a_message_that_is_plainly_a_test_and_makes_no_search(self):
        sent = watch.send_test()
        self.assertEqual(
            FakeNtfy.sent,
            [
                {
                    "topic": TOPIC,
                    "title": "FlightSleuth test alert",
                    "message": "This is a test. No search was made and no price was fetched. A real alert names the route, "
                    "the dates and the price, says why it fired, and opens the site to check it on when tapped.",
                }
            ],
        )
        self.assertEqual((sent.click, FakeSerpApi.searches, self.history.rows("provider_calls")), ("", [], []))

    def test_with_a_watch_it_opens_that_watchs_dates_on_momondo_as_a_real_alert_would(self):
        self.add()
        watch.send_test()
        self.assertEqual(FakeNtfy.sent[0]["click"], MOMONDO)
        self.assertNotIn("$", FakeNtfy.sent[0]["message"])  # a test never shows a price

    def test_without_a_topic_it_says_how_to_get_one(self):
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}):
            with self.assertRaisesRegex(notify.NotifyError, "py -m flightsleuth watch setup"):
                watch.send_test()


class LogTest(RunCase):
    def test_each_line_of_a_run_is_added_with_the_time_of_the_run(self):
        watch.log(["first", "second"], NOW)
        watch.log(["third"], NOW + timedelta(days=1, minutes=5))
        self.assertEqual(
            config.log_path().read_text(encoding="utf-8"),
            "2030-01-10 12:00  first\n2030-01-10 12:00  second\n2030-01-11 12:05  third\n",
        )


class RecordedReplyTest(unittest.TestCase):
    """A check run over the real replies in tests/fixtures.

    The listing is the unsorted round trip recorded on 2026-10-09. The return flights and the
    sellers are the recorded follow-ups of the sorted search made half an hour earlier, so they
    belong to other flights than the fare listed here. This shows that a check reads real replies;
    it does not show that these flights go together.
    """

    def setUp(self):
        install(self)
        fixtures = Path(__file__).parent / "fixtures"

        def recorded(name):
            return json.loads((fixtures / name).read_text(encoding="utf-8"))

        self.listing = recorded("roundtrip_outbound_unsorted.json")

        def replay(fake, query):
            FakeSerpApi.searches.append(query)
            if query.booking_token:
                return recorded("roundtrip_booking.json")
            return recorded("roundtrip_return.json") if query.departure_token else self.listing

        self.enterContext(mock.patch.object(FakeSerpApi, "search", replay))
        self.enterContext(mock.patch.dict(os.environ, {config.TOPIC_VAR: TOPIC}))
        FakeSerpApi.account_reply.update(total_searches_left=31, searches_per_month=250, plan_renewal_date="2026-11-08")
        self.now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        self.trip = watch.trip("JFK,EWR", "LHR", date(2026, 12, 8), date(2026, 12, 15))
        self.history = History(config.history_path())
        self.addCleanup(self.history.close)

    def test_the_real_reply_gives_the_price_the_median_and_the_typical_range(self):
        watch.add(self.trip, every_days=4, now=self.now)
        report = watch.run_due(now=self.now)
        self.assertEqual(report.lines, ["JFK,EWR -> LHR, Tue 08 Dec - Tue 15 Dec: $693 listed. No rule fired. 1 search."])
        (row,) = self.history.rows("watch_checks")
        # Google's daily lows for 10 September to 8 October: 29 of them, the last the day before the check.
        lows = [price for _, price in self.listing["price_insights"]["price_history"][-29:]]
        self.assertEqual((row["price"], row["median"], row["typical_low"], row["typical_high"]), (693, statistics.median(lows), 480, 830))
        self.assertEqual(row["fetched_at"], "2026-10-09T01:04:44+00:00")  # the time SerpApi says it asked Google
        self.assertEqual(len(self.history.rows("google_price_history")), 61)

    def test_a_price_set_above_it_fires_and_the_chain_is_read_through_to_the_sellers(self):
        watch.add(self.trip, below=700, every_days=4, now=self.now)
        report = watch.run_due(now=self.now)
        self.assertEqual([query.endpoint for query in FakeSerpApi.searches], ["search", "return_flights", "booking_options"])
        # The recorded return flights total $695. The recorded sellers offer $694, as two tickets.
        self.assertEqual(report.lines, ["JFK,EWR -> LHR, Tue 08 Dec - Tue 15 Dec: $695 confirmed. Alert sent: below the $700 you set. 3 searches."])
        (sent,) = FakeNtfy.sent
        self.assertEqual(sent["title"], "JFK,EWR to LHR: $695 round trip")
        self.assertEqual(sent["click"], "https://www.momondo.com/flight-search/JFK,EWR-LHR/2026-12-08/2026-12-15?sort=price_a")
        self.assertIn("Why: below the $700 you set.", sent["message"])
        self.assertIn("On one ticket it is $797 from United.", sent["message"])
        self.assertIn("$694 is two separate tickets (", sent["message"])
        self.assertLess(len(sent["message"].encode("utf-8")), 4096)  # past that, ntfy turns a message into an attachment


class RealCheckTest(unittest.TestCase):
    """The one real check Phase 4 paid for, replayed from the three replies it fetched on 2026-10-09.

    Unlike the class above, these belong together: the return flights were fetched with a token
    from this unsorted listing, and the sellers with a token from those return flights. That an
    unsorted listing's token leads anywhere is what no earlier reply had shown.
    """

    def setUp(self):
        install(self)
        fixtures = Path(__file__).parent / "fixtures"
        self.listing, self.returns, self.booking = (
            json.loads((fixtures / name).read_text(encoding="utf-8"))
            for name in ("watch_listing_unsorted.json", "watch_return.json", "watch_booking.json")
        )

        def replay(fake, query):
            FakeSerpApi.searches.append(query)
            if query.booking_token:
                return self.booking
            return self.returns if query.departure_token else self.listing

        self.enterContext(mock.patch.object(FakeSerpApi, "search", replay))
        self.enterContext(mock.patch.dict(os.environ, {config.TOPIC_VAR: TOPIC}))
        FakeSerpApi.account_reply.update(total_searches_left=31, searches_per_month=250, plan_renewal_date="2026-11-08")
        self.now = datetime(2026, 10, 9, 7, 28, tzinfo=timezone.utc)
        self.trip = watch.trip("JFK,EWR", "LHR", date(2026, 12, 8), date(2026, 12, 15))
        self.history = History(config.history_path())
        self.addCleanup(self.history.close)

    def check(self, **rules):
        watch.add(self.trip, every_days=4, now=self.now, **rules)
        return watch.run_due(now=self.now, max_searches=3)

    def test_the_listing_was_asked_for_in_googles_own_order_and_every_fare_in_it_has_a_token(self):
        self.assertNotIn("sort_by", self.listing["search_parameters"])
        fares = self.listing["best_flights"] + self.listing["other_flights"]
        self.assertEqual((len(fares), sum(1 for fare in fares if fare.get("departure_token"))), (69, 69))

    def test_the_cheapest_fare_listed_is_again_the_price_google_calls_its_lowest(self):
        fares = self.listing["best_flights"] + self.listing["other_flights"]
        self.assertEqual((min(fare["price"] for fare in fares), self.listing["price_insights"]["lowest_price"]), (754, 754))

    def test_left_to_its_own_rules_the_check_is_quiet_and_costs_one_search(self):
        report = self.check()
        self.assertEqual(report.lines, ["JFK,EWR -> LHR, Tue 08 Dec - Tue 15 Dec: $754 listed. No rule fired. 1 search."])
        (row,) = self.history.rows("watch_checks")
        # $754 against a 30-day median of $662.50 and a typical range that starts at $480.
        self.assertEqual((row["price"], row["median"], row["typical_low"], row["typical_high"]), (754, 662.5, 480, 830))
        self.assertEqual(row["fetched_at"], "2026-10-09T07:28:29+00:00")

    def test_the_token_of_an_unsorted_listing_fetches_return_flights_and_theirs_fetches_the_sellers(self):
        report = self.check(below=5000)
        asked = FakeSerpApi.searches
        self.assertEqual([query.endpoint for query in asked], ["search", "return_flights", "booking_options"])
        # The code followed the very tokens the real run did.
        self.assertEqual(asked[1].departure_token, self.returns["search_parameters"]["departure_token"])
        self.assertEqual(asked[2].booking_token, self.booking["search_parameters"]["booking_token"])
        self.assertEqual(
            report.lines,
            ["JFK,EWR -> LHR, Tue 08 Dec - Tue 15 Dec: $754 confirmed. Alert sent: below the $5,000 you set. 3 searches."],
        )
        (row,) = self.history.rows("watch_checks")
        self.assertEqual((row["price"], row["confirmed_price"], row["no_returns"], row["searches"]), (754, 754, "[]", 3))
        self.assertEqual(
            row["itinerary_key"], "FI622@2026-12-08T19:35>FI450@2026-12-09T07:35|LX345@2026-12-15T06:00>LX18@2026-12-15T17:15"
        )

    def test_the_alert_reads_as_the_one_that_reached_the_owners_phone(self):
        self.check(below=5000)
        (sent,) = FakeNtfy.sent
        self.assertEqual(sent["title"], "JFK,EWR to LHR: $754 round trip")
        self.assertEqual(sent["click"], "https://www.momondo.com/flight-search/JFK,EWR-LHR/2026-12-08/2026-12-15?sort=price_a")
        self.assertRegex(
            sent["message"],
            r"^Tue 08 Dec - Tue 15 Dec\. Why: below the \$5,000 you set\.\n"
            r"Out: EWR->LHR Tue 08 Dec 19:35 -> 10:50 \+1, 1 stop, 10h 15m, Icelandair \(FI 622, FI 450\)\.\n"
            r"Back: LHR->EWR Tue 15 Dec 06:00 -> 20:30, 1 stop, 19h 30m, SWISS \(LX 345, LX 18\)\.\n"
            rf"Return flights fetched {STAMP}\.\n"
            r"\$742 is two separate tickets \(AranGrant \$342 \+ SWISS \$400\)\. "
            r"On one ticket it is \$753 from Expedia, an agency, not the airline\.\n"
            r"Tap to check it on Momondo\.$",
        )

    def test_the_three_prices_of_one_fare_are_each_what_their_reply_said(self):
        # Listed and confirmed at $754; sold as two tickets for $742 and as one for $753.
        back = self.returns["other_flights"]
        self.assertEqual((len(back), min(option["price"] for option in back)), (4, 754))
        sold = [(option["together"]["book_with"], option["together"]["price"], bool(option.get("separate_tickets"))) for option in self.booking["booking_options"]]
        self.assertEqual(sold, [("AranGrant, SWISS", 742, True), ("Expedia", 753, False), ("Orbitz", 753, False)])


if __name__ == "__main__":
    unittest.main()
