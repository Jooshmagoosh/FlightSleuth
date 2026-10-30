"""A small window over the same search the command line runs."""
from __future__ import annotations

import calendar
import queue
import sqlite3
import threading
import tkinter as tk
import tkinter.font
import webbrowser
from datetime import date, timedelta
from tkinter import messagebox, simpledialog, ttk

from . import config, notify, scan, typed, watch
from .errors import UserError
from .models import Query
from .render import ALTERNATIVE_HEADERS, NO_FARES, NextStep, Summary, budget_lines, summarise, watch_title

STOPS = {"Any number of stops": "any", "Nonstop only": "nonstop", "1 stop or fewer": "1", "2 stops or fewer": "2"}
CABINS = {"Economy": "economy", "Premium economy": "premium", "Business": "business", "First": "first"}
MAX_FLEX = 7
MAX_ADULTS = 9
MAX_NIGHTS = 30
CONFIRM_BOX = "Confirm the best fare"
CLOSER_TAB = "Best fare, looked at closely"
NOT_LOOKED = (
    f'Nothing has been looked at closely yet. Tick "{CONFIRM_BOX}" before searching, or search a whole range '
    f"of dates, and the return flights, the sellers and the other ways to fly those dates appear here."
)
KEY_PROMPT = "Paste your SerpApi key here.\nYou can copy it from serpapi.com/manage-api-key"
BEST_ROW_COLOUR = "#d9f2d9"
COPY_BUTTON = "Copy the address"
COPIED = "Address copied. Paste it into the address bar of another browser."
RECORD_BUTTON = "Save the price you found there..."
WATCH_BUTTON = "Watch this fare..."
WATCH_NOTE = (
    f"A check uses one search, and up to {watch.HOLD} more when the price is worth an alert. Adding a watch uses none.\n"
    "To see or remove your watches: py -m flightsleuth watch list"
)
# The two questions that go with a price found on another site, and what each answer is kept as.
SAME_FLIGHTS = {"Not sure": None, "Yes": True, "No": False}
HELD = {"I did not get that far": None, "Yes": True, "No": False}


def _whole(text: str, name: str, low: int, high: int) -> int:
    try:
        number = int(text)
    except ValueError:
        number = low - 1
    if not low <= number <= high:
        raise UserError(f"{name} must be a number from {low} to {high}")
    return number


