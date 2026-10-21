import tempfile
import unittest
from pathlib import Path

from sky_scanner.lookups import SEED_SQL, LookupCache

DAY = 86400
NOW = 1_800_000_000.0


class LookupCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache" / "callwords.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_empty_and_unseeded(self):
        cache = LookupCache.load(self.path)
        self.assertEqual((cache.entries, cache.seeded), ({}, False))

    def test_found_stays_fresh_for_30_days_and_not_found_for_7(self):
        cache = LookupCache(self.path)
        cache.record("TALON", "found", NOW - 29 * DAY)
        cache.record("BOXER", "not_found", NOW - 6 * DAY)
        cache.record("VIPER", "found", NOW - 31 * DAY)
        cache.record("SHARK", "not_found", NOW - 8 * DAY)
        fresh = {key for key in cache.entries if cache.is_fresh(key, NOW)}
        self.assertEqual(fresh, {"TALON", "BOXER"})
        self.assertFalse(cache.is_fresh("NEVER", NOW))

    def test_record_saves_and_load_reads_back(self):
        cache = LookupCache(self.path)
        cache.seeded = True
        cache.record("TALON", "found", NOW)
        loaded = LookupCache.load(self.path)
        self.assertTrue(loaded.seeded)
        self.assertEqual(loaded.entries, {"TALON": ("found", NOW)})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["callwords.json"])

    def test_seed_merges_and_keeps_the_newest_entry(self):
        cache = LookupCache(self.path)
        cache.record("TALON", "found", NOW)
        count = cache.seed(
            [
                {"key": "TALON", "status": "not_found", "fetched_at": NOW - DAY},
                {"key": "BOXER", "status": "found", "fetched_at": NOW - DAY},
            ]
        )
        self.assertEqual(count, 2)
        self.assertEqual(cache.entries["TALON"], ("found", NOW))
        self.assertTrue(LookupCache.load(self.path).seeded)

    def test_unreadable_file_starts_over_unseeded(self):
        self.path.parent.mkdir()
        self.path.write_text("{broken")
        cache = LookupCache.load(self.path)
        self.assertEqual((cache.entries, cache.seeded), ({}, False))

    def test_seed_queries_read_the_latest_lookup_per_key(self):
        self.assertIn("partition by word", SEED_SQL["callword_lookups"])
        self.assertIn("partition by designator", SEED_SQL["aircraft_type_images"])
        self.assertIn("{dataset}.aircraft_type_images", SEED_SQL["aircraft_type_images"])


if __name__ == "__main__":
    unittest.main()
