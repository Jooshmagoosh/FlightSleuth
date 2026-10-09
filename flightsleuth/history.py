"""The durable record of what was fetched (`history.db`). Unlike the cache, it is never cleared."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Offer

TABLES = ("price_points", "provider_calls")

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

    def price_points(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM price_points ORDER BY id")]

    def calls(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM provider_calls ORDER BY id")]

    def close(self) -> None:
        self._db.close()
