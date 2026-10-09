"""Local store of raw API responses, so repeating a search costs nothing."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Cache:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL)"
        )

    def get(self, key: str, max_age_s: float, *, now: float | None = None) -> dict | None:
        found = self.lookup(key, max_age_s, now=now)
        return found[0] if found else None

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
        with self._db:
            removed = self._db.execute("DELETE FROM responses WHERE fetched_at < ?", (now - max_age_s,)).rowcount
        if removed:
            self._shrink()
        return removed

    def clear(self) -> int:
        with self._db:
            removed = self._db.execute("DELETE FROM responses").rowcount
        if removed:
            self._shrink()
        return removed

    def _shrink(self) -> None:
        # Deleting rows only marks their pages free, and one reply is 120 to 220 KB.
        self._db.execute("VACUUM")

    def close(self) -> None:
        self._db.close()
