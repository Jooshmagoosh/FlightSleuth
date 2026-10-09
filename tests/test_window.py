import os
import sqlite3
import unittest
from datetime import date, timedelta
from unittest import mock

from flightsleuth import config, planner, providers, scan
from flightsleuth.errors import UserError
from flightsleuth.models import Explore, Query
from tests.fakes import FakeSerpApi, install
from tests.test_providers import CheapSource

TODAY = date(2030, 1, 1)
# The search the brief measures by: 91 departure days, trips of 5 to 9 nights, 455 date pairs.
EARLIEST, LATEST, NIGHTS = date(2030, 3, 1), date(2030, 5, 30), (5, 9)
BEST = (date(2030, 4, 16), date(2030, 4, 23))


def request(**changes):
    options = {"window": (EARLIEST, LATEST), "nights": NIGHTS, **changes}
    return scan.Request("JFK", "LHR", options["window"][0], **options)


def valley(query):
    """Fares that fall steadily towards one best pair, so refining has something to walk down."""
    if query.return_date is None:
        return 450
    off = abs((query.depart_date - BEST[0]).days)
    length = abs((query.return_date - query.depart_date).days - 7)
    return 400 + 10 * off + 15 * length


def pairs(trips):
    return [(trip.depart_date, trip.return_date) for trip in trips]


class SampleTest(unittest.TestCase):
    def test_the_whole_window_is_455_pairs(self):
        self.assertEqual(len(planner.window_pairs(EARLIEST, LATEST, NIGHTS)), 455)

    def test_a_sample_is_the_size_asked_for_and_stays_inside_the_request(self):
        sample = planner.sample(EARLIEST, LATEST, NIGHTS, 12)
        self.assertEqual(len(sample), 12)
        self.assertEqual(len(set(sample)), 12)
        self.assertEqual(sample, sorted(sample))
        for depart, back in sample:
            self.assertTrue(EARLIEST <= depart <= LATEST)
            self.assertIn((back - depart).days, range(5, 10))

    def test_a_sample_gives_every_weekday_and_every_length_a_turn(self):
        sample = planner.sample(EARLIEST, LATEST, NIGHTS, 12)
        self.assertEqual({depart.weekday() for depart, _ in sample}, set(range(7)))
        self.assertEqual({(back - depart).days for depart, back in sample}, {5, 6, 7, 8, 9})

    def test_a_sample_reaches_from_one_end_of_the_window_to_the_other(self):
        departures = [depart for depart, _ in planner.sample(EARLIEST, LATEST, NIGHTS, 12)]
        self.assertLess(departures[0], EARLIEST + timedelta(days=8))
        self.assertGreater(departures[-1], LATEST - timedelta(days=8))
        self.assertLessEqual(max((b - a).days for a, b in zip(departures, departures[1:])), 14)

    def test_the_same_request_always_gives_the_same_sample(self):
        self.assertEqual(planner.sample(EARLIEST, LATEST, NIGHTS, 12), planner.sample(EARLIEST, LATEST, NIGHTS, 12))

    def test_more_samples_than_days_come_back_for_other_lengths(self):
        sample = planner.sample(date(2030, 3, 1), date(2030, 3, 4), (6, 8), 9)
        self.assertEqual(len(set(sample)), 9)
        self.assertEqual({depart for depart, _ in sample}, {date(2030, 3, day) for day in (1, 2, 3, 4)})

    def test_asking_for_more_than_there_is_gives_every_pair_once(self):
        sample = planner.sample(date(2030, 3, 1), date(2030, 3, 3), (6, 7), 50)
        self.assertEqual(sample, planner.window_pairs(date(2030, 3, 1), date(2030, 3, 3), (6, 7)))

    def test_one_way_samples_have_no_return(self):
        sample = planner.sample(EARLIEST, LATEST, None, 5)
        self.assertEqual(len(sample), 5)
        self.assertTrue(all(back is None for _, back in sample))


