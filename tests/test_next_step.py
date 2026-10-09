import contextlib
import io
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from flightsleuth import cli, config, links, scan
from flightsleuth.history import History
from flightsleuth.models import Query
from flightsleuth.render import UNCONFIRMED, summarise
from tests.fakes import FakeSerpApi, install, rich_reply

TODAY = date(2030, 1, 1)
WHY = (
    "On 8 Oct 2026 Momondo listed the same flights for less on 5 of 6 test routes. "
    "The two largest savings, $97 and $51, were on long trips."
)
ROUND_TRIP = Query("JFK,EWR", "LHR", date(2030, 3, 12), date(2030, 3, 19))
FIXED = ("search", "JFK", "LHR", "--depart", "2030-03-12", "--return", "2030-03-19")
MOMONDO = "https://www.momondo.com/flight-search/JFK-LHR/2030-03-12/2030-03-19?sort=price_a"
EMPTY = {"search_metadata": {"status": "Success"}, "error": "Google Flights hasn't returned any results."}


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


def summary(**changes):
    """One fixed-date round trip, searched and summarised as the front ends do it."""
    options = {"return_date": date(2030, 3, 19), **changes}
    outcome = scan.run(scan.plan(scan.Request("JFK", "LHR", date(2030, 3, 12), **options), today=TODAY))
    return summarise(outcome.trips, outcome.results, split=outcome.split, checks=outcome.checks, notes=outcome.notes)


def no_returns(self, query):
    """Every return lookup comes back empty, so the closer look confirms nothing."""
    FakeSerpApi.searches.append(query)
    return EMPTY if query.departure_token else rich_reply(query)


