"""The durable record of what was fetched (`history.db`). Unlike the cache, it is never cleared."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from . import config
from .models import Offer, Query

TABLES = (
    "price_points",
    "provider_calls",
    "google_price_history",
    "bakeoff_fares",
    "bakeoff_lookups",
    "site_prices",
    "watches",
    "watch_events",
    "watch_checks",
    "watch_alerts",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_points (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,          -- airports as searched, e.g. "JFK,EWR"
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,              -- NULL for a one-way
    adults INTEGER NOT NULL,
    cabin TEXT NOT NULL,
    stops TEXT NOT NULL,
    price REAL NOT NULL,           -- the cheapest fare in the reply
    currency TEXT NOT NULL,
    source TEXT NOT NULL,          -- provider name
    fetched_at TEXT NOT NULL,      -- UTC, when the source was really asked
    itinerary_key TEXT             -- the flights that fare was for
);
CREATE TABLE IF NOT EXISTS provider_calls (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    live INTEGER NOT NULL,         -- 1 sent to the provider, 0 answered from the cache
    cost REAL NOT NULL,            -- in the provider's own credits
    latency_ms INTEGER,            -- NULL when answered from the cache
    status TEXT NOT NULL,          -- "ok", or the kind of error
    called_at TEXT NOT NULL        -- UTC
);
-- Google's own record of the lowest price, a point a day. Kept apart from price_points, which
-- holds only what flightsleuth fetched itself.
CREATE TABLE IF NOT EXISTS google_price_history (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,     -- '' for a one-way: NULLs would never collide in the key below
    adults INTEGER NOT NULL,
    cabin TEXT NOT NULL,
    stops TEXT NOT NULL,
    currency TEXT NOT NULL,
    market TEXT NOT NULL,          -- '' for the source's default
    bags INTEGER NOT NULL,
    day TEXT NOT NULL,             -- Google's date for the point
    price REAL NOT NULL,
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,     -- UTC, when the reply carrying it was fetched
    UNIQUE (origin, destination, depart_date, return_date, adults, cabin, stops, currency, market, bags, day)
);
-- One fare per route and measure for each bake-off run.
CREATE TABLE IF NOT EXISTS bakeoff_fares (
    id INTEGER PRIMARY KEY,
    run TEXT NOT NULL,             -- UTC, when the bake-off started
    route TEXT NOT NULL,           -- such as "MCO,SFB to JFK,EWR,LGA"
    depart_date TEXT NOT NULL,
    return_date TEXT,
    measure TEXT NOT NULL,         -- "baseline", "split", "market:pl", or a source's or site's name
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    same_flights INTEGER,          -- 1 the baseline's flights, 0 a different itinerary, NULL not known
    how TEXT NOT NULL,             -- "live", "saved" or "typed"
    detail TEXT NOT NULL,          -- the flights, the seller or a note
    fetched_at TEXT NOT NULL       -- UTC
);
-- Whether the return flights of a listed fare came back when they were asked for.
CREATE TABLE IF NOT EXISTS bakeoff_lookups (
    id INTEGER PRIMARY KEY,
    run TEXT NOT NULL,
    route TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    price REAL NOT NULL,
    itinerary_key TEXT NOT NULL,
    cheapest INTEGER NOT NULL,     -- 1 it was the cheapest fare listed for those dates
    found INTEGER NOT NULL,        -- 1 return flights came back, 0 the reply was empty
    in_unsorted INTEGER            -- after a miss: 1 Google's own order still listed it, 0 it did not, NULL not checked
);
-- A price somebody read on another site and typed in, beside the fare flightsleuth had shown them.
-- Nothing here was fetched by a program, so none of it goes into price_points.
CREATE TABLE IF NOT EXISTS site_prices (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,          -- the search, as in price_points
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    adults INTEGER NOT NULL,
    cabin TEXT NOT NULL,
    stops TEXT NOT NULL,
    currency TEXT NOT NULL,
    market TEXT NOT NULL,          -- '' for the source's default
    bags INTEGER NOT NULL,
    site TEXT NOT NULL,            -- such as "Momondo"
    price REAL NOT NULL,           -- as typed
    seller TEXT NOT NULL,          -- who the site said sells it at that price, as typed; '' when not given
    same_flights INTEGER,          -- 1 the flights of our fare, 0 a different itinerary, NULL not known
    held INTEGER,                  -- 1 the seller's payment page showed that price, 0 it did not, NULL not followed that far
    our_price REAL NOT NULL,       -- the fare it is set against
    our_source TEXT NOT NULL,      -- the provider that fare came from
    our_fetched_at TEXT,           -- UTC, when that fare was fetched
    our_confirmed INTEGER NOT NULL, -- 1 that fare's return flights had been fetched, 0 it was only listed
    itinerary_key TEXT NOT NULL,   -- that fare's flights
    recorded_at TEXT NOT NULL      -- UTC, when the price was typed
);
-- A fare somebody asked to be told about. A watch is never changed: what happens to it afterwards
-- is in the three tables below, and where it stands is worked out from them.
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,          -- the search, as in price_points
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,              -- NULL for a one-way
    adults INTEGER NOT NULL,
    cabin TEXT NOT NULL,
    stops TEXT NOT NULL,
    currency TEXT NOT NULL,
    below REAL,                    -- alert under this price; NULL when no price was set
    drop_percent REAL NOT NULL,    -- alert this far under the recent median; 0 turns that rule off
    typical INTEGER NOT NULL,      -- 1 alert under Google's typical range
    every_days INTEGER NOT NULL,   -- days from one check to the next
    created_at TEXT NOT NULL,      -- UTC
    created_day TEXT NOT NULL      -- that moment's date on the owner's calendar: the day it first falls due
);
-- What happened to a watch besides its checks: 'removed' by the owner, 'retired' once its departure
-- date had passed, or 'outage', when the owner was told it had gone unchecked.
CREATE TABLE IF NOT EXISTS watch_events (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL               -- UTC
);
-- Every time a watch was looked at, whether or not the look worked.
CREATE TABLE IF NOT EXISTS watch_checks (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL,
    checked_at TEXT NOT NULL,      -- UTC
    day TEXT NOT NULL,             -- that moment's date on the owner's calendar; the next check counts from it
    finished INTEGER NOT NULL,     -- 1 the fare was priced; 0 the check failed or was skipped, and the watch stays due
    outcome TEXT NOT NULL,         -- such as 'quiet', 'alerted', 'repeat', 'no return flights', 'failed'
    detail TEXT NOT NULL,          -- one line: what was found, or what went wrong
    price REAL,                    -- the cheapest fare listed; NULL when none was
    confirmed_price REAL,          -- that fare once its return flights were fetched; NULL when they were not
    itinerary_key TEXT,            -- the flights of the fare the prices are for
    median REAL,                   -- the median of Google's daily lows that the price was set against
    typical_low REAL,              -- Google's typical range for those dates
    typical_high REAL,
    reasons TEXT NOT NULL,         -- the rules that fired, comma-separated; '' when none did
    no_returns TEXT NOT NULL,      -- JSON: [outbound flights, price] of each fare with no return flights
    searches INTEGER NOT NULL,     -- live searches this check spent
    fetched_at TEXT                -- UTC, when the price was fetched
);
-- Every alert composed for a watch and every try at sending it. Only a row with sent = 1 reached ntfy.
CREATE TABLE IF NOT EXISTS watch_alerts (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL,
    check_id INTEGER NOT NULL,     -- the check that found the price
    price REAL NOT NULL,           -- the confirmed fare the alert is about
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    click TEXT NOT NULL,           -- the address the alert opens
    sent INTEGER NOT NULL,         -- 1 ntfy accepted it, 0 it did not
    error TEXT NOT NULL,           -- why not; '' when it was sent
    fetched_at TEXT NOT NULL,      -- UTC, when the price was fetched
    at TEXT NOT NULL               -- UTC, when this try was made
);
""" + "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
"""
    for table in TABLES
    for verb in ("UPDATE", "DELETE")
)


def _utc(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds") if moment else None


def _insert(table: str, row: dict, verb: str = "INSERT") -> str:
    return f"{verb} INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})"


def _search(trip: Query) -> dict:
    """The columns a row keeps about the search it belongs to."""
    return {
        "origin": trip.origin,
        "destination": trip.destination,
        "depart_date": trip.depart_date.isoformat(),
        "return_date": trip.return_date.isoformat() if trip.return_date else None,
        "adults": trip.adults,
        "cabin": trip.cabin,
        "stops": trip.stops,
        "currency": trip.currency,
    }


class History:
    """`history.db`, open. Use it in a `with` block, or close it when done.

    Every table is written through `_add`, which takes a row as a dict of column names to
    values. A bool is stored as 1 or 0 and None as NULL, as sqlite3 does by itself.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or config.history_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def __enter__(self) -> History:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def _add(self, table: str, row: dict) -> int:
        """Append one row. Returns its id."""
        with self._db:
            return self._db.execute(_insert(table, row), tuple(row.values())).lastrowid

    def _add_new(self, table: str, rows: list[dict]) -> int:
        """Append the rows whose unique key the table does not hold yet. Returns how many went in."""
        if not rows:
            return 0
        with self._db:
            return self._db.executemany(
                _insert(table, rows[0], "INSERT OR IGNORE"), [tuple(row.values()) for row in rows]
            ).rowcount

    def rows(self, table: str) -> list[dict]:
        """Every row of one table, oldest first."""
        if table not in TABLES:
            raise ValueError(f"history.db has no table called {table!r}")
        return [dict(row) for row in self._db.execute(f"SELECT * FROM {table} ORDER BY id")]

    def spent(self) -> float:
        """Everything the live calls so far have cost, in the sources' own credits."""
        return self._db.execute("SELECT COALESCE(SUM(cost), 0) FROM provider_calls WHERE live = 1").fetchone()[0]

    def add_price_point(self, offer: Offer) -> None:
        """Record the fare a live reply gave. `offer` must carry its source and fetch time."""
        self._add(
            "price_points",
            {
                **_search(offer.query),
                "price": offer.price,
                "source": offer.source,
                "fetched_at": _utc(offer.fetched_at),
                "itinerary_key": offer.itinerary_key,
            },
        )

    def add_call(
        self, provider: str, endpoint: str, *, live: bool, cost: float, latency_ms: int | None, status: str
    ) -> None:
        self._add(
            "provider_calls",
            {
                "provider": provider,
                "endpoint": endpoint,
                "live": live,
                "cost": cost,
                "latency_ms": latency_ms,
                "status": status,
                "called_at": _utc(datetime.now(timezone.utc)),
            },
        )

    def add_price_history(self, query: Query, source: str, points, recorded_at: datetime) -> int:
        """Keep Google's daily lowest prices for one search. A day already held is left as it is."""
        search = _search(query)
        # '' and not NULL for a one-way: NULLs never collide, so the unique key would not hold.
        search.update(return_date=search["return_date"] or "", market=query.market, bags=query.bags)
        return self._add_new(
            "google_price_history",
            [
                {
                    **search,
                    "day": when.astimezone(timezone.utc).date().isoformat(),
                    "price": price,
                    "source": source,
                    "recorded_at": _utc(recorded_at),
                }
                for when, price in points
            ],
        )

    def add_bakeoff_fare(
        self,
        run: str,
        route: str,
        trip: Query,
        measure: str,
        price: float,
        *,
        same_flights: bool | None,
        how: str,
        detail: str,
        fetched_at: datetime,
    ) -> None:
        search = _search(trip)
        self._add(
            "bakeoff_fares",
            {
                "run": run,
                "route": route,
                "depart_date": search["depart_date"],
                "return_date": search["return_date"],
                "measure": measure,
                "price": price,
                "currency": trip.currency,
                "same_flights": same_flights,
                "how": how,
                "detail": detail,
                "fetched_at": _utc(fetched_at),
            },
        )

    def add_bakeoff_lookup(
        self, run: str, route: str, offer: Offer, *, cheapest: bool, found: bool, in_unsorted: bool | None = None
    ) -> None:
        search = _search(offer.query)
        self._add(
            "bakeoff_lookups",
            {
                "run": run,
                "route": route,
                "depart_date": search["depart_date"],
                "return_date": search["return_date"],
                "price": offer.price,
                "itinerary_key": offer.outbound_key,
                "cheapest": cheapest,
                "found": found,
                "in_unsorted": in_unsorted,
            },
        )

    def add_site_price(
        self,
        trip: Query,
        site: str,
        price: float,
        *,
        seller: str,
        same_flights: bool | None,
        held: bool | None,
        ours: Offer,
        confirmed: bool,
        recorded_at: datetime,
    ) -> None:
        """Keep a price read on another site by eye, with the fare of ours it was checked against."""
        self._add(
            "site_prices",
            {
                **_search(trip),
                "market": trip.market,
                "bags": trip.bags,
                "site": site,
                "price": price,
                "seller": seller,
                "same_flights": same_flights,
                "held": held,
                "our_price": ours.price,
                "our_source": ours.source,
                "our_fetched_at": _utc(ours.fetched_at),
                "our_confirmed": confirmed,
                "itinerary_key": ours.itinerary_key,
                "recorded_at": _utc(recorded_at),
            },
        )

    def add_watch(
        self,
        trip: Query,
        *,
        below: float | None,
        drop_percent: float,
        typical: bool,
        every_days: int,
        created_at: datetime,
        created_day: date,
    ) -> int:
        """Keep a new watch. Returns its number, which is how the owner names it afterwards."""
        return self._add(
            "watches",
            {
                **_search(trip),
                "below": below,
                "drop_percent": drop_percent,
                "typical": typical,
                "every_days": every_days,
                "created_at": _utc(created_at),
                "created_day": created_day.isoformat(),
            },
        )

    def add_watch_event(self, watch_id: int, event: str, detail: str, at: datetime) -> None:
        self._add("watch_events", {"watch_id": watch_id, "event": event, "detail": detail, "at": _utc(at)})

    def add_watch_check(
        self,
        watch_id: int,
        *,
        checked_at: datetime,
        day: date,
        finished: bool,
        outcome: str,
        detail: str,
        price: float | None = None,
        confirmed_price: float | None = None,
        itinerary_key: str | None = None,
        median: float | None = None,
        typical_range: tuple[float, float] | None = None,
        reasons: tuple[str, ...] = (),
        no_returns: tuple[tuple[str, float], ...] = (),
        searches: int = 0,
        fetched_at: datetime | None = None,
    ) -> int:
        low, high = typical_range or (None, None)
        return self._add(
            "watch_checks",
            {
                "watch_id": watch_id,
                "checked_at": _utc(checked_at),
                "day": day.isoformat(),
                "finished": finished,
                "outcome": outcome,
                "detail": detail,
                "price": price,
                "confirmed_price": confirmed_price,
                "itinerary_key": itinerary_key,
                "median": median,
                "typical_low": low,
                "typical_high": high,
                "reasons": ",".join(reasons),
                "no_returns": json.dumps([list(pair) for pair in no_returns]),
                "searches": searches,
                "fetched_at": _utc(fetched_at),
            },
        )

    def add_watch_alert(
        self,
        watch_id: int,
        check_id: int,
        *,
        price: float,
        title: str,
        message: str,
        click: str,
        sent: bool,
        error: str,
        fetched_at: datetime,
        at: datetime,
    ) -> None:
        self._add(
            "watch_alerts",
            {
                "watch_id": watch_id,
                "check_id": check_id,
                "price": price,
                "title": title,
                "message": message,
                "click": click,
                "sent": sent,
                "error": error,
                "fetched_at": _utc(fetched_at),
                "at": _utc(at),
            },
        )