class NeighboursTest(unittest.TestCase):
    def test_the_whole_trip_slides_first_then_each_end_moves(self):
        near = planner.neighbours((date(2030, 4, 10), date(2030, 4, 17)), EARLIEST, LATEST, NIGHTS)
        self.assertEqual(
            near,
            [
                (date(2030, 4, 9), date(2030, 4, 16)),
                (date(2030, 4, 11), date(2030, 4, 18)),
                (date(2030, 4, 10), date(2030, 4, 16)),
                (date(2030, 4, 10), date(2030, 4, 18)),
                (date(2030, 4, 9), date(2030, 4, 17)),
                (date(2030, 4, 11), date(2030, 4, 17)),
            ],
        )

    def test_nothing_leaves_the_window_or_the_allowed_lengths(self):
        near = planner.neighbours((EARLIEST, EARLIEST + timedelta(days=5)), EARLIEST, LATEST, NIGHTS)
        self.assertEqual(
            near,
            [(date(2030, 3, 2), date(2030, 3, 7)), (date(2030, 3, 1), date(2030, 3, 7)), ],
        )

    def test_a_one_way_only_slides(self):
        near = planner.neighbours((date(2030, 4, 10), None), EARLIEST, LATEST, None)
        self.assertEqual(near, [(date(2030, 4, 9), None), (date(2030, 4, 11), None)])


class ExploreDatesTest(unittest.TestCase):
    def test_a_range_of_nights_maps_to_the_explore_length_it_overlaps(self):
        self.assertEqual(planner.explore_duration((2, 4)), "weekend")
        self.assertEqual(planner.explore_duration((5, 9)), "week")
        self.assertEqual(planner.explore_duration((10, 16)), "two weeks")
        self.assertIsNone(planner.explore_duration((10, 11)))
        self.assertIsNone(planner.explore_duration(None))

    def test_the_months_a_window_touches_are_listed_across_the_new_year(self):
        self.assertEqual(planner.window_months(date(2030, 11, 20), date(2031, 1, 5), date(2030, 10, 8)), [11, 12, 1])

    def test_a_window_beyond_six_months_has_no_explore_months(self):
        self.assertEqual(planner.window_months(date(2031, 3, 1), date(2031, 3, 20), date(2030, 10, 8)), [3])
        self.assertIsNone(planner.window_months(date(2031, 3, 1), date(2031, 4, 2), date(2030, 10, 8)))

    def test_a_suggestion_is_pulled_to_an_allowed_length_or_dropped_if_it_departs_outside(self):
        fit = planner.fit
        self.assertEqual(fit(date(2030, 4, 2), date(2030, 4, 9), EARLIEST, LATEST, NIGHTS), (date(2030, 4, 2), date(2030, 4, 9)))
        self.assertEqual(fit(date(2030, 4, 2), date(2030, 4, 16), EARLIEST, LATEST, NIGHTS), (date(2030, 4, 2), date(2030, 4, 11)))
        self.assertEqual(fit(date(2030, 4, 2), None, EARLIEST, LATEST, NIGHTS), (date(2030, 4, 2), date(2030, 4, 9)))
        self.assertIsNone(fit(date(2030, 6, 2), date(2030, 6, 9), EARLIEST, LATEST, NIGHTS))


class GateTest(unittest.TestCase):
    """The Phase 2 gate: the 455-pair search plans at 25 or fewer by default and about 40 when thorough."""

    def setUp(self):
        install(self)

    def test_the_455_pair_search_plans_within_the_default_cap(self):
        plan = scan.plan(request(), today=TODAY)
        self.assertEqual(plan.strategy, "sampled")
        self.assertEqual((len(plan.calls), plan.refine_calls, plan.check_calls), (12, 7, 6))
        self.assertEqual((plan.live, plan.total, plan.saved), (25, 25, 0))
        scan.check_cap(plan)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_thorough_plans_about_forty_and_looks_closely_at_two_fares(self):
        plan = scan.plan(request(thorough=True), today=TODAY)
        self.assertEqual((len(plan.calls), plan.refine_calls, plan.check_calls, plan.checks), (18, 10, 12, 2))
        self.assertEqual(plan.live, 40)
        scan.check_cap(plan)

    def test_without_the_closer_look_every_search_goes_on_dates(self):
        plan = scan.plan(request(check=False), today=TODAY)
        self.assertEqual((len(plan.calls), plan.refine_calls, plan.check_calls), (16, 9, 0))

    def test_planning_needs_no_key_and_writes_nothing(self):
        with mock.patch.dict(os.environ, {config.KEY_VAR: ""}):
            scan.plan(request(), today=TODAY)
        self.assertFalse(config.history_path().exists())


