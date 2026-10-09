import tempfile
import unittest
from pathlib import Path

from flightsleuth.cache import Cache


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self.tmp.name) / "nested" / "cache.db")

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def test_round_trips_a_response(self):
        self.cache.put("k", {"price": 438}, now=1000)
        self.assertEqual(self.cache.get("k", max_age_s=60, now=1030), {"price": 438})

    def test_lookup_also_says_when_the_reply_was_fetched(self):
        self.cache.put("k", {"price": 438}, now=1000)
        self.assertEqual(self.cache.lookup("k", max_age_s=60, now=1030), ({"price": 438}, 1000))
        self.assertIsNone(self.cache.lookup("k", max_age_s=60, now=1061))

    def test_expired_entries_are_misses(self):
        self.cache.put("k", {"price": 438}, now=1000)
        self.assertIsNone(self.cache.get("k", max_age_s=60, now=1061))

    def test_put_replaces_and_refreshes(self):
        self.cache.put("k", {"price": 438}, now=1000)
        self.cache.put("k", {"price": 401}, now=2000)
        self.assertEqual(self.cache.get("k", max_age_s=60, now=2030), {"price": 401})
        self.assertEqual(self.cache.count(), 1)

    def test_clear_reports_how_many_went(self):
        self.cache.put("a", {}, now=1000)
        self.cache.put("b", {}, now=1000)
        self.assertEqual(self.cache.clear(), 2)
        self.assertEqual(self.cache.count(), 0)


if __name__ == "__main__":
    unittest.main()
