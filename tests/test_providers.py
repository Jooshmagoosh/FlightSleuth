import contextlib
import io
import os
import unittest
from datetime import date, datetime
from unittest import mock

from flightsleuth import cli, config, providers, scan
from flightsleuth.errors import UserError
from flightsleuth.models import Offer, SearchResult, Segment
from flightsleuth.providers import Allowance, Capability, DataClass, Provider, SerpApiProvider
from tests.fakes import FakeSerpApi, install


class CheapSource(Provider):
    """A second, free source with its own reply format. It always quotes 99."""

    name = "cheapsource"
    capabilities = frozenset({Capability.LIVE_SEARCH})
    cost_per_call = 0
    data_class = DataClass.SANCTIONED
    searches = []

    def search(self, query):
        CheapSource.searches.append(query)
        return {"fare": 99, "carrier": "Cheapair", "leaves": f"{query.depart_date} 06:00"}

    def parse(self, query, raw):
        leaves = datetime.fromisoformat(raw["leaves"])
        flight = Segment(raw["carrier"], "ZZ 1", query.origin, query.destination, leaves, leaves)
        return SearchResult(query, (Offer(query, raw["fare"], (flight,), duration_min=400),))


REQUEST = scan.Request("JFK", "LHR", date(2030, 3, 12))


class RegistryTest(unittest.TestCase):
    def setUp(self):
        install(self)
        CheapSource.searches = []

    def add_cheap_source(self):
        providers.register(CheapSource)
        self.addCleanup(providers.unregister, CheapSource.name)

    def test_serpapi_is_the_only_source_until_another_is_registered(self):
        self.assertEqual(providers.names(), ["serpapi"])
        self.assertIsInstance(providers.load("serpapi"), SerpApiProvider)

    def test_serpapi_is_labelled(self):
        self.assertEqual(SerpApiProvider.data_class, DataClass.VENDOR)
        self.assertEqual(SerpApiProvider.cost_per_call, 1)
        self.assertIn(Capability.LIVE_SEARCH, SerpApiProvider.capabilities)

    def test_a_second_source_is_searched_alongside_the_first(self):
        self.add_cheap_source()
        plan = scan.plan(REQUEST)
        self.assertEqual([call.provider for call in plan.calls], ["serpapi", "cheapsource"])
        self.assertEqual(plan.live, 2)

        outcome = scan.run(plan)
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertEqual(len(CheapSource.searches), 1)
        (result,) = outcome.results.values()
        self.assertEqual([(offer.price, offer.airlines) for offer in result.offers], [(99, ("Cheapair",)), (200, ("Testair",))])
        # Only SerpApi is metered, so only its call comes off the count.
        self.assertEqual(outcome.searches_left, 249)
        # scan labels each offer with its source; the sources do not have to.
        self.assertEqual([offer.source for offer in result.offers], ["cheapsource", "serpapi"])

    def test_the_second_source_shows_up_in_the_command_line_with_no_other_change(self):
        self.add_cheap_source()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("2 searches needed: 0 saved, 2 live.", err.getvalue())
        self.assertIn("Cheapest: $99 one way, Tue 12 Mar", out.getvalue())
        self.assertIn("Cheapair", out.getvalue())

    def test_each_source_saves_and_reuses_its_own_replies(self):
        self.add_cheap_source()
        scan.run(scan.plan(REQUEST))
        again = scan.plan(REQUEST)
        self.assertEqual((again.saved, again.live), (2, 0))
        outcome = scan.run(again)
        self.assertEqual(len(FakeSerpApi.searches) + len(CheapSource.searches), 2)
        self.assertEqual(next(iter(outcome.results.values())).cheapest.price, 99)

    def test_a_free_source_needs_no_key(self):
        self.add_cheap_source()
        providers.unregister("serpapi")
        with mock.patch.dict(os.environ, {config.KEY_VAR: ""}):
            outcome = scan.run(scan.plan(REQUEST))
        self.assertEqual(next(iter(outcome.results.values())).cheapest.price, 99)
        self.assertIsNone(outcome.searches_left)

    def test_no_sources_is_said_plainly(self):
        providers.unregister("serpapi")
        with self.assertRaisesRegex(UserError, "no price source is available"):
            scan.plan(REQUEST)

    def test_a_source_without_its_labels_cannot_be_registered(self):
        class Unlabelled(Provider):
            name = "unlabelled"
            capabilities = frozenset({Capability.LIVE_SEARCH})
            cost_per_call = 0

        with self.assertRaisesRegex(TypeError, "must set data_class"):
            providers.register(Unlabelled)
        self.assertNotIn("unlabelled", providers.names())

    def test_unknown_source_is_said_plainly(self):
        with self.assertRaisesRegex(UserError, "no price source called 'nope'"):
            providers.load("nope")

    def test_the_account_says_what_is_left_the_monthly_total_and_the_reset_day(self):
        # The owner's free plan, as the real account endpoint reported it on 2026-10-09.
        FakeSerpApi.account_reply.update(total_searches_left=31, searches_per_month=250, plan_renewal_date="2026-11-08")
        self.assertEqual(providers.load("serpapi").allowance(), Allowance(31, 250, date(2026, 11, 8)))
        self.assertEqual(FakeSerpApi.searches, [])  # asking is free

    def test_another_plan_is_read_the_same_way_with_nothing_assumed(self):
        FakeSerpApi.account_reply.update(
            total_searches_left=4321, searches_per_month=5000, plan_renewal_date="2026-12-01T00:00:00Z"
        )
        self.assertEqual(providers.load("serpapi").allowance(), Allowance(4321, 5000, date(2026, 12, 1)))

    def test_an_account_that_names_no_reset_day_has_none(self):
        for said in (None, "", "soon"):
            FakeSerpApi.account_reply.update(plan_renewal_date=said)
            self.assertEqual(providers.load("serpapi").allowance(), Allowance(250, None, None), said)

    def test_a_source_that_is_not_metered_has_no_allowance(self):
        self.assertIsNone(CheapSource().allowance())

    def test_quota_command_asks_the_registry(self):
        FakeSerpApi.account_reply.update(plan_name="Free Plan", searches_per_month=250)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["quota"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.getvalue().splitlines(),
            ["Plan: Free Plan", "This month: 250 of 250 searches left", "This hour: 0 used of 50 allowed"],
        )


if __name__ == "__main__":
    unittest.main()
