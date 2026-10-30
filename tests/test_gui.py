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

from flightsleuth import config, gui, watch
from tests.fakes import NO_FLIGHTS, FakeSerpApi, history_rows, install


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
        # No test may put anything on the real clipboard: what the window copies lands in this list.
        self.copied = []
        self.root.clipboard_clear = self.copied.clear
        self.root.clipboard_append = self.copied.append
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
        # Since 2026-10-09 a round trip's Momondo button leads the row above; NextStepWindowTest has it.
        self.assertEqual(
            [button.cget("text") for button in buttons],
            ["Skyscanner", "Skiplagged (lists hidden-city fares)", "Kiwi.com (lists self-transfer fares)"],
        )
        with mock.patch.object(gui.webbrowser, "open") as opened:
            buttons[0].invoke()
        opened.assert_called_once_with("https://www.skyscanner.com/transport/flights/jfk/lhr/300312/300319/")

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


MOMONDO = "https://www.momondo.com/flight-search/JFK-LHR/2030-03-12/2030-03-19?sort=price_a"
WHY = (
    "On 8 Oct 2026 Momondo listed the same flights for less on 5 of 6 test routes. "
    "The two largest savings, $97 and $51, were on long trips."
)
FREE = "Checking it on Momondo is a free way to confirm it: it uses none of your searches."
TIME = r"\d\d \w{3} \d\d:\d\d"


def texts(frame):
    return [widget.cget("text") for widget in frame.winfo_children()]



