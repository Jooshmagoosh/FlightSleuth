import os
import unittest
from datetime import date
from unittest import mock

from flightsleuth import config, providers, scan
from flightsleuth.errors import UserError
from flightsleuth.models import SearchResult
from flightsleuth.providers import Capability, DataClass, Provider
from tests.fakes import FakeSerpApi, install

REQUEST = scan.Request("JFK", "LHR", date(2030, 3, 12))


class HomeScraper(Provider):
    """Stands in for a source that may only run on the user's own machine."""

    name = "homescraper"
    capabilities = frozenset({Capability.LIVE_SEARCH})
    cost_per_call = 0
    data_class = DataClass.PERSONAL_ONLY
    built = 0
    searches = []

    def __init__(self):
        HomeScraper.built += 1

    def search(self, query):
        HomeScraper.searches.append(query)
        return {}

    def parse(self, query, raw):
        return SearchResult(query, ())


def hosted():
    return mock.patch.dict(os.environ, {config.MODE_VAR: "hosted"})


class ModeTest(unittest.TestCase):
    def setUp(self):
        install(self)
        HomeScraper.built = 0
        HomeScraper.searches = []
        providers.register(HomeScraper)
        self.addCleanup(providers.unregister, HomeScraper.name)

    def test_personal_is_the_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(config.MODE_VAR, None)
            self.assertEqual(config.mode(), "personal")

    def test_mode_is_read_from_the_environment(self):
        with mock.patch.dict(os.environ, {config.MODE_VAR: " Hosted "}):
            self.assertEqual(config.mode(), "hosted")

    def test_a_misspelt_mode_is_an_error_rather_than_a_silent_default(self):
        with mock.patch.dict(os.environ, {config.MODE_VAR: "public"}):
            with self.assertRaisesRegex(UserError, "must be personal or hosted"):
                config.mode()

    def test_personal_mode_uses_a_personal_only_source(self):
        scan.run(scan.plan(REQUEST))
        self.assertEqual(len(HomeScraper.searches), 1)

    def test_hosted_mode_refuses_to_load_a_personal_only_source(self):
        with hosted():
            with self.assertRaisesRegex(UserError, "personal use only and cannot be loaded in hosted mode"):
                providers.load("homescraper")
            self.assertEqual([source.name for source in providers.offering(Capability.LIVE_SEARCH)], ["serpapi"])
        self.assertEqual(HomeScraper.built, 0)

    def test_hosted_mode_still_loads_vendor_and_sanctioned_sources(self):
        with hosted():
            self.assertEqual(providers.load("serpapi").data_class, DataClass.VENDOR)

    def test_a_hosted_scan_never_touches_a_personal_only_source(self):
        with hosted():
            plan = scan.plan(REQUEST)
            self.assertEqual([call.provider for call in plan.calls], ["serpapi"])
            scan.run(plan)
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertEqual(HomeScraper.searches, [])
        self.assertEqual(HomeScraper.built, 0)

    def test_a_plan_made_in_personal_mode_cannot_be_run_in_hosted_mode(self):
        plan = scan.plan(REQUEST)
        self.assertIn("homescraper", [call.provider for call in plan.calls])
        HomeScraper.built = 0
        with hosted():
            with self.assertRaisesRegex(UserError, "cannot be loaded in hosted mode"):
                scan.run(plan)
        self.assertEqual(HomeScraper.searches, [])
        self.assertEqual(FakeSerpApi.searches, [])

    def test_hosted_mode_with_only_personal_sources_has_nothing_to_search(self):
        providers.unregister("serpapi")
        with hosted():
            with self.assertRaisesRegex(UserError, "no price source is available"):
                scan.plan(REQUEST)


if __name__ == "__main__":
    unittest.main()
