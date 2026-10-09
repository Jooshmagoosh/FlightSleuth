"""The durable record of what was fetched (`history.db`). Unlike the cache, it is never cleared."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Offer, Query

TABLES = ("price_points", "provider_calls", "google_price_history", "bakeoff_fares", "bakeoff_lookups", "site_prices")

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
""" + "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
"""
    for table in TABLES
    for verb in ("UPDATE", "DELETE")
)


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class History:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def add_price_point(self, offer: Offer) -> None:
        """Record the fare a live reply gave. `offer` must carry its source and fetch time."""
        query = offer.query
        with self._db:
            self._db.execute(
                "INSERT INTO price_points (origin, destination, depart_date, return_date, adults, cabin, stops,"
                " price, currency, source, fetched_at, itinerary_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    query.origin,
                    query.destination,
                    query.depart_date.isoformat(),
                    query.return_date.isoformat() if query.return_date else None,
                    query.adults,
                    query.cabin,
                    query.stops,
                    offer.price,
                    query.currency,
                    offer.source,
                    _utc(offer.fetched_at),
                    offer.itinerary_key,
                ),
            )

    def add_call(
        self, provider: str, endpoint: str, *, live: bool, cost: float, latency_ms: int | None, status: str
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO provider_calls (provider, endpoint, live, cost, latency_ms, status, called_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (provider, endpoint, int(live), cost, latency_ms, status, _utc(datetime.now(timezone.utc))),
            )

    def add_price_history(self, query: Query, source: str, points, recorded_at: datetime) -> int:
        """Keep Google's daily lowest prices for one search. A day already held is left as it is."""
        rows = [
            (
                query.origin,
                query.destination,
                query.depart_date.isoformat(),
                query.return_date.isoformat() if query.return_date else "",
                query.adults,
                query.cabin,
                query.stops,
                query.currency,
                query.market,
                query.bags,
                when.astimezone(timezone.utc).date().isoformat(),
                price,
                source,
                _utc(recorded_at),
            )
            for when, price in points
        ]
        with self._db:
            return self._db.executemany(
                "INSERT OR IGNORE INTO google_price_history (origin, destination, depart_date, return_date, adults,"
                " cabin, stops, currency, market, bags, day, price, source, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            ).rowcount

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
        with self._db:
            self._db.execute(
                "INSERT INTO bakeoff_fares (run, route, depart_date, return_date, measure, price, currency,"
                " same_flights, how, detail, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run,
                    route,
                    trip.depart_date.isoformat(),
                    trip.return_date.isoformat() if trip.return_date else None,
                    measure,
                    price,
                    trip.currency,
                    None if same_flights is None else int(same_flights),
                    how,
                    detail,
                    _utc(fetched_at),
                ),
            )

    def add_bakeoff_lookup(
        self, run: str, route: str, offer: Offer, *, cheapest: bool, found: bool, in_unsorted: bool | None = None
    ) -> None:
        query = offer.query
        with self._db:
            self._db.execute(
                "INSERT INTO bakeoff_lookups (run, route, depart_date, return_date, price, itinerary_key, cheapest,"
                " found, in_unsorted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run,
                    route,
                    query.depart_date.isoformat(),
                    query.return_date.isoformat() if query.return_date else None,
                    offer.price,
                    offer.outbound_key,
                    int(cheapest),
                    int(found),
                    None if in_unsorted is None else int(in_unsorted),
                ),
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
        with self._db:
            self._db.execute(
                "INSERT INTO site_prices (origin, destination, depart_date, return_date, adults, cabin, stops,"
                " currency, market, bags, site, price, seller, same_flights, held, our_price, our_source,"
                " our_fetched_at, our_confirmed, itinerary_key, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trip.origin,
                    trip.destination,
                    trip.depart_date.isoformat(),
                    trip.return_date.isoformat() if trip.return_date else None,
                    trip.adults,
                    trip.cabin,
                    trip.stops,
                    trip.currency,
                    trip.market,
                    trip.bags,
                    site,
                    price,
                    seller,
                    None if same_flights is None else int(same_flights),
                    None if held is None else int(held),
                    ours.price,
                    ours.source,
                    _utc(ours.fetched_at) if ours.fetched_at else None,
                    int(confirmed),
                    ours.itinerary_key,
                    _utc(recorded_at),
                ),
            )

    def price_points(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM price_points ORDER BY id")]

    def calls(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM provider_calls ORDER BY id")]

    def price_history(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM google_price_history ORDER BY id")]

    def bakeoff_fares(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM bakeoff_fares ORDER BY id")]

    def bakeoff_lookups(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM bakeoff_lookups ORDER BY id")]

    def site_prices(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM site_prices ORDER BY id")]

    def close(self) -> None:
        self._db.close()