class NextStepWindowTest(unittest.TestCase):
    """What Phase 3 added to the window: checking the fare on Momondo comes first, and what was found is kept."""

    setUp = GuiTest.setUp
    tearDown = GuiTest.tearDown
    search = GuiTest.search

    def one_way(self):
        self.app.round_trip.set(False)
        self.app._trip_kind_changed()
        self.search()

    def test_a_round_trip_leads_with_the_momondo_button(self):
        self.search()
        self.assertEqual(
            texts(self.app._next_buttons), ["Check this fare on Momondo", "Copy the address", "Save the price you found there..."]
        )
        with mock.patch.object(gui.webbrowser, "open") as opened:
            self.app._next_buttons.winfo_children()[0].invoke()
        opened.assert_called_once_with(MOMONDO)

    def test_it_stands_ahead_of_check_and_book_and_above_the_other_sites(self):
        self.search()
        row = self.app._links.master
        self.assertEqual(row.pack_slaves(), [self.app._next_buttons, self.app._links])
        self.assertEqual(texts(self.app._links), ["Check and book"])
        stacked = row.master.pack_slaves()
        order = [stacked.index(widget) for widget in (row, self.app._next_label, self.app._more_links, self.app.tabs)]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(self.app._more_links.winfo_children()), 4)  # the label and three sites

    def test_a_fare_nobody_looked_up_is_told_that_checking_it_there_is_a_free_way_to_confirm_it(self):
        self.search()
        self.assertEqual(self.app.next_text.get(), f"{FREE} {WHY}")
        # What was there before still says how to confirm it by searching.
        self.assertIn('Tick "Confirm the best fare" and search again', self.app.notes.get())
        self.assertEqual(self.app.headline.get(), "Cheapest listed: $520 round trip, Tue 12 Mar - Tue 19 Mar (not yet confirmed)")

    def test_a_confirmed_fare_is_given_only_what_was_measured(self):
        FakeSerpApi.rich = True
        self.app.confirm.set(True)
        self.search()
        self.assertEqual(self.app.headline.get(), "Cheapest: $520 round trip, Tue 12 Mar - Tue 19 Mar")
        self.assertEqual(self.app.next_text.get(), WHY)

    def test_a_one_way_keeps_its_four_sites_and_gets_no_next_step(self):
        self.one_way()
        self.assertEqual(self.app._next_buttons.winfo_manager(), "")
        self.assertEqual(self.app._next_label.winfo_manager(), "")
        self.assertEqual(
            texts(self.app._more_links)[1:],
            ["Momondo", "Skyscanner", "Skiplagged (lists hidden-city fares)", "Kiwi.com (lists self-transfer fares)"],
        )

    def test_the_next_step_goes_away_when_the_next_result_has_none(self):
        self.search()
        self.assertEqual(self.app._next_buttons.winfo_manager(), "pack")
        self.one_way()
        self.assertEqual(self.app._next_buttons.winfo_manager(), "")
        self.assertEqual(texts(self.app._next_buttons), [])
        self.app.record_price()
        self.assertIsNone(self.app._dialog)

    def test_it_goes_away_when_the_next_search_finds_no_fares(self):
        self.search()
        self.app.depart.set(date(2030, 3, 13))
        empty = NO_FLIGHTS
        with mock.patch.object(FakeSerpApi, "search", lambda self, query: empty):
            self.search()
        self.assertEqual(self.app.headline.get(), gui.NO_FARES)
        self.assertEqual(self.app._next_buttons.winfo_manager(), "")
        self.app.record_price()
        self.assertIsNone(self.app._dialog)

    def test_the_address_can_be_copied_for_another_browser(self):
        self.search()
        self.copied.append("whatever was there before")
        with mock.patch.object(gui.webbrowser, "open") as opened:
            self.app._next_buttons.winfo_children()[1].invoke()
        self.assertEqual(self.copied, [MOMONDO])
        opened.assert_not_called()
        self.assertEqual(self.app.status.get(), "Address copied. Paste it into the address bar of another browser.")

    def open_dialog(self):
        self.app._next_buttons.winfo_children()[2].invoke()
        return self.app._dialog

    def test_the_price_found_there_is_typed_into_a_small_window_that_names_the_fare(self):
        self.search()
        dialog = self.open_dialog()
        self.assertEqual(dialog.title(), "The price you found on Momondo")
        (body,) = dialog.winfo_children()
        labels = [widget.cget("text") for widget in body.winfo_children() if isinstance(widget, gui.ttk.Label)]
        self.assertRegex(labels[0], rf"^The fare you are checking:\nTue 12 Mar - Tue 19 Mar: \$520 listed, not confirmed, fetched {TIME}$")
        self.assertEqual(
            labels[1:5],
            [
                "Price you found on Momondo, in USD",
                "Who sells it at that price",
                "Is it the same flights?",
                "Did the seller's payment page show that price?",
            ],
        )
        self.assertEqual((self.app.found_same.get(), self.app.found_held.get()), ("Not sure", "I did not get that far"))
        # Asking for it twice does not open a second one.
        self.assertIs(self.open_dialog(), dialog)

    def test_saving_keeps_the_price_the_seller_and_both_answers(self):
        self.search()
        self.open_dialog()
        self.app.found_price.set(" $1,480.50 ")
        self.app.found_seller.set(" Mytrip ")
        self.app.found_same.set("Yes")
        self.app.found_held.set("No")
        self.app._save_button.invoke()

        self.assertEqual(self.errors, [])
        (row,) = history_rows("site_prices")
        self.assertEqual(
            (row["site"], row["price"], row["seller"], row["same_flights"], row["held"]), ("Momondo", 1480.5, "Mytrip", 1, 0)
        )
        self.assertEqual((row["origin"], row["destination"], row["depart_date"], row["return_date"]), ("JFK", "LHR", "2030-03-12", "2030-03-19"))
        self.assertEqual((row["our_price"], row["our_confirmed"]), (520, 0))
        self.assertIsNone(self.app._dialog)
        said = self.app.next_text.get().splitlines()
        self.assertEqual(said[0], f"{FREE} {WHY}")
        self.assertRegex(
            said[1],
            rf"^Saved\. Momondo: \$1,480\.50 from Mytrip, typed {TIME}\. "
            rf"That is \$960\.50 more than the \$520 listed fare fetched {TIME}\.$",
        )
        self.assertEqual(len(FakeSerpApi.searches), 1)  # recording sent nothing

    def test_only_the_price_is_needed_and_a_confirmed_fare_is_kept_as_confirmed(self):
        FakeSerpApi.rich = True
        self.app.confirm.set(True)
        self.search()
        self.open_dialog()
        self.app.found_price.set("499")
        self.app._save_button.invoke()
        (row,) = history_rows("site_prices")
        self.assertEqual((row["price"], row["seller"], row["same_flights"], row["held"]), (499, "", None, None))
        self.assertEqual((row["our_price"], row["our_confirmed"]), (520, 1))
        self.assertEqual(row["itinerary_key"], "TA1@2030-03-12T18:30|TA2@2030-03-19T09:00")
        self.assertRegex(self.app.next_text.get(), rf"That is \$21 less than the \$520 confirmed fare fetched {TIME}\.$")

    def test_something_that_is_not_a_price_is_explained_and_nothing_is_saved(self):
        self.search()
        dialog = self.open_dialog()
        self.app._save_button.invoke()
        self.app.found_price.set("cheap")
        self.app._save_button.invoke()
        self.assertEqual(
            self.errors,
            ["Type the price you found first, as a number such as 412", "'cheap' is not a price. Type a number such as 412"],
        )
        self.assertEqual(history_rows("site_prices"), [])
        self.assertIs(self.app._dialog, dialog)  # still open, to try again
        self.assertEqual(self.app.next_text.get(), f"{FREE} {WHY}")

    def test_closing_the_small_window_empties_it_for_the_next_price(self):
        self.search()
        self.open_dialog()
        self.app.found_price.set("499")
        self.app.found_same.set("Yes")
        self.app._close_dialog()
        self.assertIsNone(self.app._dialog)
        self.assertEqual((self.app.found_price.get(), self.app.found_same.get()), ("", "Not sure"))
        self.assertEqual(history_rows("site_prices"), [])

    def test_a_new_search_closes_it_and_forgets_what_the_last_one_saved(self):
        self.search()
        self.open_dialog()
        self.app.found_price.set("499")
        self.app._save_button.invoke()
        self.open_dialog()
        self.app.found_price.set("450")
        self.app.depart.set(date(2030, 3, 13))
        self.app.back.set(date(2030, 3, 20))
        self.search()
        self.assertIsNone(self.app._dialog)
        self.assertEqual(self.app.found_price.get(), "")
        self.assertEqual(self.app.next_text.get(), f"{FREE} {WHY}")
        self.assertEqual(len(history_rows("site_prices")), 1)


