import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from flightsleuth import scan
from tests.fakes import FakeSerpApi, install, reply


def request(**changes):
    return scan.Request("JFK", "LHR", date(2030, 3, 12), **changes)


class CapTest(unittest.TestCase):
    def setUp(self):
        install(self)

    def test_run_refuses_a_plan_over_the_cap_whoever_calls_it(self):
        plan = scan.plan(request(flex=13))  # 27 departure dates
        with self.assertRaises(scan.OverCap) as caught:
            scan.run(plan)
        self.assertEqual((caught.exception.live, caught.exception.limit), (27, 25))
        self.assertEqual(FakeSerpApi.searches, [])

    def test_the_cap_can_be_raised_on_purpose(self):
        scan.run(scan.plan(request(flex=13)), max_searches=27)
        self.assertEqual(len(FakeSerpApi.searches), 27)

    def test_saved_replies_do_not_count_towards_the_cap(self):
        scan.run(scan.plan(request(flex=12)))  # 25 live: allowed
        plan = scan.plan(request(flex=13))
        self.assertEqual((plan.saved, plan.live), (25, 2))
        scan.run(plan)
        self.assertEqual(len(FakeSerpApi.searches), 27)


class ProvenanceTest(unittest.TestCase):
    """Every offer says where its price came from, when, and whether that was just now."""

    def setUp(self):
        install(self)

    def search(self):
        outcome = scan.run(scan.plan(request()))
        (result,) = outcome.results.values()
        (offer,) = result.offers
        return offer

    def test_a_fresh_search_is_marked_live_with_the_time_it_was_fetched(self):
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        offer = self.search()
        self.assertEqual(offer.source, "serpapi")
        self.assertTrue(offer.verified_live)
        self.assertTrue(before <= offer.fetched_at <= datetime.now(timezone.utc) + timedelta(seconds=1))

    def test_a_replayed_search_is_not_live_and_keeps_its_original_time(self):
        first = self.search()
        again = self.search()
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertFalse(again.verified_live)
        self.assertEqual(again.fetched_at, first.fetched_at)
        self.assertEqual((again.price, again.source), (first.price, "serpapi"))

    def test_the_time_the_source_gives_wins_over_this_machines_clock(self):
        def stamped(self, query):
            metadata = {"status": "Success", "processed_at": "2030-01-01 12:00:00 UTC"}
            return {**reply(query), "search_metadata": metadata}

        with mock.patch.object(FakeSerpApi, "search", stamped):
            offer = self.search()
        self.assertEqual(offer.fetched_at, datetime(2030, 1, 1, 12, tzinfo=timezone.utc))
        self.assertTrue(offer.verified_live)
        self.assertEqual(self.search().fetched_at, offer.fetched_at)


if __name__ == "__main__":
    unittest.main()