class NextStepTest(unittest.TestCase):
    """After a round trip, checking the fare on Momondo comes ahead of every other link."""

    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def test_a_confirmed_fare_is_followed_by_the_momondo_link_and_what_was_measured(self):
        out = self.run_cli(*FIXED, "--check")
        self.assertIn("Cheapest: $520 round trip, Tue 12 Mar - Tue 19 Mar\n", out)
        self.assertIn(f"\nCheck this fare on Momondo: {MOMONDO}\n  {WHY}\nCheck and book: ", out)
        self.assertNotIn("free way to confirm", out)

    def test_it_comes_before_the_google_link_and_the_other_three_sites(self):
        out = self.run_cli(*FIXED, "--check")
        order = [out.index(text) for text in ("Check this fare on Momondo:", "Check and book:", "Same dates on Skyscanner:")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(out.count("Same dates on "), 3)
        self.assertNotIn("Same dates on Momondo", out)
        self.assertEqual(out.count(MOMONDO), 1)

    def test_a_fare_nobody_looked_up_is_told_that_checking_it_there_is_a_free_way_to_confirm_it(self):
        out = self.run_cli(*FIXED)
        self.assertIn("Cheapest listed: $520 round trip, Tue 12 Mar - Tue 19 Mar (not yet confirmed)\n", out)
        self.assertIn(
            f"Check this fare on Momondo: {MOMONDO}\n"
            f"  Checking it on Momondo is a free way to confirm it: it uses none of your searches.\n"
            f"  {WHY}\n",
            out,
        )
        self.assertNotIn("Same dates on Momondo", out)

    def test_so_is_a_fare_the_closer_look_could_not_confirm(self):
        FakeSerpApi.phantoms = 1
        with mock.patch.object(FakeSerpApi, "search", no_returns):
            out = self.run_cli(*FIXED, "--check")
        self.assertIn("round trip, Tue 12 Mar - Tue 19 Mar (not confirmed)\n", out)
        self.assertIn("  Checking it on Momondo is a free way to confirm it", out)

    def test_the_headline_does_not_change_because_of_it(self):
        # Only fetched return flights may turn "Cheapest listed" into "Cheapest".
        self.assertEqual(summary().headline, "Cheapest listed: $520 round trip, Tue 12 Mar - Tue 19 Mar (not yet confirmed)")
        self.assertEqual(summary().caveat, UNCONFIRMED)

    def test_a_one_way_stays_as_it_was(self):
        out = self.run_cli("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        self.assertNotIn("Check this fare", out)
        self.assertIn("Same dates on Momondo: https://www.momondo.com/flight-search/JFK-LHR/2030-03-12?sort=price_a", out)
        self.assertEqual(out.count("Same dates on "), 4)

    def test_so_do_two_one_way_tickets_that_beat_the_round_trip(self):
        FakeSerpApi.rich = False  # the plain fake prices a one-way at 200 and this round trip at 520
        out = self.run_cli(*FIXED, "--split")
        self.assertIn("Cheapest: $400 as two one-way tickets", out)
        self.assertNotIn("Check this fare", out)
        self.assertEqual(out.count("Same dates on "), 4)

    def test_no_fares_means_no_next_step(self):
        with mock.patch.object(FakeSerpApi, "search", lambda self, query: EMPTY):
            out = self.run_cli(*FIXED)
        self.assertNotIn("Momondo", out)

    def test_what_the_link_cannot_carry_is_named_so_like_is_compared_with_like(self):
        out = self.run_cli(*FIXED, "--adults", "2", "--cabin", "business", "--stops", "1", "--bags", "1")
        self.assertIn(
            "  The link carries only the airports and dates. Set the page to 2 adults, business class, "
            "1 stop or fewer, 1 carry-on bag in the price before comparing prices.\n",
            out,
        )
        self.assertNotIn("The link carries only", self.run_cli(*FIXED, "--fresh"))

    def test_the_step_knows_which_fare_a_price_found_there_is_set_against(self):
        listed = summary().next_step
        self.assertEqual((listed.site, listed.label, listed.url), ("Momondo", "Check this fare on Momondo", MOMONDO))
        self.assertEqual((listed.fare.price, listed.fare.return_segments, listed.confirmed), (520, None, False))
        self.assertEqual(listed.trip, Query("JFK", "LHR", date(2030, 3, 12), date(2030, 3, 19)))

        held = summary(check=True).next_step
        self.assertEqual((held.fare.price, held.confirmed), (520, True))
        self.assertEqual([segment.flight_number for segment in held.fare.return_segments], ["TA 2"])

    def test_it_is_set_against_the_fare_the_headline_names_when_a_cheaper_one_is_only_listed(self):
        FakeSerpApi.return_surcharge = 45
        found = summary(check=True)
        self.assertEqual(found.headline, "Cheapest confirmed: $565 round trip, Tue 12 Mar - Tue 19 Mar")
        self.assertEqual((found.next_step.fare.price, found.next_step.confirmed), (565, True))

    def test_a_site_marked_first_keeps_its_warning_on_the_link(self):
        risky = tuple({**site, "why_first": "measured"} if site["name"] == "Skiplagged" else site for site in links.sites()[1:])
        with mock.patch.object(links, "sites", lambda: risky):
            out = self.run_cli(*FIXED)
        self.assertIn("Check this fare on Skiplagged (lists hidden-city fares): https://skiplagged.com/flights/JFK/LHR/", out)
        self.assertNotIn("Same dates on Skiplagged", out)


if __name__ == "__main__":
    unittest.main()


TYPED_AT = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def site_prices():
    history = History(config.history_path())
    try:
        return history.site_prices()
    finally:
        history.close()


class SitePriceTableTest(unittest.TestCase):
    """history.db keeps what was read on another site, and what it was read against."""

    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def keep(self, step, price, **answers):
        answers = {"seller": "", "same_flights": None, "held": None, **answers}
        history = History(config.history_path())
        try:
            history.add_site_price(
                step.trip, step.site, price, ours=step.fare, confirmed=step.confirmed, recorded_at=TYPED_AT, **answers
            )
        finally:
            history.close()

    def test_a_typed_price_is_kept_beside_the_fare_it_was_checked_against(self):
        before = datetime.now(timezone.utc) - timedelta(seconds=2)
        self.keep(summary(check=True, adults=2, bags=1).next_step, 499.5, seller="Mytrip", same_flights=True, held=False)
        (row,) = site_prices()
        fetched = datetime.fromisoformat(row.pop("our_fetched_at"))
        self.assertTrue(before <= fetched <= datetime.now(timezone.utc) + timedelta(seconds=2))
        self.assertEqual(
            row,
            {
                "id": 1,
                "origin": "JFK",
                "destination": "LHR",
                "depart_date": "2030-03-12",
                "return_date": "2030-03-19",
                "adults": 2,
                "cabin": "economy",
                "stops": "any",
                "currency": "USD",
                "market": "",
                "bags": 1,
                "site": "Momondo",
                "price": 499.5,
                "seller": "Mytrip",
                "same_flights": 1,
                "held": 0,
                "our_price": 555,
                "our_source": "serpapi",
                "our_confirmed": 1,
                "itinerary_key": "TA1@2030-03-12T18:30|TA2@2030-03-19T09:00",
                "recorded_at": "2030-01-02T12:00:00+00:00",
            },
        )

    def test_a_price_set_against_a_fare_nobody_confirmed_says_so(self):
        self.keep(summary().next_step, 480)
        (row,) = site_prices()
        self.assertEqual((row["our_price"], row["our_confirmed"], row["itinerary_key"]), (520, 0, "TA1@2030-03-12T18:30"))
        self.assertEqual((row["seller"], row["same_flights"], row["held"]), ("", None, None))

    def test_it_adds_nothing_to_the_prices_flightsleuth_fetched_itself(self):
        step = summary().next_step
        history = History(config.history_path())
        fetched = history.price_points()
        history.close()
        self.keep(step, 480)
        history = History(config.history_path())
        self.addCleanup(history.close)
        self.assertEqual(history.price_points(), fetched)
        self.assertEqual(len(fetched), 1)

    def test_its_rows_cannot_be_changed_or_deleted(self):
        self.keep(summary().next_step, 480)
        db = sqlite3.connect(config.history_path())
        self.addCleanup(db.close)
        for statement in ("UPDATE site_prices SET price = 1", "DELETE FROM site_prices"):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only", msg=statement):
                db.execute(statement)
        db.rollback()
        self.assertEqual(site_prices()[0]["price"], 480)

    def test_a_history_file_from_before_this_table_gains_it_and_keeps_its_rows(self):
        step = summary().next_step
        db = sqlite3.connect(config.history_path())
        db.executescript("DROP TRIGGER site_prices_no_update; DROP TRIGGER site_prices_no_delete; DROP TABLE site_prices;")
        db.close()
        self.keep(step, 480)
        self.assertEqual(len(site_prices()), 1)
        history = History(config.history_path())
        self.addCleanup(history.close)
        self.assertEqual(len(history.price_points()), 1)