class WindowRunTest(unittest.TestCase):
    def setUp(self):
        install(self)
        FakeSerpApi.fare = valley
        FakeSerpApi.rich = True

    def run_window(self, **changes):
        plan = scan.plan(request(**changes), today=TODAY)
        return plan, scan.run(plan)

    def test_a_run_never_sends_more_than_the_plan_counted(self):
        plan, outcome = self.run_window()
        self.assertLessEqual(len(FakeSerpApi.searches), 25)
        self.assertEqual(outcome.live, len(FakeSerpApi.searches))
        self.assertEqual(outcome.strategy, "sampled")

    def test_refining_finds_a_cheaper_pair_than_any_in_the_sample(self):
        plan, outcome = self.run_window()
        sampled = min(valley(trip) for trip in plan.trips)
        found = min(outcome.results[trip].cheapest.price for trip in outcome.trips)
        self.assertLess(found, sampled)
        # Seven refining searches on top of the twelve sampled, each a new pair inside the request.
        self.assertEqual(len(outcome.trips), 19)
        self.assertEqual(len(set(pairs(outcome.trips))), 19)
        for depart, back in pairs(outcome.trips):
            self.assertTrue(EARLIEST <= depart <= LATEST)
            self.assertIn((back - depart).days, range(5, 10))

    def test_refining_walks_from_the_best_pair_to_its_neighbours(self):
        plan, outcome = self.run_window()
        best = min(plan.trips, key=valley)
        added = set(pairs(outcome.trips)) - set(pairs(plan.trips))
        near = planner.neighbours((best.depart_date, best.return_date), EARLIEST, LATEST, NIGHTS)
        self.assertTrue(set(near) <= added)

    def test_the_progress_callback_counts_towards_the_plans_ceiling(self):
        seen = []
        plan = scan.plan(request(), today=TODAY)
        scan.run(plan, on_search=lambda done, live: seen.append((done, live)))
        self.assertEqual([done for done, _ in seen], list(range(1, len(seen) + 1)))
        self.assertEqual({live for _, live in seen}, {25})

    def test_running_the_same_window_again_spends_nothing(self):
        _, first = self.run_window()
        spent = len(FakeSerpApi.searches)
        plan, again = self.run_window()
        self.assertEqual(len(FakeSerpApi.searches), spent)
        self.assertEqual((again.live, again.saved), (0, spent))
        self.assertEqual(pairs(again.trips), pairs(first.trips))
        self.assertEqual(again.checks[0].confirmed.price, first.checks[0].confirmed.price)

    def test_an_old_window_run_replays_to_the_same_fare_for_nothing(self):
        _, first = self.run_window()
        spent = len(FakeSerpApi.searches)
        # Two hours on, the saved listings are too old for a new lookup. Nothing new is needed.
        db = sqlite3.connect(config.cache_path())
        with db:
            db.execute("UPDATE responses SET fetched_at = fetched_at - 7200")
        db.close()

        plan, again = self.run_window()
        self.assertEqual((plan.refine_calls, plan.saved), (7, 12))
        self.assertLessEqual(plan.live, 25)
        self.assertEqual((len(FakeSerpApi.searches), again.live), (spent, 0))
        self.assertEqual(pairs(again.trips), pairs(first.trips))
        self.assertEqual(again.checks[0].trip, first.checks[0].trip)
        self.assertEqual(again.checks[0].confirmed.price, first.checks[0].confirmed.price)
        self.assertFalse(again.checks[0].confirmed.verified_live)

    def test_thorough_looks_closely_at_the_two_best_date_pairs(self):
        plan, outcome = self.run_window(thorough=True)
        self.assertLessEqual(len(FakeSerpApi.searches), 40)
        self.assertEqual(len(outcome.checks), 2)
        self.assertNotEqual(outcome.checks[0].trip, outcome.checks[1].trip)
        self.assertLessEqual(outcome.checks[0].confirmed.price, outcome.checks[1].confirmed.price)

    def test_a_smaller_limit_shrinks_the_sample(self):
        plan = scan.plan(request(), today=TODAY, max_searches=15)
        self.assertEqual((len(plan.calls), plan.refine_calls, plan.check_calls, plan.live), (6, 3, 6, 15))
        scan.run(plan)
        self.assertLessEqual(len(FakeSerpApi.searches), 15)

    def test_a_limit_too_small_to_sample_is_refused_plainly(self):
        with self.assertRaisesRegex(UserError, "a limit of 7 live searches leaves too few to sample"):
            scan.plan(request(), today=TODAY, max_searches=7)

    def test_the_account_must_cover_the_ceiling_not_just_the_first_stage(self):
        FakeSerpApi.account_reply["total_searches_left"] = 20
        with self.assertRaisesRegex(UserError, "needs 25 live searches but only 20 are left this month"):
            self.run_window()
        self.assertEqual(FakeSerpApi.searches, [])

    def test_a_window_small_enough_is_priced_in_full(self):
        window = (date(2030, 3, 10), date(2030, 3, 14))
        plan, outcome = self.run_window(window=window, nights=(7, 7))
        self.assertEqual((plan.strategy, len(plan.calls), plan.refine_calls, plan.check_calls), ("grid", 5, 0, 6))
        self.assertEqual(pairs(outcome.trips), planner.window_pairs(*window, (7, 7)))

    def test_a_one_way_window_samples_departure_days(self):
        plan, outcome = self.run_window(nights=None)
        # A one-way closer look is the sellers and one other market: two calls.
        self.assertEqual((plan.strategy, plan.check_calls, plan.live), ("sampled", 2, 25))
        self.assertTrue(all(trip.return_date is None for trip in outcome.trips))
        self.assertLessEqual(len(FakeSerpApi.searches), 25)

    def test_a_window_that_began_before_today_starts_today(self):
        plan = scan.plan(request(window=(date(2029, 12, 20), date(2030, 1, 3)), nights=(7, 7)), today=TODAY)
        self.assertEqual(plan.window, (TODAY, date(2030, 1, 3)))
        self.assertEqual([call.query.depart_date for call in plan.calls], [date(2030, 1, day) for day in (1, 2, 3)])

    def test_two_sources_share_the_cap_between_them(self):
        CheapSource.searches = []
        providers.register(CheapSource)
        self.addCleanup(providers.unregister, CheapSource.name)
        plan = scan.plan(request(), today=TODAY)
        self.assertLessEqual(plan.live, 25)
        self.assertEqual({call.provider for call in plan.calls}, {"serpapi", "cheapsource"})
        outcome = scan.run(plan)
        self.assertLessEqual(len(FakeSerpApi.searches) + len(CheapSource.searches), 25)
        self.assertEqual(outcome.live, len(FakeSerpApi.searches) + len(CheapSource.searches))


