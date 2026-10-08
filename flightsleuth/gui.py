"""A small window over the same search the command line runs."""
from __future__ import annotations

import calendar
import queue
import threading
import tkinter as tk
import tkinter.font
import webbrowser
from datetime import date, timedelta
from tkinter import messagebox, simpledialog, ttk

from . import config, scan
from .errors import UserError
from .render import Summary, summarise

STOPS = {"Any number of stops": "any", "Nonstop only": "nonstop", "1 stop or fewer": "1", "2 stops or fewer": "2"}
CABINS = {"Economy": "economy", "Premium economy": "premium", "Business": "business", "First": "first"}
MAX_FLEX = 7
MAX_ADULTS = 9
KEY_PROMPT = "Paste your SerpApi key here.\nYou can copy it from serpapi.com/manage-api-key"
BEST_ROW_COLOUR = "#d9f2d9"


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
        self._font = tkinter.font.nametofont("TkDefaultFont")
        root.title("Flightsleuth")
        self._build_form()
        self._build_results()
        root.bind("<Return>", lambda event: self.search())
        root.bind("<Destroy>", self._closed)
        root.update_idletasks()
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
        # A fixed starting size, so the window does not jump wider when the first results arrive.
        width = max(root.winfo_reqwidth(), self._font.measure("0") * 100)
        root.geometry(f"{width}x{root.winfo_reqheight()}")
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
        ttk.Spinbox(flexible, from_=0, to=MAX_FLEX, textvariable=self.flex, width=3).pack(side="left", padx=(0, 6))
        label(flexible, "days earlier and later")

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
        ttk.Spinbox(options, from_=1, to=MAX_ADULTS, textvariable=self.adults, width=3).pack(side="left")

        self.split = tk.BooleanVar(value=False)
        self._split_box = ttk.Checkbutton(
            row(), text="Also check whether two one-way tickets are cheaper (uses more searches)", variable=self.split
        )
        self._split_box.pack(side="left")

        self.status = tk.StringVar()
        actions = row()
        self._search_button = ttk.Button(actions, text="Search", command=self.search)
        self._search_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="SerpApi key...", command=self.ask_key).pack(side="left", padx=(0, 12))
        ttk.Label(actions, textvariable=self.status).pack(side="left")

    def _build_results(self) -> None:
        results = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        results.pack(fill="both", expand=True)

        bold = self._font.copy()
        bold.configure(weight="bold", size=self._font.cget("size") + 2)
        self.headline = tk.StringVar(value="Fill in the trip above and press Search.")
        self.detail = tk.StringVar()
        ttk.Label(results, textvariable=self.headline, font=bold).pack(anchor="w")
        ttk.Label(results, textvariable=self.detail, justify="left").pack(anchor="w")
        self._links = ttk.Frame(results)
        self._links.pack(anchor="w", pady=(4, 8))

        ttk.Style().configure("Treeview", rowheight=self._font.metrics("linespace") + 6)
        ttk.Label(results, text="Cheapest price for each date").pack(anchor="w")
        self.dates = self._table(results, height=5, expand=False)
        self.flights_title = tk.StringVar(value="Cheapest flights")
        ttk.Label(results, textvariable=self.flights_title).pack(anchor="w")
        self.flights = self._table(results, height=8, expand=True)

    def _table(self, parent: tk.Misc, *, height: int, expand: bool) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=expand, pady=(2, 8))
        tree = ttk.Treeview(frame, show="headings", height=height, selectmode="browse")
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.tag_configure("best", background=BEST_ROW_COLOUR)
        bar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return tree

    def _fill(self, tree: ttk.Treeview, headers: list[str], rows: list[list[str]], best: int | None = None) -> None:
        tree.delete(*tree.get_children())
        columns = [f"c{index}" for index in range(len(headers))]
        tree["columns"] = columns
        for index, (column, header) in enumerate(zip(columns, headers)):
            widest = max([header, *(row[index] for row in rows)], key=len)
            tree.heading(column, text=header, anchor="w")
            tree.column(column, width=self._font.measure(widest + "mmm"), anchor="w", stretch=column == columns[-1])
        for index, row in enumerate(rows):
            tree.insert("", "end", values=row, tags=("best",) if index == best else ())

    def _trip_kind_changed(self) -> None:
        round_trip = self.round_trip.get()
        self.back.enable(round_trip)
        self._split_box.configure(state="normal" if round_trip else "disabled")

    def _read_form(self) -> scan.Request:
        if not self.origin.get().strip() or not self.destination.get().strip():
            raise UserError("fill in both the From and To airports")
        round_trip = self.round_trip.get()
        return scan.Request(
            origin=scan.airports(self.origin.get()),
            destination=scan.airports(self.destination.get()),
            depart=self.depart.get(),
            return_date=self.back.get() if round_trip else None,
            flex=_whole(self.flex.get(), "the number of days earlier and later", 0, MAX_FLEX),
            split=round_trip and self.split.get(),
            stops=STOPS[self.stops.get()],
            cabin=CABINS[self.cabin.get()],
            adults=_whole(self.adults.get(), "the number of adults", 1, MAX_ADULTS),
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
        self.status.set(f"Searching... 0 of {plan.live}" if plan.live else "Using saved results...")

    def _on_progress(self, progress: tuple[int, int]) -> None:
        done, live = progress
        self.status.set(f"Searching... {done} of {live}")

    def _on_done(self, finished: tuple[scan.Plan, scan.Outcome]) -> None:
        plan, outcome = finished
        self._set_busy(False)
        self.show(summarise(outcome.trips, outcome.results, split=outcome.split))
        status = f"Done. Searches used: {plan.live}. Reused from earlier: {plan.saved}."
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

        for old in self._links.winfo_children():
            old.destroy()
        for text, url in summary.links:
            # The address comes from the API's reply, so only ever hand the browser a web page.
            if url.startswith("https://"):
                ttk.Button(self._links, text=text, command=lambda url=url: webbrowser.open(url)).pack(
                    side="left", padx=(0, 8)
                )

        best = summary.rows.index(summary.best) if summary.best else None
        self._fill(self.dates, summary.date_headers, [summary.date_cells(row) for row in summary.rows], best)
        note = " (the price is for both ways; the flight shown is the one going out)" if summary.round_trip else ""
        self.flights_title.set(f"Cheapest flights{note}")
        self._fill(self.flights, summary.offer_headers, [summary.offer_cells(offer) for offer in summary.offers])

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._search_button.configure(state="disabled" if busy else "normal")

    def _fail(self, message: str) -> None:
        messagebox.showerror("Flightsleuth", message[:1].upper() + message[1:], parent=self.root)


def _work(request: scan.Request, post) -> None:
    """The search, run off the main thread. It reports through `post` and touches no widgets."""
    try:
        plan = scan.plan(request)
        if plan.live > scan.DEFAULT_MAX_SEARCHES:
            raise UserError(
                f"that would use {plan.live} searches at once, and the limit is {scan.DEFAULT_MAX_SEARCHES}. "
                f"Check fewer days earlier and later, or untick the one-way option."
            )
        post(("plan", plan))
        outcome = scan.run(plan, on_search=lambda done, live: post(("progress", (done, live))))
        post(("done", (plan, outcome)))
    except UserError as e:
        post(("error", str(e)))
    except Exception as e:  # nothing is watching this thread, so say it in the window
        post(("error", f"something unexpected went wrong: {e!r}"))


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
