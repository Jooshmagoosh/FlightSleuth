import gc
import os
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

try:
    import tkinter as tk
except ImportError:
    # Some Pythons ship without tkinter. The rest of the suite must still run there.
    raise unittest.SkipTest("tkinter is not installed, so the window cannot be tested") from None

from flightsleuth import config, gui
from tests.fakes import FakeSerpApi, install


def rows(tree):
    return [list(tree.item(item, "values")) for item in tree.get_children()]


def headings(tree):
    return [tree.heading(column, "text") for column in tree["columns"]]


class GuiTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(install(self))
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display to open a window on: {e}")
        self.root.withdraw()
        self.errors = []
        self.enterContext(
            mock.patch.object(gui.messagebox, "showerror", lambda title, message, **kw: self.errors.append(message))
        )
        self.app = gui.App(self.root)
        self.app.origin.set("JFK")
        self.app.destination.set("LHR")
        self.app.depart.set(date(2030, 3, 12))
        self.app.back.set(date(2030, 3, 19))

    def tearDown(self):
        self.root.destroy()
        # Free the window here and now: Tk objects must not be garbage-collected on a worker thread.
        del self.app, self.root
        gc.collect()

    def search(self):
        self.app.search()
        deadline = time.monotonic() + 10
        while self.app.busy:
            self.assertLess(time.monotonic(), deadline, "the search never finished")
            self.root.update()
            time.sleep(0.01)

    def test_flexible_round_trip_fills_both_tables(self):
        self.app.origin.set("jfk, ewr")
        self.app.flex.set("1")
        self.search()

        self.assertEqual(self.errors, [])
        self.assertEqual(len(FakeSerpApi.searches), 3)
        self.assertEqual(
            self.app.headline.get(), "Cheapest listed: $455 round trip, Mon 11 Mar - Mon 18 Mar (not yet confirmed)"
        )
        self.assertEqual(self.app.detail.get(), "Google rates $455 as low for those dates (typical range $500 to $800).")
        self.assertEqual(headings(self.app.dates), ["Depart", "Return", "Round trip"])
        self.assertEqual(
            rows(self.app.dates),
            [
                ["Mon 11 Mar", "Mon 18 Mar", "$455"],
                ["Tue 12 Mar", "Tue 19 Mar", "$520"],
                ["Wed 13 Mar", "Wed 20 Mar", "$590"],
            ],
        )
        self.assertEqual(headings(self.app.flights)[:4], ["Price", "Route", "Outbound", "Return"])
        self.assertEqual(
            rows(self.app.flights)[0],
            ["$455", "JFK->LHR", "Mon 11 Mar 18:30 -> 23:10", "Mon 18 Mar", "nonstop", "7h 10m", "Testair"],
        )
        self.assertEqual(
            self.app.status.get(), "Done. Searches used: 3. Reused from earlier: 0. About 247 left this month."
        )

    def test_cheapest_date_is_highlighted(self):
        self.app.flex.set("1")
        self.search()
        tagged = [self.app.dates.item(item, "tags") for item in self.app.dates.get_children()]
        self.assertEqual(tagged, [("best",), "", ""])

    def test_booking_button_opens_the_google_flights_page(self):
        self.search()
        (button,) = self.app._links.winfo_children()
        self.assertEqual(button.cget("text"), "Check and book")
        with mock.patch.object(gui.webbrowser, "open") as opened:
            button.invoke()
        opened.assert_called_once_with("https://www.google.com/travel/flights?tfs=X")

    def test_a_link_that_is_not_a_web_page_gets_no_button(self):
        risky = {
            "search_metadata": {"status": "Success", "google_flights_url": "file:///C:/Windows/System32/calc.exe"},
            "other_flights": [
                {
                    "flights": [
                        {
                            "departure_airport": {"id": "JFK", "time": "2030-03-12 18:30"},
                            "arrival_airport": {"id": "LHR", "time": "2030-03-12 23:10"},
                            "airline": "Testair",
                        }
                    ],
                    "total_duration": 430,
                    "price": 300,
                }
            ],
        }
        with mock.patch.object(FakeSerpApi, "search", lambda self, query: risky):
            self.search()
        self.assertEqual(
            self.app.headline.get(), "Cheapest listed: $300 round trip, Tue 12 Mar - Tue 19 Mar (not yet confirmed)"
        )
        self.assertEqual(self.app._links.winfo_children(), [])

    def test_repeating_a_search_reuses_saved_results(self):
        self.search()
        self.search()
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertEqual(self.app.status.get(), "Done. Searches used: 0. Reused from earlier: 1.")

    def test_one_way_ignores_the_return_date_and_the_split_option(self):
        self.app.split.set(True)
        self.app.round_trip.set(False)
        self.app._trip_kind_changed()
        self.search()

        self.assertEqual(self.errors, [])
        self.assertEqual(len(FakeSerpApi.searches), 1)
        self.assertIsNone(FakeSerpApi.searches[0].return_date)
        self.assertEqual(self.app.headline.get(), "Cheapest: $200 one way, Tue 12 Mar")
        self.assertEqual(headings(self.app.flights)[:3], ["Price", "Route", "Departs"])
        self.assertEqual(str(self.app._split_box.cget("state")), "disabled")

    def test_two_one_ways_winning_shows_both_flights_and_both_links(self):
        self.app.split.set(True)
        self.search()

        self.assertEqual(self.app.headline.get(), "Cheapest: $400 as two one-way tickets, Tue 12 Mar - Tue 19 Mar")
        self.assertIn("out   $200   JFK->LHR   Tue 12 Mar 18:30 -> 23:10", self.app.detail.get())
        self.assertIn("back   $200   LHR->JFK   Tue 19 Mar 18:30 -> 23:10", self.app.detail.get())
        self.assertNotIn("Google rates", self.app.detail.get())
        self.assertEqual(headings(self.app.dates), ["Depart", "Return", "Round trip", "Two one-ways"])
        self.assertEqual(rows(self.app.dates), [["Tue 12 Mar", "Tue 19 Mar", "$520", "$400"]])
        self.assertEqual(
            [button.cget("text") for button in self.app._links.winfo_children()],
            ["Check and book the flight out", "Check and book the flight back"],
        )

    def test_bad_airport_is_explained_without_searching(self):
        self.app.origin.set("New York")
        self.search()
        self.assertEqual(self.errors, ["'New York' is not an airport list like JFK or JFK,EWR"])
        self.assertEqual(FakeSerpApi.searches, [])

    def test_blank_airport_is_explained(self):
        self.app.destination.set("  ")
        self.search()
        self.assertEqual(self.errors, ["Fill in both the From and To airports"])

    def test_impossible_date_is_explained(self):
        self.app.depart.set(date(2030, 3, 31))
        self.app.depart._month.current(1)
        self.search()
        self.assertEqual(self.errors, ["February 31, 2030 is not a real date"])

    def test_return_before_departure_is_explained(self):
        self.app.back.set(date(2030, 3, 1))
        self.search()
        self.assertEqual(self.errors, ["The return date is before the departure date"])
        self.assertFalse(self.app.busy)
        self.assertEqual(FakeSerpApi.searches, [])

    def test_too_big_a_search_is_refused(self):
        self.app.flex.set("7")
        self.app.split.set(True)
        self.search()
        self.assertEqual(len(self.errors), 1)
        self.assertIn("would use 45 searches at once, and the limit is 25", self.errors[0])
        self.assertEqual(FakeSerpApi.searches, [])

    def test_running_out_of_searches_is_reported_and_the_window_recovers(self):
        FakeSerpApi.fail_after = 1
        self.app.flex.set("1")
        self.search()
        self.assertIn("Stopped after 1 of 3 live searches", self.errors[0])
        self.assertEqual(str(self.app._search_button.cget("state")), "normal")

        FakeSerpApi.fail_after = None
        self.search()
        self.assertEqual(self.app.status.get(), "Done. Searches used: 2. Reused from earlier: 1. About 248 left this month.")

    def test_first_search_asks_for_the_key_and_saves_it(self):
        self.enterContext(mock.patch.dict(os.environ, {config.KEY_VAR: ""}))
        with mock.patch.object(gui.simpledialog, "askstring", return_value=" pasted-key ") as asked:
            self.search()
        asked.assert_called_once()
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8"), "SERPAPI_API_KEY=pasted-key\n")
        self.assertEqual(
            self.app.headline.get(), "Cheapest listed: $520 round trip, Tue 12 Mar - Tue 19 Mar (not yet confirmed)"
        )

    def test_backing_out_of_the_key_prompt_cancels_the_search(self):
        self.enterContext(mock.patch.dict(os.environ, {config.KEY_VAR: ""}))
        with mock.patch.object(gui.simpledialog, "askstring", return_value=None):
            self.search()
        self.assertFalse((self.home / ".env").exists())
        self.assertEqual(FakeSerpApi.searches, [])
        self.assertFalse(self.app.busy)


