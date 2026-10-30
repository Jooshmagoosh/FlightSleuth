"""Local store of raw API responses, so repeating a search costs nothing."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import config


class Cache:
    """`cache.db`, open. Use it in a `with` block, or close it when done."""

    def __init__(self, path: Path | None = None):
        self.path = path or config.cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL)"
        )

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def lookup(self, key: str, max_age_s: float, *, now: float | None = None) -> tuple[dict, float] | None:
        """The saved reply and when it was fetched (seconds since the epoch), if it is fresh enough."""
        now = time.time() if now is None else now
        row = self._db.execute(
            "SELECT body, fetched_at FROM responses WHERE key = ? AND fetched_at >= ?", (key, now - max_age_s)
        ).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def put(self, key: str, body: dict, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO responses (key, fetched_at, body) VALUES (?, ?, ?)",
                (key, now, json.dumps(body)),
            )

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM responses").fetchone()[0]

    def prune(self, max_age_s: float, *, now: float | None = None) -> int:
        """Drop replies too old to be reused, and hand the space back. Returns how many went."""
        now = time.time() if now is None else now
        return self._delete("WHERE fetched_at < ?", (now - max_age_s,))

    def clear(self) -> int:
        return self._delete()

    def _delete(self, where: str = "", values: tuple = ()) -> int:
        with self._db:
            removed = self._db.execute(f"DELETE FROM responses {where}", values).rowcount
        if removed:
            # Deleting rows only marks their pages free, and one reply is 120 to 220 KB.
            self._db.execute("VACUUM")
        return removed

    def close(self) -> None:
        self._db.close()
