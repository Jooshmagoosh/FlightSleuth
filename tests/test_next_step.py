import unittest
from datetime import date
from unittest import mock

from flightsleuth import links
from flightsleuth.models import Query
from tests.fakes import install

WHY = (
    "On 8 Oct 2026 Momondo listed the same flights for less on 5 of 6 test routes. "
    "The two largest savings, $97 and $51, were on long trips."
)
ROUND_TRIP = Query("JFK,EWR", "LHR", date(2030, 3, 12), date(2030, 3, 19))


class FirstSiteTest(unittest.TestCase):
    """Which site to check before the others is data, and so is the measurement that says why."""

    def setUp(self):
        install(self)

    def test_momondo_is_the_one_site_marked_and_it_says_what_was_measured_and_when(self):
        marked = [site for site in links.sites() if site.get("why_first")]
        self.assertEqual([site["name"] for site in marked], ["Momondo"])
        self.assertEqual(marked[0]["why_first"], WHY)

    def test_it_opens_the_same_search_as_its_link_among_the_other_sites(self):
        first = links.first_site(ROUND_TRIP)
        self.assertEqual((first.name, first.label, first.why), ("Momondo", "Momondo", WHY))
        self.assertEqual(first.url, "https://www.momondo.com/flight-search/JFK,EWR-LHR/2030-03-12/2030-03-19?sort=price_a")
        self.assertIn((first.label, first.url), links.other_sites(ROUND_TRIP))

    def test_a_first_site_with_a_warning_is_never_shown_without_it(self):
        risky = tuple({**site, "why_first": "measured"} if site["name"] == "Skiplagged" else site for site in links.sites()[1:])
        with mock.patch.object(links, "sites", lambda: risky):
            first = links.first_site(ROUND_TRIP)
        self.assertEqual((first.name, first.label), ("Skiplagged", "Skiplagged (lists hidden-city fares)"))

    def test_with_no_site_marked_there_is_none(self):
        unmarked = links.sites()[1:]
        with mock.patch.object(links, "sites", lambda: unmarked):
            self.assertIsNone(links.first_site(ROUND_TRIP))


if __name__ == "__main__":
    unittest.main()