STAMP = r"^\d\d \w{3} \d\d:\d\d$"  # a fetch time, such as "08 Oct 14:02"


def stamps(tree):
    return [tree.item(item, "text") for item in tree.get_children()]


class CloserLookWindowTest(unittest.TestCase):
    """What Phase 2 added to the window. The tests above pin everything that was there before."""

    setUp = GuiTest.setUp
    tearDown = GuiTest.tearDown
    search = GuiTest.search

    def test_every_price_row_shows_when_it_was_fetched(self):
        self.app.flex.set("1")
        self.search()
        self.assertEqual(self.app.dates.heading("#0", "text"), "Fetched")
        self.assertEqual(len(stamps(self.app.dates)), 3)
        for stamp in stamps(self.app.dates) + stamps(self.app.flights):
            self.assertRegex(stamp, STAMP)

    def test_a_saved_price_says_it_is_saved(self):
        self.search()
        self.search()
        self.assertTrue(all(stamp.endswith(" (saved)") for stamp in stamps(self.app.dates) + stamps(self.app.flights)))

    def test_a_round_trip_nobody_looked_up_is_called_a_listed_price(self):
        self.search()
        self.assertEqual(
            self.app.notes.get(),
            "This is the listed price: its return flights have not been looked up. "
            'Tick "Confirm the best fare" and search again to look them up.',
        )
        self.assertEqual(rows(self.app.ways), [])
        self.assertTrue(self.app.closer.get().startswith("Nothing has been looked at closely yet."))

    def test_a_one_way_carries_no_such_note(self):
        self.app.round_trip.set(False)
        self.app._trip_kind_changed()
        self.search()
        self.assertEqual(self.app.notes.get(), "")

    def test_confirming_the_best_fare_shows_its_return_flights_and_the_other_ways_to_fly(self):
        FakeSerpApi.rich = True
        self.app.confirm.set(True)
        self.search()

        self.assertEqual(self.errors, [])
        self.assertEqual(len(FakeSerpApi.searches), 6)
        self.assertEqual(self.app.headline.get(), "Cheapest: $520 round trip, Tue 12 Mar - Tue 19 Mar")
        self.assertEqual(
            self.app.notes.get(),
            'Tue 12 Mar - Tue 19 Mar: $520 confirmed. The "Best fare, looked at closely" tab has the details.',
        )
        # The second tab holds the flights both ways and what was confirmed about them.
        self.assertEqual([self.app.tabs.tab(tab, "text") for tab in self.app.tabs.tabs()], ["Prices", "Best fare, looked at closely"])
        closer = self.app.closer.get().splitlines()
        self.assertEqual(closer[0], "out   JFK->LHR   Tue 12 Mar 18:30 -> 23:10   nonstop   7h 10m   Testair (TA 1)")
        self.assertEqual(closer[1], "back   LHR->JFK   Tue 19 Mar 09:00 -> 12:00   nonstop   8h 00m   Testair (TA 2)")
        self.assertRegex(closer[2], r"^Return flights fetched \d\d \w{3} \d\d:\d\d\.$")
        self.assertEqual(headings(self.app.ways), ["Price", "How", "Flights", "Catch"])
        self.assertEqual(
            rows(self.app.ways),
            [
                ["$400", "Two one-way tickets", "different itinerary", "two separate tickets ($200 + $200)"],
                ["$520", "Buy from Testair", "same flights", "one ticket"],
                ["$520", "As sold in GB", "same outbound flights", "listed; return flights not looked up"],
                ["$545", "Buy from Cheapo Travel", "same flights", "one ticket; an agency, not the airline"],
            ],
        )
        for stamp in stamps(self.app.ways):
            self.assertRegex(stamp, STAMP)
        self.assertEqual(
            self.app.status.get(), "Done. Searches used: 6. Reused from earlier: 0. About 244 left this month."
        )

    def test_a_fare_with_no_return_flights_is_named_under_the_headline(self):
        FakeSerpApi.rich = True
        FakeSerpApi.phantoms = 3
        self.app.confirm.set(True)
        self.search()
        self.assertEqual(self.app.headline.get(), "Cheapest confirmed: $520 round trip, Tue 12 Mar - Tue 19 Mar")
        self.assertIn("The $480 fare listed for these dates (Phantom Air (PH 0)) had no return flights", self.app.closer.get())
        self.assertIn("2 cheaper fares are listed for these dates, from $481", self.app.closer.get())

    def test_a_search_without_the_closer_look_clears_what_the_last_one_found(self):
        FakeSerpApi.rich = True
        self.app.confirm.set(True)
        self.search()
        self.app.confirm.set(False)
        self.app.depart.set(date(2030, 3, 13))
        self.app.back.set(date(2030, 3, 20))
        self.search()
        self.assertEqual(rows(self.app.ways), [])
        self.assertTrue(self.app.closer.get().startswith("Nothing has been looked at closely yet."))
        self.assertIn("This is the listed price", self.app.notes.get())

    def test_the_best_dates_can_be_opened_on_each_other_site(self):
        self.search()
        label, *buttons = self.app._more_links.winfo_children()
        self.assertEqual(label.cget("text"), "Same dates on:")
        self.assertEqual(
            [button.cget("text") for button in buttons],
            ["Momondo", "Skyscanner", "Skiplagged (lists hidden-city fares)", "Kiwi.com (lists self-transfer fares)"],
        )
        with mock.patch.object(gui.webbrowser, "open") as opened:
            buttons[0].invoke()
        opened.assert_called_once_with("https://www.momondo.com/flight-search/JFK-LHR/2030-03-12/2030-03-19?sort=price_a")

    def test_a_small_range_of_dates_is_priced_in_full_and_its_best_fare_confirmed(self):
        FakeSerpApi.rich = True
        self.app.depart.set(date(2030, 3, 10))
        self.app.window.set(True)
        self.app._trip_kind_changed()
        self.app.latest.set(date(2030, 3, 14))
        self.app.nights_low.set("7")
        self.app.nights_high.set("7")
        self.search()

        self.assertEqual(self.errors, [])
        self.assertEqual(len(rows(self.app.dates)), 5)
        self.assertEqual(self.app.headline.get(), "Cheapest: $455 round trip, Mon 11 Mar - Mon 18 Mar")
        self.assertIn("Mon 11 Mar - Mon 18 Mar: $455 confirmed", self.app.notes.get())
        self.assertEqual(len(FakeSerpApi.searches), 10)  # five dates, then the closer look without its spare
        self.assertEqual(str(self.app._flex_box.cget("state")), "disabled")
        self.assertEqual(str(self.app._confirm_box.cget("state")), "disabled")

    def test_a_wide_range_is_sampled_inside_the_limit(self):
        FakeSerpApi.rich = True
        self.app.depart.set(date(2030, 3, 1))
        self.app.window.set(True)
        self.app._trip_kind_changed()
        self.app.latest.set(date(2030, 5, 30))
        self.search()

        self.assertEqual(self.errors, [])
        self.assertLessEqual(len(FakeSerpApi.searches), 25)
        self.assertGreater(len(rows(self.app.dates)), 12)
        self.assertRegex(self.app.status.get(), r"^Done\. Searches used: \d+\. Reused from earlier: 0\. About \d+ left this month\.$")

    def test_a_one_way_range_needs_no_nights(self):
        self.app.round_trip.set(False)
        self.app.window.set(True)
        self.app._trip_kind_changed()
        self.app.latest.set(date(2030, 3, 14))
        self.app.nights_low.set("nine")
        self.search()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.app.headline.get(), "Cheapest: $200 one way, Tue 12 Mar")
        self.assertEqual(str(self.app._nights_boxes[0].cget("state")), "disabled")

    def test_nights_that_run_backwards_are_explained(self):
        self.app.window.set(True)
        self.app.nights_low.set("9")
        self.app.nights_high.set("5")
        self.search()
        self.assertEqual(self.errors, ["The most nights is fewer than the fewest nights"])
        self.assertEqual(FakeSerpApi.searches, [])

    def test_a_range_that_ends_before_it_starts_is_explained(self):
        self.app.window.set(True)
        self.app.latest.set(date(2030, 3, 1))
        self.search()
        self.assertEqual(self.errors, ["The date window ends before it starts"])

    def test_the_range_boxes_start_greyed_out(self):
        self.assertEqual(str(self.app._nights_boxes[0].cget("state")), "disabled")
        self.assertEqual(str(self.app._flex_box.cget("state")), "normal")
        self.assertEqual(str(self.app._confirm_box.cget("state")), "normal")

    def test_carry_on_bags_go_into_every_search(self):
        self.app.bags.set("2")
        self.search()
        self.assertEqual(FakeSerpApi.searches[0].bags, 2)

    def test_confirming_counts_towards_the_limit_and_the_refusal_says_so(self):
        self.app.flex.set("7")
        self.app.split.set(True)
        self.app.confirm.set(True)
        self.search()
        self.assertIn("would use 49 searches at once, and the limit is 25", self.errors[0])
        self.assertIn('Unticking "Confirm the best fare" saves up to 4 too.', self.errors[0])
        self.assertEqual(FakeSerpApi.searches, [])


if __name__ == "__main__":
    unittest.main()