class WindowRulesTest(unittest.TestCase):
    def setUp(self):
        install(self)

    def refused(self, message, **changes):
        with self.assertRaisesRegex(UserError, message):
            scan.plan(request(**changes), today=TODAY)

    def test_a_window_that_ends_before_it_starts_is_refused(self):
        self.refused("the date window ends before it starts", window=(LATEST, EARLIEST))

    def test_a_window_wholly_in_the_past_is_refused(self):
        self.refused("the date window is in the past", window=(date(2029, 11, 1), date(2029, 12, 1)))

    def test_a_window_takes_nights_not_a_return_date(self):
        self.refused("needs a trip length in nights", nights=None, return_date=date(2030, 3, 9))

    def test_a_window_and_flexible_days_do_not_mix(self):
        self.refused("flexible days do not apply", flex=2)

    def test_an_unknown_strategy_is_refused(self):
        self.refused("'random' is not a strategy", strategy="random")

    def test_the_other_strategies_need_a_window(self):
        for strategy in ("sampled", "one-way", "explore"):
            with self.assertRaisesRegex(UserError, f"the {strategy} strategy needs a date window"):
                scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 12), strategy=strategy), today=TODAY)


class OneWayStrategyTest(unittest.TestCase):
    """Two one-way prices stand in for each round trip, and only the best few pairs get a real search."""

    def setUp(self):
        install(self)
        FakeSerpApi.rich = True
        # Cheap to leave on the 12th and cheap to come home on the 19th.
        FakeSerpApi.fare = lambda query: (
            {12: 100, 19: 90}.get(query.depart_date.day, 250) if query.return_date is None else 700 - query.depart_date.day
        )
        self.window = (date(2030, 3, 10), date(2030, 3, 14))

    def test_every_leg_is_priced_once_and_the_cheapest_sums_are_verified(self):
        plan = scan.plan(request(window=self.window, nights=(6, 8), strategy="one-way", check=False), today=TODAY)
        # Five ways out, and seven days to come home on: the 16th to the 22nd.
        self.assertEqual((plan.strategy, len(plan.calls), plan.refine_calls, plan.live), ("one-way", 12, 3, 15))
        self.assertTrue(all(call.query.return_date is None for call in plan.calls))

        outcome = scan.run(plan)
        self.assertEqual(len(FakeSerpApi.searches), 15)
        self.assertTrue(outcome.split)
        self.assertEqual(len(outcome.trips), 3)
        self.assertIn((date(2030, 3, 12), date(2030, 3, 19)), pairs(outcome.trips))
        for trip in outcome.trips:
            self.assertIsNotNone(outcome.results[trip].cheapest)

    def test_a_one_way_trip_cannot_use_it(self):
        with self.assertRaisesRegex(UserError, "needs a trip length"):
            scan.plan(request(window=self.window, nights=None, strategy="one-way"), today=TODAY)

    def test_a_window_too_wide_for_it_is_refused_by_the_cap(self):
        plan = scan.plan(request(strategy="one-way"), today=TODAY)
        with self.assertRaises(scan.OverCap):
            scan.run(plan)
        self.assertEqual(FakeSerpApi.searches, [])