class DatePicker(ttk.Frame):
    """Month, day and year dropdowns, so nobody has to type a date format."""

    def __init__(self, parent: tk.Misc, initial: date):
        super().__init__(parent)
        this_year = date.today().year
        self._month = ttk.Combobox(self, values=list(calendar.month_name)[1:], width=10, state="readonly")
        self._day = ttk.Combobox(self, values=[str(d) for d in range(1, 32)], width=3, state="readonly")
        self._year = ttk.Combobox(self, values=[str(this_year), str(this_year + 1)], width=5, state="readonly")
        for box in (self._month, self._day, self._year):
            box.pack(side="left", padx=(0, 4))
        self.set(initial)

    def set(self, value: date) -> None:
        self._month.current(value.month - 1)
        self._day.set(str(value.day))
        self._year.set(str(value.year))

    def get(self) -> date:
        try:
            return date(int(self._year.get()), self._month.current() + 1, int(self._day.get()))
        except ValueError:
            raise UserError(f"{self._month.get()} {self._day.get()}, {self._year.get()} is not a real date") from None

    def enable(self, enabled: bool) -> None:
        for box in (self._month, self._day, self._year):
            box.configure(state="readonly" if enabled else "disabled")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.busy = False
        # The search runs on another thread; it reports back through this queue
        # because only the main thread may touch the widgets.
        self._events: queue.Queue = queue.Queue()
        # The fare a watch would be set on: the best date pair of the result on show, with its price in words.
        self._watched: tuple[Query, str] | None = None
        self._watch_dialog: tk.Toplevel | None = None
        self.adding = False  # a watch is being set against the account's searches, off the main thread
        self.watch_below = tk.StringVar()
        self.watch_drop = tk.StringVar(value=f"{watch.DEFAULT_DROP:g}")
        self.watch_typical = tk.BooleanVar(value=True)
        self.watch_every = tk.StringVar(value="1")
        self._font = tkinter.font.nametofont("TkDefaultFont")
        root.title("Flightsleuth")
        self._build_form()
        self._build_results()
        root.bind("<Return>", lambda event: self.search())
        root.bind("<Destroy>", self._closed)
        root.update_idletasks()
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
        # A fixed starting size, so the window does not jump wider when the first results arrive.
        width = max(root.winfo_reqwidth(), self._font.measure("0") * 130)
        # Two lines taller than it asks for, where the screen has the room: a round trip's next step
        # brings a sentence with it, and the tables should not be what makes way.
        height = root.winfo_reqheight() + 2 * self._font.metrics("linespace")
        height = min(height, max(root.winfo_reqheight(), root.winfo_screenheight() - 80))
        root.geometry(f"{width}x{height}")
        self._poll()

    def _build_form(self) -> None:
        form = ttk.Frame(self.root, padding=12)
        form.pack(fill="x")

        def row() -> ttk.Frame:
            frame = ttk.Frame(form)
            frame.pack(fill="x", pady=4)
            return frame

        def label(parent: tk.Misc, text: str, **options) -> None:
            ttk.Label(parent, text=text, **options).pack(side="left", padx=(0, 6))

        self.origin = tk.StringVar()
        self.destination = tk.StringVar()
        places = row()
        label(places, "From")
        ttk.Entry(places, textvariable=self.origin, width=14).pack(side="left", padx=(0, 16))
        label(places, "To")
        ttk.Entry(places, textvariable=self.destination, width=14).pack(side="left", padx=(0, 16))
        label(places, "Airport codes. For several, use commas: JFK,EWR", foreground="gray40")

        self.round_trip = tk.BooleanVar(value=True)
        kind = row()
        for text, value in (("Round trip", True), ("One way", False)):
            ttk.Radiobutton(
                kind, text=text, variable=self.round_trip, value=value, command=self._trip_kind_changed
            ).pack(side="left", padx=(0, 16))

        leave = date.today() + timedelta(days=30)
        dates = row()
        label(dates, "Leave on")
        self.depart = DatePicker(dates, leave)
        self.depart.pack(side="left", padx=(0, 16))
        label(dates, "Come back on")
        self.back = DatePicker(dates, leave + timedelta(days=7))
        self.back.pack(side="left")

        self.flex = tk.StringVar(value="0")
        flexible = row()
        label(flexible, "Also check")
        self._flex_box = ttk.Spinbox(flexible, from_=0, to=MAX_FLEX, textvariable=self.flex, width=3)
        self._flex_box.pack(side="left", padx=(0, 6))
        label(flexible, "days earlier and later")

        # A whole range of dates instead: "Leave on" becomes the first day anyone may leave.
        self.window = tk.BooleanVar(value=False)
        self.nights_low = tk.StringVar(value="5")
        self.nights_high = tk.StringVar(value="9")
        ranged = row()
        ttk.Checkbutton(
            ranged, text="Or search a whole range: leave any day up to", variable=self.window, command=self._trip_kind_changed
        ).pack(side="left", padx=(0, 6))
        self.latest = DatePicker(ranged, leave + timedelta(days=30))
        self.latest.pack(side="left", padx=(0, 6))
        label(ranged, "staying")
        self._nights_boxes = [
            ttk.Spinbox(ranged, from_=1, to=MAX_NIGHTS, textvariable=variable, width=3)
            for variable in (self.nights_low, self.nights_high)
        ]
        self._nights_boxes[0].pack(side="left", padx=(0, 6))
        label(ranged, "to")
        self._nights_boxes[1].pack(side="left", padx=(0, 6))
        label(ranged, "nights")

        self.stops = tk.StringVar(value=next(iter(STOPS)))
        self.cabin = tk.StringVar(value=next(iter(CABINS)))
        self.adults = tk.StringVar(value="1")
        options = row()
        label(options, "Stops")
        ttk.Combobox(options, textvariable=self.stops, values=list(STOPS), width=20, state="readonly").pack(
            side="left", padx=(0, 16)
        )
        label(options, "Cabin")
        ttk.Combobox(options, textvariable=self.cabin, values=list(CABINS), width=17, state="readonly").pack(
            side="left", padx=(0, 16)
        )
        label(options, "Adults")
        ttk.Spinbox(options, from_=1, to=MAX_ADULTS, textvariable=self.adults, width=3).pack(
            side="left", padx=(0, 16)
        )
        self.bags = tk.StringVar(value="0")
        label(options, "Carry-on bags in the price")
        ttk.Spinbox(options, from_=0, to=MAX_ADULTS, textvariable=self.bags, width=3).pack(side="left")

        self.split = tk.BooleanVar(value=False)
        self._split_box = ttk.Checkbutton(
            row(), text="Also check whether two one-way tickets are cheaper (uses more searches)", variable=self.split
        )
        self._split_box.pack(side="left")

        self.confirm = tk.BooleanVar(value=False)
        self._confirm_box = ttk.Checkbutton(
            row(),
            text=f"{CONFIRM_BOX}: look up its return flights and who sells it (up to 6 more searches)",
            variable=self.confirm,
        )
        self._confirm_box.pack(side="left")

        self.status = tk.StringVar()
        actions = row()
        self._search_button = ttk.Button(actions, text="Search", command=self.search)
        self._search_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="SerpApi key...", command=self.ask_key).pack(side="left", padx=(0, 8))
        # Greyed out until a search has found a fare to watch.
        self._watch_button = ttk.Button(actions, text=WATCH_BUTTON, command=self.watch_fare, state="disabled")
        self._watch_button.pack(side="left", padx=(0, 12))
        ttk.Label(actions, textvariable=self.status).pack(side="left")
        self._trip_kind_changed()  # start with the range's own boxes greyed out

    def _build_results(self) -> None:
        results = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        results.pack(fill="both", expand=True)

        bold = self._font.copy()
        bold.configure(weight="bold", size=self._font.cget("size") + 2)
        self.headline = tk.StringVar(value="Fill in the trip above and press Search.")
        self.detail = tk.StringVar()
        self.notes = tk.StringVar()  # what was and was not confirmed about the price above
        wide = self._font.measure("0") * 110
        ttk.Label(results, textvariable=self.headline, font=bold).pack(anchor="w")
        ttk.Label(results, textvariable=self.detail, justify="left").pack(anchor="w")
        ttk.Label(results, textvariable=self.notes, justify="left", wraplength=wide).pack(anchor="w")
        # What to do next with the best fare. It takes its place above the links only when there is something to do.
        self._step: NextStep | None = None
        self._dialog: tk.Toplevel | None = None
        self.next_text = tk.StringVar()
        self.found_price = tk.StringVar()
        self.found_seller = tk.StringVar()
        self.found_same = tk.StringVar(value=next(iter(SAME_FLIGHTS)))
        self.found_held = tk.StringVar(value=next(iter(HELD)))
        # One row of buttons: the next step first, then the booking links. The next step's buttons and
        # the sentence under them are only packed while a result has one, so they cost no room otherwise.
        actions = ttk.Frame(results)
        actions.pack(anchor="w", pady=(4, 2))
        self._next_buttons = ttk.Frame(actions)
        self._links = ttk.Frame(actions)
        self._links.pack(side="left")
        self._next_label = ttk.Label(results, textvariable=self.next_text, justify="left", wraplength=wide)
        self._more_links = ttk.Frame(results)  # the same dates on other sites
        self._more_links.pack(anchor="w", pady=(0, 8))

        ttk.Style().configure("Treeview", rowheight=self._font.metrics("linespace") + 6)
        # Two tabs, so that looking closer at a fare adds no height to the window.
        self.tabs = ttk.Notebook(results)
        self.tabs.pack(fill="both", expand=True)
        prices = ttk.Frame(self.tabs, padding=(6, 6, 6, 0))
        closer = ttk.Frame(self.tabs, padding=(6, 6, 6, 0))
        self.tabs.add(prices, text="Prices")
        self.tabs.add(closer, text=CLOSER_TAB)

        ttk.Label(prices, text="Cheapest price for each date").pack(anchor="w")
        self.dates = self._table(prices, height=4, expand=False)
        self.flights_title = tk.StringVar(value="Cheapest flights")
        ttk.Label(prices, textvariable=self.flights_title).pack(anchor="w")
        self.flights = self._table(prices, height=6, expand=True)

        self.closer = tk.StringVar(value=NOT_LOOKED)
        ttk.Label(closer, textvariable=self.closer, justify="left", wraplength=wide).pack(anchor="w")
        ttk.Label(closer, text="Other ways to fly these dates").pack(anchor="w", pady=(6, 0))
        self.ways = self._table(closer, height=5, expand=True)

    def _table(self, parent: tk.Misc, *, height: int, expand: bool) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=expand, pady=(2, 8))
        # The first column is the tree's own, and holds when each row's price was fetched.
        tree = ttk.Treeview(frame, show="tree headings", height=height, selectmode="browse")
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.tag_configure("best", background=BEST_ROW_COLOUR)
        bar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return tree

    def _fill(
        self,
        tree: ttk.Treeview,
        headers: list[str],
        rows: list[list[str]],
        best: int | None = None,
        fetched: list[str] | None = None,
    ) -> None:
        tree.delete(*tree.get_children())
        columns = [f"c{index}" for index in range(len(headers))]
        tree["columns"] = columns
        fetched = fetched or [""] * len(rows)
        tree.heading("#0", text="Fetched", anchor="w")
        widest = max(["Fetched", *fetched], key=len)
        tree.column("#0", width=self._font.measure(widest + "mmmmm"), anchor="w", stretch=False)
        for index, (column, header) in enumerate(zip(columns, headers)):
            widest = max([header, *(row[index] for row in rows)], key=len)
            tree.heading(column, text=header, anchor="w")
            tree.column(column, width=self._font.measure(widest + "mmm"), anchor="w", stretch=column == columns[-1])
        for index, row in enumerate(rows):
            tree.insert("", "end", text=fetched[index], values=row, tags=("best",) if index == best else ())

    def _trip_kind_changed(self) -> None:
        round_trip = self.round_trip.get()
        ranged = self.window.get()
        self.back.enable(round_trip and not ranged)
        self.latest.enable(ranged)
        self._flex_box.configure(state="disabled" if ranged else "normal")
        for box in self._nights_boxes:
            box.configure(state="normal" if ranged and round_trip else "disabled")
        # A range always gets the closer look, and that prices the two one-ways itself.
        self._split_box.configure(state="normal" if round_trip and not ranged else "disabled")
        self._confirm_box.configure(state="disabled" if ranged else "normal")

    def _read_form(self) -> scan.Request:
        if not self.origin.get().strip() or not self.destination.get().strip():
            raise UserError("fill in both the From and To airports")
        round_trip = self.round_trip.get()
        shared = dict(
            origin=scan.airports(self.origin.get()),
            destination=scan.airports(self.destination.get()),
            depart=self.depart.get(),
            stops=STOPS[self.stops.get()],
            cabin=CABINS[self.cabin.get()],
            adults=_whole(self.adults.get(), "the number of adults", 1, MAX_ADULTS),
            bags=_whole(self.bags.get(), "the number of carry-on bags", 0, MAX_ADULTS),
        )
        if self.window.get():
            nights = None
            if round_trip:
                low = _whole(self.nights_low.get(), "the fewest nights", 1, MAX_NIGHTS)
                high = _whole(self.nights_high.get(), "the most nights", 1, MAX_NIGHTS)
                if high < low:
                    raise UserError("the most nights is fewer than the fewest nights")
                nights = (low, high)
            return scan.Request(**shared, window=(shared["depart"], self.latest.get()), nights=nights)
        return scan.Request(
            **shared,
            return_date=self.back.get() if round_trip else None,
            flex=_whole(self.flex.get(), "the number of days earlier and later", 0, MAX_FLEX),
            split=round_trip and self.split.get(),
            check=True if self.confirm.get() else None,
        )

    def ask_key(self) -> bool:
        """Ask for the SerpApi key and save it. False if the user backed out."""
        key = simpledialog.askstring("SerpApi key", KEY_PROMPT, parent=self.root)
        if not key or not key.strip():
            return False
        config.save_api_key(key)
        self.status.set("Key saved.")
        return True

    def search(self) -> None:
        if self.busy:
            return
        try:
            request = self._read_form()
        except UserError as e:
            self._fail(str(e))
            return
        if config.find_api_key() is None and not self.ask_key():
            return
        self._set_busy(True)
        self.status.set("Checking what needs searching...")
        threading.Thread(target=_work, args=(request, self._events.put), daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                getattr(self, f"_on_{kind}")(payload)
        except queue.Empty:
            pass
        self._next_poll = self.root.after(100, self._poll)

    def _closed(self, event: tk.Event) -> None:
        # Every widget inside the window reports its own destruction here too.
        if event.widget is self.root:
            self.root.after_cancel(self._next_poll)

    def _on_plan(self, plan: scan.Plan) -> None:
        # A search in stages only knows the most it may use until its replies come in.
        self._of = "of up to" if plan.later else "of"
        self.status.set(f"Searching... 0 {self._of} {plan.live}" if plan.live else "Using saved results...")

    def _on_progress(self, progress: tuple[int, int]) -> None:
        done, live = progress
        self.status.set(f"Searching... {done} {self._of} {live}")

    def _on_done(self, finished: tuple[scan.Plan, scan.Outcome]) -> None:
        plan, outcome = finished
        self._set_busy(False)
        if outcome.trips:
            self.show(
                summarise(outcome.trips, outcome.results, split=outcome.split, checks=outcome.checks, notes=outcome.notes)
            )
        else:
            self.headline.set(NO_FARES)
            self._show_next(None)  # the step on show was for the last result's fare
            self._offer_watch(None)
        status = f"Done. Searches used: {outcome.live}. Reused from earlier: {outcome.saved}."
        if outcome.searches_left is not None:
            status += f" About {outcome.searches_left} left this month."
        self.status.set(status)

    def _on_error(self, message: str) -> None:
        self._set_busy(False)
        self.status.set("")
        self._fail(message)

    def show(self, summary: Summary) -> None:
        self.headline.set(summary.headline)
        detail = ["   ".join(cells) for cells in summary.split_legs]
        if summary.insight:
            detail.append(summary.insight)
        self.detail.set("\n".join(detail))

        notes = list(summary.notes)
        closer = summary.closer[0] if summary.closer else None
        if summary.caveat:
            notes.insert(0, f'{summary.caveat} Tick "{CONFIRM_BOX}" and search again to look them up.')
        if closer:
            notes.insert(0, f'{closer.title}. The "{CLOSER_TAB}" tab has the details.')
            self.closer.set("\n".join([*("   ".join(cells) for cells in closer.legs), *closer.lines]))
        else:
            self.closer.set(NOT_LOOKED)
        self.notes.set("\n".join(notes))

        for frame in (self._next_buttons, self._links, self._more_links):
            for old in frame.winfo_children():
                old.destroy()
        self._show_next(summary.next_step)
        self._offer_watch(summary)
        for text, url in summary.links:
            # The address comes from the API's reply, so only ever hand the browser a web page.
            if url.startswith("https://"):
                ttk.Button(self._links, text=text, command=lambda url=url: webbrowser.open(url)).pack(
                    side="left", padx=(0, 8)
                )
        if summary.other_links:
            ttk.Label(self._more_links, text="Same dates on:").pack(side="left", padx=(0, 6))
        for text, url in summary.other_links:
            ttk.Button(self._more_links, text=text, command=lambda url=url: webbrowser.open(url)).pack(
                side="left", padx=(0, 8)
            )

        ways = closer.alternatives if closer else []
        self._fill(self.ways, ALTERNATIVE_HEADERS[:-1], [row[:-1] for row in ways], fetched=[row[-1] for row in ways])

        best = summary.rows.index(summary.best) if summary.best else None
        self._fill(
            self.dates,
            summary.date_headers,
            [summary.date_cells(row) for row in summary.rows],
            best,
            fetched=[summary.date_fetched(row) for row in summary.rows],
        )
        note = " (the price is for both ways; the flight shown is the one going out)" if summary.round_trip else ""
        self.flights_title.set(f"Cheapest flights{note}")
        self._fill(
            self.flights,
            summary.offer_headers,
            [summary.offer_cells(offer) for offer in summary.offers],
            fetched=[summary.offer_fetched(offer) for offer in summary.offers],
        )

    def _show_next(self, step: NextStep | None) -> None:
        """Put the next step above the links, or take it away when the result has none."""
        self._step = step
        self._close_dialog()  # whatever was being typed was about the last result
        if step is None:
            self._next_buttons.pack_forget()
            self._next_label.pack_forget()
            return
        buttons = (
            ttk.Button(self._next_buttons, text=step.label, default="active", command=lambda: webbrowser.open(step.url)),
            ttk.Button(self._next_buttons, text=COPY_BUTTON, command=lambda: self._copy(step.url)),
            ttk.Button(self._next_buttons, text=RECORD_BUTTON, command=self.record_price),
        )
        for button in buttons:
            button.pack(side="left", padx=(0, 8))
        self.next_text.set(" ".join(step.lines))
        self._next_buttons.pack(side="left", before=self._links)
        self._next_label.pack(anchor="w", pady=(0, 2), before=self._more_links)

    def _copy(self, url: str) -> None:
        """For opening the page in a browser other than the usual one."""
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.status.set(COPIED)

    def record_price(self) -> None:
        """Open the small window where the price found on the next step's site is typed in."""
        step = self._step
        if step is None:
            return
        if self._dialog is not None:
            self._dialog.lift()
            return
        self._dialog, body = self._small_window(f"The price you found on {step.site}", self._close_dialog)
        intro = ttk.Label(body, text=f"The fare you are checking:\n{step.against}", justify="left")
        price = ttk.Entry(body, textvariable=self.found_price, width=12)
        fields = (
            (f"Price you found on {step.site}, in {step.trip.currency}", price),
            ("Who sells it at that price", ttk.Entry(body, textvariable=self.found_seller, width=28)),
            (
                "Is it the same flights?",
                ttk.Combobox(body, textvariable=self.found_same, values=list(SAME_FLIGHTS), width=25, state="readonly"),
            ),
            (
                "Did the seller's payment page show that price?",
                ttk.Combobox(body, textvariable=self.found_held, values=list(HELD), width=25, state="readonly"),
            ),
        )
        note = "Only the price is needed. Leave the rest if you are not sure."
        self._save_button = _lay_out(body, intro, fields, note, ("Save", self.save_found), self._close_dialog)
        price.focus_set()

    def save_found(self) -> None:
        """Keep what was typed about the next step's site, and say under the step what was kept."""
        step = self._step
        if step is None:
            return
        entered = self.found_price.get().strip()
        try:
            amount = typed.price(entered)
        except ValueError:
            self._fail(f"{entered!r} is not a price. Type a number such as 412", parent=self._dialog)
            return
        if amount is None:
            self._fail("type the price you found first, as a number such as 412", parent=self._dialog)
            return
        try:
            line = typed.record(
                step,
                amount,
                seller=self.found_seller.get(),
                same_flights=SAME_FLIGHTS[self.found_same.get()],
                held=HELD[self.found_held.get()],
            )
        except (sqlite3.Error, OSError) as e:
            self._fail(f"that price could not be saved: {e}", parent=self._dialog)
            return
        self._close_dialog()
        self.next_text.set(" ".join(step.lines) + "\n" + line)

    def _close_dialog(self) -> None:
        """Close the price window, if it is open, and empty its boxes for the next price."""
        if self._dialog is not None:
            self._dialog.destroy()
            self._dialog = None
        self.found_price.set("")
        self.found_seller.set("")
        self.found_same.set(next(iter(SAME_FLIGHTS)))
        self.found_held.set(next(iter(HELD)))

    def _offer_watch(self, summary: Summary | None) -> None:
        """Let the best fare on show be watched, or grey the button out when there is none."""
        self._close_watch()  # whatever was being typed was about the last result
        best = summary.best if summary else None
        if best is None or best.whole is None or best.split_wins:
            self._watched = None
        else:
            # The fare the headline names, with the time it was fetched beside it.
            fare = summary.next_step.fare if summary.next_step else best.whole
            self._watched = (best.trip, f"{summary.headline}. Fetched {summary.offer_fetched(fare)}.")
        self._watch_button.configure(state="normal" if self._watched else "disabled")

    def watch_fare(self) -> None:
        """Open the small window where the rules for watching the fare on show are set."""
        if self._watched is None:
            return
        if self._watch_dialog is not None:
            self._watch_dialog.lift()
            return
        trip, price = self._watched
        self._watch_dialog, body = self._small_window("Watch this fare", self._close_watch)
        about = [f"{watch_title(trip)}: the cheapest fare on these dates, with any airline.", price]
        if trip.bags or trip.market:
            about.append("A watch prices the fare as an ordinary search does: no carry-on bags added, and no other country's prices.")
        intro = ttk.Label(body, text="\n".join(about), justify="left", wraplength=self._font.measure("0") * 80)
        below = ttk.Entry(body, textvariable=self.watch_below, width=12)
        fields = (
            (f"Tell me when it is below this price, in {trip.currency}", below),
            (
                f"or this many percent under its {watch.MEDIAN_DAYS}-day median (0 for never)",
                ttk.Spinbox(body, from_=0, to=99, textvariable=self.watch_drop, width=4),
            ),
            (
                "or below Google's typical range for these dates",
                ttk.Checkbutton(body, variable=self.watch_typical),
            ),
            (
                "Check it every this many days",
                ttk.Spinbox(body, from_=1, to=watch.LONGEST_INTERVAL, textvariable=self.watch_every, width=4),
            ),
        )
        self._add_button = _lay_out(body, intro, fields, WATCH_NOTE, ("Watch", self.add_watch), self._close_watch)
        below.focus_set()

    def _small_window(self, title: str, close) -> tuple[tk.Toplevel, ttk.Frame]:
        """A small window over the main one, which `close` closes. Returns it, and the frame to fill."""
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.bind("<Escape>", lambda event: close())
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        return dialog, body

    def add_watch(self) -> None:
        """Set the watch against the searches there are, off the main thread, and keep it if they cover it."""
        if self._watched is None or self.adding:
            return
        entered = self.watch_below.get().strip()
        try:
            try:
                below = typed.price(entered)
            except ValueError:
                raise UserError(f"{entered!r} is not a price. Type a number such as 412, or leave it empty") from None
            try:
                drop = float(self.watch_drop.get())
            except ValueError:
                raise UserError("the percentage under the median must be a number from 0 to 99") from None
            rules = dict(
                below=below,
                drop_percent=drop,
                typical=self.watch_typical.get(),
                every_days=_whole(self.watch_every.get(), "the days between checks", 1, watch.LONGEST_INTERVAL),
            )
        except UserError as e:
            self._fail(str(e), parent=self._watch_dialog)
            return
        self.adding = True
        self._add_button.configure(state="disabled")
        threading.Thread(target=_watch_work, args=(self._watched[0], rules, self._events.put), daemon=True).start()

    def _on_watched(self, added: watch.Added) -> None:
        self.adding = False
        self._close_watch()
        # Kept short: the line shares its row with three buttons.
        if config.ntfy_topic() is None:
            self.status.set(f"Watching it, but alerts are not set up. Run: {notify.SETUP}")
            return
        every = added.watch.every_days
        status = f"Watching this fare every {every} day{'s' if every != 1 else ''}."
        if added.budget:
            status += f" The searches cover it, with {added.budget.now.spare} to spare."
        self.status.set(status)

    def _on_unwatched(self, message: str) -> None:
        self.adding = False
        if self._watch_dialog is not None:
            self._add_button.configure(state="normal")
        self._fail(message, parent=self._watch_dialog)

    def _close_watch(self) -> None:
        if self._watch_dialog is not None:
            self._watch_dialog.destroy()
            self._watch_dialog = None

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._search_button.configure(state="disabled" if busy else "normal")

    def _fail(self, message: str, parent: tk.Misc | None = None) -> None:
        messagebox.showerror("Flightsleuth", message[:1].upper() + message[1:], parent=parent or self.root)


def _work(request: scan.Request, post) -> None:
    """The search, run off the main thread. It reports through `post` and touches no widgets."""
    try:
        plan = scan.plan(request)
        try:
            scan.check_cap(plan)
        except scan.OverCap as e:
            raise UserError(
                f"that would use {e.live} searches at once, and the limit is {e.limit}. "
                f"Check fewer days earlier and later, or untick the one-way option."
                + (f' Unticking "{CONFIRM_BOX}" saves up to {plan.check_calls} too.' if plan.check_calls else "")
            ) from None
        post(("plan", plan))
        outcome = scan.run(plan, on_search=lambda done, live: post(("progress", (done, live))))
        post(("done", (plan, outcome)))
    except UserError as e:
        post(("error", str(e)))
    except Exception as e:  # nothing is watching this thread, so say it in the window
        post(("error", f"something unexpected went wrong: {e!r}"))


def _lay_out(body: ttk.Frame, intro: ttk.Label, fields, note: str, action: tuple[str, object], close) -> ttk.Button:
    """Fill a small window: what it is about, a label and a box for each field, a grey note, then its button and Close.

    `action` is the button's text and what it does; Enter does the same. Returns the button.
    """
    intro.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
    for row, (text, box) in enumerate(fields, 1):
        ttk.Label(body, text=text).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        box.grid(row=row, column=1, sticky="w", pady=3)
    ttk.Label(body, text=note, foreground="gray40", justify="left").grid(
        row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(6, 0)
    )
    buttons = ttk.Frame(body)
    buttons.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e", pady=(10, 0))
    text, act = action
    button = ttk.Button(buttons, text=text, default="active", command=act)
    button.pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="Close", command=close).pack(side="left")
    body.winfo_toplevel().bind("<Return>", lambda event: act())
    return button


def _watch_work(trip: Query, rules: dict, post) -> None:
    """Adding a watch, run off the main thread because it asks the account what is left. It spends nothing."""
    try:
        post(("watched", watch.add(trip, **rules)))
    except watch.OverBudget as e:
        post(("unwatched", "\n".join([str(e), "", *budget_lines(e.budget)])))
    except UserError as e:
        post(("unwatched", str(e)))
    except Exception as e:  # nothing is watching this thread, so say it in the window
        post(("unwatched", f"something unexpected went wrong: {e!r}"))


def _sharpen() -> None:
    """Ask Windows to draw at the screen's real resolution rather than stretch a blurry image."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (ImportError, AttributeError, OSError):
        pass


def main() -> None:
    _sharpen()
    root = tk.Tk()
    App(root)
    root.mainloop()
