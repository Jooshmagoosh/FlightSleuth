import ast
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from flightsleuth import config, links, providers, scan, typed
from flightsleuth.history import History
from flightsleuth.models import Query
from flightsleuth.render import UNCONFIRMED, summarise
from tests.fakes import NO_FLIGHTS, STAMP, FakeSerpApi, history_rows, install, rich_reply, run_cli

TODAY = date(2030, 1, 1)
WHY = (
    "On 8 Oct 2026 Momondo listed the same flights for less on 5 of 6 test routes. "
    "The two largest savings, $97 and $51, were on long trips."
)
ROUND_TRIP = Query("JFK,EWR", "LHR", date(2030, 3, 12), date(2030, 3, 19))
FIXED = ("search", "JFK", "LHR", "--depart", "2030-03-12", "--return", "2030-03-19")
MOMONDO = "https://www.momondo.com/flight-search/JFK-LHR/2030-03-12/2030-03-19?sort=price_a"


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
    return NO_FLIGHTS if query.departure_token else rich_reply(query)


class NextStepTest(unittest.TestCase):
    """After a round trip, checking the fare on Momondo comes ahead of every other link."""

    def setUp(self):
        install(self)
        FakeSerpApi.rich = True

    def ok(self, *argv):
        """Run the command line, insist that it succeeded, and return what it printed."""
        code, out, err = run_cli(*argv)
        self.assertEqual(code, 0, err)
        return out

    def test_a_confirmed_fare_is_followed_by_the_momondo_link_and_what_was_measured(self):
        out = self.ok(*FIXED, "--check")
        self.assertIn("Cheapest: $520 round trip, Tue 12 Mar - Tue 19 Mar\n", out)
        self.assertIn(f"\nCheck this fare on Momondo: {MOMONDO}\n  {WHY}\nCheck and book: ", out)
        self.assertNotIn("free way to confirm", out)

    def test_it_comes_before_the_google_link_and_the_other_three_sites(self):
        out = self.ok(*FIXED, "--check")
        order = [out.index(text) for text in ("Check this fare on Momondo:", "Check and book:", "Same dates on Skyscanner:")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(out.count("Same dates on "), 3)
        self.assertNotIn("Same dates on Momondo", out)
        self.assertEqual(out.count(MOMONDO), 1)

    def test_a_fare_nobody_looked_up_is_told_that_checking_it_there_is_a_free_way_to_confirm_it(self):
        out = self.ok(*FIXED)
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
            out = self.ok(*FIXED, "--check")
        self.assertIn("round trip, Tue 12 Mar - Tue 19 Mar (not confirmed)\n", out)
        self.assertIn("  Checking it on Momondo is a free way to confirm it", out)

    def test_the_headline_does_not_change_because_of_it(self):
        # Only fetched return flights may turn "Cheapest listed" into "Cheapest".
        self.assertEqual(summary().headline, "Cheapest listed: $520 round trip, Tue 12 Mar - Tue 19 Mar (not yet confirmed)")
        self.assertEqual(summary().caveat, UNCONFIRMED)

    def test_a_one_way_stays_as_it_was(self):
        out = self.ok("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        self.assertNotIn("Check this fare", out)
        self.assertIn("Same dates on Momondo: https://www.momondo.com/flight-search/JFK-LHR/2030-03-12?sort=price_a", out)
        self.assertEqual(out.count("Same dates on "), 4)

    def test_so_do_two_one_way_tickets_that_beat_the_round_trip(self):
        FakeSerpApi.rich = False  # the plain fake prices a one-way at 200 and this round trip at 520
        out = self.ok(*FIXED, "--split")
        self.assertIn("Cheapest: $400 as two one-way tickets", out)
        self.assertNotIn("Check this fare", out)
        self.assertEqual(out.count("Same dates on "), 4)

    def test_no_fares_means_no_next_step(self):
        with mock.patch.object(FakeSerpApi, "search", lambda self, query: NO_FLIGHTS):
            out = self.ok(*FIXED)
        self.assertNotIn("Momondo", out)

    def test_what_the_link_cannot_carry_is_named_so_like_is_compared_with_like(self):
        out = self.ok(*FIXED, "--adults", "2", "--cabin", "business", "--stops", "1", "--bags", "1")
        self.assertIn(
            "  The link carries only the airports and dates. Set the page to 2 adults, business class, "
            "1 stop or fewer, 1 carry-on bag in the price before comparing prices.\n",
            out,
        )
        self.assertNotIn("The link carries only", self.ok(*FIXED, "--fresh"))

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
            out = self.ok(*FIXED)
        self.assertIn("Check this fare on Skiplagged (lists hidden-city fares): https://skiplagged.com/flights/JFK/LHR/", out)
        self.assertNotIn("Same dates on Skiplagged", out)


if __name__ == "__main__":
    unittest.main()


TYPED_AT = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)



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
        (row,) = history_rows("site_prices")
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
        (row,) = history_rows("site_prices")
        self.assertEqual((row["our_price"], row["our_confirmed"], row["itinerary_key"]), (520, 0, "TA1@2030-03-12T18:30"))
        self.assertEqual((row["seller"], row["same_flights"], row["held"]), ("", None, None))

    def test_it_adds_nothing_to_the_prices_flightsleuth_fetched_itself(self):
        step = summary().next_step
        history = History(config.history_path())
        fetched = history.rows("price_points")
        history.close()
        self.keep(step, 480)
        history = History(config.history_path())
        self.addCleanup(history.close)
        self.assertEqual(history.rows("price_points"), fetched)
        self.assertEqual(len(fetched), 1)

    def test_its_rows_cannot_be_changed_or_deleted(self):
        self.keep(summary().next_step, 480)
        db = sqlite3.connect(config.history_path())
        self.addCleanup(db.close)
        for statement in ("UPDATE site_prices SET price = 1", "DELETE FROM site_prices"):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only", msg=statement):
                db.execute(statement)
        db.rollback()
        self.assertEqual(history_rows("site_prices")[0]["price"], 480)

    def test_a_history_file_from_before_this_table_gains_it_and_keeps_its_rows(self):
        step = summary().next_step
        db = sqlite3.connect(config.history_path())
        db.executescript("DROP TRIGGER site_prices_no_update; DROP TRIGGER site_prices_no_delete; DROP TABLE site_prices;")
        db.close()
        self.keep(step, 480)
        self.assertEqual(len(history_rows("site_prices")), 1)
        history = History(config.history_path())
        self.addCleanup(history.close)
        self.assertEqual(len(history.rows("price_points")), 1)


def at_keyboard(test, *answers):
    """Sit somebody at the keyboard who types these answers, one to a question. Returns the questions asked."""
    tty = mock.Mock()
    tty.isatty.return_value = True
    test.enterContext(mock.patch("sys.stdin", tty))
    return test.enterContext(mock.patch("builtins.input", side_effect=list(answers)))


class TypedPriceTest(unittest.TestCase):
    def test_a_price_may_be_typed_with_a_dollar_sign_and_commas(self):
        for text, amount in (("412", 412), (" $1,234.50 ", 1234.5), ("", None), ("   ", None)):
            self.assertEqual(typed.price(text), amount, text)

    def test_anything_else_is_not_a_price(self):
        for text in ("abc", "0", "-5", "nan", "inf", "$", "12 dollars"):
            with self.assertRaises(ValueError, msg=text):
                typed.price(text)

    def test_only_y_and_n_are_answers(self):
        answers = [typed.yes_no(text) for text in ("y", " Yes ", "N", "no", "", "maybe")]
        self.assertEqual(answers, [True, True, False, False, None, None])


class RecordAtTheKeyboardTest(unittest.TestCase):
    """At the end of a round-trip search the command line asks what Momondo showed, and keeps the answer."""

    setUp = NextStepTest.setUp
    ok = NextStepTest.ok

    def test_the_price_the_seller_and_both_answers_are_kept(self):
        asked = at_keyboard(self, "abc", "$1,234.50", " Mytrip ", "y", "n")
        out = self.ok(*FIXED, "--check")
        (row,) = history_rows("site_prices")
        self.assertEqual(
            (row["site"], row["price"], row["seller"], row["same_flights"], row["held"]), ("Momondo", 1234.5, "Mytrip", 1, 0)
        )
        self.assertEqual((row["our_price"], row["our_confirmed"]), (520, 1))
        self.assertEqual(
            [call.args[0] for call in asked.call_args_list],
            [
                "USD price you found on Momondo for this fare (Enter to skip): ",
                "USD price you found on Momondo for this fare (Enter to skip): ",
                "Who sells it at that price (Enter to skip): ",
                "Is that the same flights? y, n, or Enter if unsure: ",
                "Did the seller's payment page show that price? y, n, or Enter if you did not get that far: ",
            ],
        )
        self.assertIn("'abc' is not a price. Type a number such as 412, or press Enter to skip.\n", out)
        self.assertRegex(
            out,
            rf"Saved\. Momondo: \$1,234\.50 from Mytrip, typed {STAMP}\. "
            rf"That is \$714\.50 more than the \$520 confirmed fare fetched {STAMP}\.\n$",
        )

    def test_a_price_found_for_a_fare_nobody_confirmed_is_kept_as_that(self):
        at_keyboard(self, "480", "", "", "")
        out = self.ok(*FIXED)
        (row,) = history_rows("site_prices")
        self.assertEqual(
            (row["price"], row["seller"], row["same_flights"], row["held"], row["our_confirmed"]), (480, "", None, None, 0)
        )
        self.assertRegex(
            out, rf"Saved\. Momondo: \$480, typed {STAMP}\. That is \$40 less than the \$520 listed fare fetched {STAMP}\.\n$"
        )

    def test_a_matching_price_is_said_to_be_the_same(self):
        at_keyboard(self, "520", "", "", "")
        self.assertRegex(self.ok(*FIXED), rf"That is the same as the \$520 listed fare fetched {STAMP}\.\n$")

    def test_a_fare_replayed_from_saved_results_is_said_to_be_saved(self):
        self.ok(*FIXED)
        at_keyboard(self, "480", "", "", "")
        self.assertRegex(self.ok(*FIXED), rf"the \$520 listed fare fetched {STAMP} \(saved\)\.\n$")

    def test_pressing_enter_skips_it_after_one_question(self):
        asked = at_keyboard(self, "")
        out = self.ok(*FIXED)
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(history_rows("site_prices"), [])
        self.assertNotIn("Saved", out)

    def test_a_keyboard_that_goes_away_is_an_empty_answer(self):
        asked = at_keyboard(self)
        asked.side_effect = EOFError
        self.ok(*FIXED)
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(history_rows("site_prices"), [])

    def test_nobody_is_asked_when_no_one_is_at_the_keyboard(self):
        with mock.patch("builtins.input") as asked:
            self.ok(*FIXED)
        asked.assert_not_called()
        self.assertEqual(history_rows("site_prices"), [])

    def test_no_record_turns_the_question_off(self):
        asked = at_keyboard(self)
        self.ok(*FIXED, "--no-record")
        asked.assert_not_called()

    def test_a_one_way_a_dry_run_and_an_empty_result_ask_nothing(self):
        asked = at_keyboard(self)
        self.ok("search", "JFK", "LHR", "--depart", "2030-03-12", "--one-way")
        self.ok(*FIXED, "--dry-run")
        with mock.patch.object(FakeSerpApi, "search", lambda self, query: NO_FLIGHTS):
            self.ok(*FIXED)
        asked.assert_not_called()

    def test_recording_sends_no_search_and_logs_no_call(self):
        at_keyboard(self, "480", "Mytrip", "y", "y")
        self.ok(*FIXED)
        self.assertEqual(len(FakeSerpApi.searches), 1)
        history = History(config.history_path())
        self.addCleanup(history.close)
        self.assertEqual((len(history.rows("provider_calls")), len(history.rows("price_points")), len(history.rows("site_prices"))), (1, 1, 1))


class GateTest(unittest.TestCase):
    """Phase 3 was to add no price source and no call to any network.

    Phase 4 added one module that reaches a network, with the owner's say-so on 2026-10-09:
    `notify.py`, which sends alerts to ntfy. It is not a price source.
    """

    NETWORK = {"urllib", "http", "socket", "ssl", "ftplib", "smtplib", "poplib", "imaplib", "xmlrpc", "asyncio"}

    def test_serpapi_is_still_the_only_source(self):
        install(self)
        self.assertEqual(providers.names(), ["serpapi"])

    def test_only_the_serpapi_client_and_the_alert_sender_import_anything_that_reaches_a_network(self):
        package = Path(links.__file__).parent
        reaching = set()
        for path in package.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                else:
                    continue
                if any(name.split(".")[0] in self.NETWORK for name in names):
                    reaching.add(path.relative_to(package).as_posix())
        self.assertEqual(reaching, {"serpapi.py", "notify.py"})