class ExploreStrategyTest(unittest.TestCase):
    def setUp(self):
        install(self)
        FakeSerpApi.rich = True
        FakeSerpApi.fare = valley
        self.today = date(2030, 2, 1)

    def test_one_explore_call_a_month_then_the_suggested_dates_are_priced(self):
        FakeSerpApi.explore_reply = {
            "search_metadata": {"status": "Success"},
            "destinations": [{"name": "London", "destination_airport": {"code": "LHR"}, "start_date": "2030-04-15", "flight_price": 410}],
        }
        plan = scan.plan(request(strategy="explore", check=False), today=self.today)
        self.assertEqual([call.query.month for call in plan.calls], [3, 4, 5])
        self.assertTrue(all(isinstance(call.query, Explore) and call.query.duration == "week" for call in plan.calls))
        self.assertEqual((plan.refine_calls, plan.live), (22, 25))

        outcome = scan.run(plan)
        self.assertEqual(outcome.notes, [])
        self.assertLessEqual(len(FakeSerpApi.searches), 25)
        # No end date came back, so the middle of the allowed lengths stands in: seven nights.
        self.assertIn((date(2030, 4, 15), date(2030, 4, 22)), pairs(outcome.trips))
        best = min(outcome.trips, key=lambda trip: outcome.results[trip].cheapest.price)
        self.assertEqual((best.depart_date, best.return_date), BEST)

    def test_when_explore_suggests_nothing_the_window_is_sampled_and_the_run_says_so(self):
        plan = scan.plan(request(strategy="explore", check=False), today=self.today)
        outcome = scan.run(plan)
        self.assertEqual(outcome.notes, ["Explore suggested no dates inside the window, so the window was sampled instead."])
        self.assertGreater(len(outcome.trips), 10)
        self.assertLessEqual(len(FakeSerpApi.searches), 25)

    def test_it_is_refused_for_trip_lengths_explore_has_no_match_for(self):
        with self.assertRaisesRegex(UserError, "suits trips of a weekend, about a week or about two weeks"):
            scan.plan(request(strategy="explore", nights=(10, 11)), today=self.today)

    def test_it_is_refused_beyond_six_months(self):
        with self.assertRaisesRegex(UserError, "only reaches 6 months ahead"):
            scan.plan(request(strategy="explore"), today=date(2029, 10, 1))


class ExploreSearchTest(unittest.TestCase):
    def setUp(self):
        install(self)

    def test_an_explore_search_is_one_call_and_its_places_come_back_stamped(self):
        FakeSerpApi.explore_reply = {
            "search_metadata": {"status": "Success"},
            "destinations": [
                {"name": "Paris", "country": "France", "destination_airport": {"code": "CDG"}, "start_date": "2030-03-05", "flight_price": 58},
                {"name": "London", "country": "United Kingdom", "destination_airport": {"code": "LGW"}, "start_date": "2030-03-03", "flight_price": 36},
            ],
        }
        plan = scan.plan_explore(Explore("MCO", area="/m/02j9z", month=3))
        self.assertEqual((plan.live, plan.total), (1, 1))
        outcome = scan.run(plan)
        self.assertEqual([(place.name, place.price) for place in outcome.destinations], [("London", 36), ("Paris", 58)])
        self.assertTrue(all(place.source == "serpapi" and place.verified_live for place in outcome.destinations))
        self.assertEqual((outcome.live, outcome.searches_left), (1, 249))

        again = scan.run(scan.plan_explore(Explore("MCO", area="/m/02j9z", month=3)))
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertFalse(again.destinations[0].verified_live)
        self.assertEqual(again.destinations[0].fetched_at, outcome.destinations[0].fetched_at)

    def test_a_month_that_does_not_exist_is_refused(self):
        with self.assertRaisesRegex(UserError, "the month must be a number from 1 to 12"):
            scan.plan_explore(Explore("MCO", month=13))


class BaseQueryTest(unittest.TestCase):
    def setUp(self):
        install(self)

    def test_market_and_bags_reach_every_search(self):
        plan = scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 12), flex=1, market="gb", bags=1), today=TODAY)
        self.assertEqual({(call.query.market, call.query.bags) for call in plan.calls}, {("gb", 1)})
        self.assertEqual(plan.calls[0].query, Query("JFK", "LHR", date(2030, 3, 11), market="gb", bags=1))


if __name__ == "__main__":
    unittest.main()