TRIP = "JFK -> LHR, Tue 12 Mar - Tue 19 Mar"


class WatchWindowTest(unittest.TestCase):
    """What Phase 4 added to the window: one button, and a small window where a watch's rules are set."""

    setUp = GuiTest.setUp
    tearDown = GuiTest.tearDown
    search = GuiTest.search

    def open_watch(self):
        self.app._watch_button.invoke()
        self.root.update()
        return self.app._watch_dialog

    def add(self):
        self.app._add_button.invoke()
        deadline = time.monotonic() + 10
        while self.app.adding:
            self.assertLess(time.monotonic(), deadline, "the watch was never added")
            self.root.update()
            time.sleep(0.01)

    def watches(self):
        return [standing.watch for standing in watch.listed()]

    def test_the_button_sits_beside_search_and_is_greyed_out_until_there_is_a_fare(self):
        button = self.app._watch_button
        self.assertEqual((button.cget("text"), str(button.cget("state"))), ("Watch this fare...", "disabled"))
        self.assertIs(button.master, self.app._search_button.master)
        self.assertIsNone(self.open_watch())
        self.search()
        self.assertEqual(str(button.cget("state")), "normal")

    def test_it_adds_nothing_to_the_rows_under_the_headline_or_to_the_tabs(self):
        self.search()
        row = self.app._links.master
        self.assertEqual(row.pack_slaves(), [self.app._next_buttons, self.app._links])
        self.assertEqual(len(self.app.tabs.tabs()), 2)

    def test_the_small_window_names_the_fare_with_its_fetch_time_and_asks_for_the_rules(self):
        self.search()
        dialog = self.open_watch()
        self.assertEqual(dialog.title(), "Watch this fare")
        (body,) = dialog.winfo_children()
        labels = [widget.cget("text") for widget in body.winfo_children() if isinstance(widget, gui.ttk.Label)]
        self.assertRegex(
            labels[0],
            rf"^{TRIP}: the cheapest fare on these dates, with any airline\.\n"
            rf"Cheapest listed: \$520 round trip, Tue 12 Mar - Tue 19 Mar \(not yet confirmed\)\. Fetched {TIME}\.$",
        )
        self.assertEqual(
            labels[1:],
            [
                "Tell me when it is below this price, in USD",
                "or this many percent under its 30-day median (0 for never)",
                "or below Google's typical range for these dates",
                "Check it every this many days",
                "A check uses one search, and up to 3 more when the price is worth an alert. Adding a watch uses none.\n"
                "To see or remove your watches: py -m flightsleuth watch list",
            ],
        )
        self.assertEqual(
            (self.app.watch_below.get(), self.app.watch_drop.get(), self.app.watch_typical.get(), self.app.watch_every.get()),
            ("", "15", True, "1"),
        )
        # Asking for it twice does not open a second one.
        self.assertIs(self.open_watch(), dialog)

    def test_adding_keeps_the_watch_with_its_rules_and_spends_no_search(self):
        self.search()
        self.open_watch()
        self.app.watch_below.set(" $450 ")
        self.app.watch_drop.set("20")
        self.app.watch_typical.set(False)
        self.app.watch_every.set("4")
        self.add()

        self.assertEqual(self.errors, [])
        (found,) = self.watches()
        self.assertEqual(found.trip, watch.trip("JFK", "LHR", date(2030, 3, 12), date(2030, 3, 19)))
        self.assertEqual((found.below, found.drop_percent, found.typical, found.every_days), (450, 20, False, 4))
        self.assertEqual(len(FakeSerpApi.searches), 1)  # the search that found the fare
        self.assertIsNone(self.app._watch_dialog)
        # No topic has been set up in this test, and that is what the owner most needs to hear.
        self.assertEqual(self.app.status.get(), "Watching it, but alerts are not set up. Run: py -m flightsleuth watch setup")

    def test_with_alerts_set_up_the_status_says_how_often_and_what_is_spare(self):
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: "flightsleuth-test-topic"}):
            self.search()
            self.open_watch()
            self.add()
            self.assertEqual(self.app.status.get(), "Watching this fare every 1 day. The searches cover it, with 117 to spare.")
            self.open_watch()
            self.app.watch_every.set("4")
            self.add()
            # The first watch's 33 and this one's 11, of the 150 a month watches may use.
            self.assertEqual(self.app.status.get(), "Watching this fare every 4 days. The searches cover it, with 106 to spare.")

    def test_left_as_it_opens_it_watches_daily_for_a_drop_under_the_median_or_the_typical_range(self):
        self.search()
        self.open_watch()
        self.add()
        (found,) = self.watches()
        self.assertEqual((found.below, found.drop_percent, found.typical, found.every_days), (None, 15, True, 1))

    def test_a_one_way_fare_can_be_watched(self):
        self.app.round_trip.set(False)
        self.app._trip_kind_changed()
        self.search()
        self.open_watch()
        self.add()
        self.assertEqual(self.watches()[0].trip, watch.trip("JFK", "LHR", date(2030, 3, 12)))

    def test_a_watch_does_not_carry_the_bags_a_search_added_and_the_small_window_says_so(self):
        self.app.bags.set("1")
        self.search()
        (body,) = self.open_watch().winfo_children()
        self.assertIn("\nA watch prices the fare as an ordinary search does: no carry-on bags added", body.winfo_children()[0].cget("text"))
        self.add()
        self.assertEqual(self.watches()[0].trip.bags, 0)

    def test_something_that_is_not_a_price_is_explained_and_nothing_is_added(self):
        self.search()
        self.open_watch()
        self.app.watch_below.set("cheap")
        self.add()
        self.assertEqual(self.errors, ["'cheap' is not a price. Type a number such as 412, or leave it empty"])
        self.assertEqual(self.watches(), [])
        self.assertIsNotNone(self.app._watch_dialog)

    def test_a_watch_with_every_rule_turned_off_is_explained(self):
        self.search()
        self.open_watch()
        self.app.watch_drop.set("0")
        self.app.watch_typical.set(False)
        self.add()
        self.assertEqual(len(self.errors), 1)
        self.assertIn("A watch needs at least one rule", self.errors[0])
        self.assertEqual(self.watches(), [])
        self.assertEqual(str(self.app._add_button.cget("state")), "normal")

    def test_a_watch_the_searches_cannot_cover_is_refused_with_the_sums(self):
        self.search()
        FakeSerpApi.account_reply["total_searches_left"] = 9
        self.open_watch()
        self.add()
        (message,) = self.errors
        self.assertEqual(
            message.splitlines()[:6],
            [
                "The searches cannot cover this watch. Checked every 5 days it would fit.",
                "",
                "In the next 30 days (the account did not say when its searches reset):",
                f"  {TRIP}: 30 checks, one every 1 day",
                "  30 checks at 1 search each, and 3 held back for each of 1 watch to confirm an alert: 30 + 3 = 33",
                "  9 searches are left, so that is 24 too many.",
            ],
        )
        self.assertEqual(self.watches(), [])
        # The small window stays open, so the interval can be changed and tried again.
        self.app.watch_every.set("5")
        self.add()
        self.assertEqual([found.every_days for found in self.watches()], [5])

    def test_a_fourth_watch_is_refused(self):
        self.search()
        for _ in range(3):
            self.open_watch()
            self.app.watch_every.set("10")
            self.add()
        self.open_watch()
        self.add()
        self.assertIn("There are already 3 watches, which is the most", self.errors[0])
        self.assertEqual(len(self.watches()), 3)

    def test_two_one_way_tickets_winning_leave_nothing_to_watch(self):
        self.app.split.set(True)
        self.search()
        self.assertEqual(self.app.headline.get()[:38], "Cheapest: $400 as two one-way tickets,")
        self.assertEqual(str(self.app._watch_button.cget("state")), "disabled")

    def test_a_search_that_finds_no_fares_greys_the_button_out_again_and_closes_the_small_window(self):
        self.search()
        self.open_watch()
        empty = NO_FLIGHTS
        self.app.depart.set(date(2030, 3, 13))
        with mock.patch.object(FakeSerpApi, "search", lambda fake, query: empty):
            self.search()
        self.assertIsNone(self.app._watch_dialog)
        self.assertEqual(str(self.app._watch_button.cget("state")), "disabled")

    def test_a_new_search_closes_the_small_window_and_the_next_one_is_about_the_new_fare(self):
        self.search()
        self.open_watch()
        self.app.depart.set(date(2030, 3, 13))
        self.app.back.set(date(2030, 3, 20))
        self.search()
        self.assertIsNone(self.app._watch_dialog)
        self.open_watch()
        self.add()
        self.assertEqual(self.watches()[0].trip.depart_date, date(2030, 3, 13))


if __name__ == "__main__":
    unittest.main()
