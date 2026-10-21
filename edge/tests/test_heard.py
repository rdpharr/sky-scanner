import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sky_scanner.heard import Heard

DAY = 86400


def snapshot(address="ABC123", first=100.0, last=110.0, callsign=None, address_type="icao"):
    fields = {"callsign": {"value": callsign, "timestamp": last}} if callsign is not None else {}
    return {
        "address_type": address_type,
        "address": address,
        "first_received_at": first,
        "last_received_at": last,
        "fields": fields,
    }


class HeardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "heard" / "heard.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_empty_and_unseeded(self):
        heard = Heard.load(self.path)
        self.assertEqual(heard.aircraft, {})
        self.assertFalse(heard.seeded)

    def test_update_tracks_first_and_last_seen_and_callsigns(self):
        heard = Heard(self.path)
        heard.update([snapshot(first=100, last=110, callsign="rch123 ")])
        heard.update([snapshot(first=50, last=60), snapshot(first=200, last=210, callsign="RCH123")])
        entry = heard.aircraft[("icao", "ABC123")]
        self.assertEqual((entry.first_seen, entry.last_seen), (50, 210))
        self.assertEqual(entry.callsigns, {"RCH123": 210})

    def test_addresses_are_scoped_by_type(self):
        heard = Heard(self.path)
        heard.update([snapshot(address="abc123"), snapshot(address="ABC123", address_type="tisb_other")])
        self.assertEqual(sorted(heard.aircraft), [("icao", "ABC123"), ("tisb_other", "ABC123")])

    def test_save_and_load_round_trip(self):
        heard = Heard(self.path)
        heard.update([snapshot(callsign="TALON95")])
        heard.seeded = True
        heard.save()
        loaded = Heard.load(self.path)
        self.assertTrue(loaded.seeded)
        self.assertEqual(loaded.aircraft, heard.aircraft)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_rejects_an_unknown_format_version(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"version": 99, "aircraft": []}))
        with self.assertRaises(ValueError):
            Heard.load(self.path)

    def test_prune_forgets_old_callsigns_but_keeps_the_aircraft(self):
        heard = Heard(self.path, callsign_days=30)
        now = 100 * DAY
        heard.update([snapshot(last=now - 31 * DAY, callsign="OLD1"), snapshot(last=now - DAY, callsign="NEW1")])
        heard.update([snapshot(address="DEF456", last=now - 40 * DAY, callsign="GONE1")])
        heard.prune(now)
        self.assertEqual(heard.aircraft[("icao", "ABC123")].callsigns, {"NEW1": now - DAY})
        self.assertEqual(heard.aircraft[("icao", "DEF456")].callsigns, {})

    def test_heard_since(self):
        heard = Heard(self.path)
        heard.update([snapshot(address="AAAAAA", last=10), snapshot(address="BBBBBB", last=20)])
        self.assertEqual(heard.heard_since(15), [("icao", "BBBBBB")])

    def test_seed_merges_with_what_was_heard_meanwhile(self):
        heard = Heard(self.path)
        heard.update([snapshot(first=500, last=510, callsign="NEW1")])
        rows = [
            {
                "address_type": "icao",
                "address": "abc123",
                "first_seen": 100.0,
                "last_seen": 400.0,
                "callsigns": [{"callsign": "OLD1", "last_used": 300.0}],
            },
            {"address_type": "icao", "address": "DEF456", "first_seen": 1.0, "last_seen": 2.0, "callsigns": None},
        ]
        self.assertEqual(heard.seed(rows), 2)
        entry = heard.aircraft[("icao", "ABC123")]
        self.assertEqual((entry.first_seen, entry.last_seen), (100, 510))
        self.assertEqual(entry.callsigns, {"OLD1": 300, "NEW1": 510})
        self.assertTrue(heard.seeded)

    def test_seed_params_cover_the_callsign_window(self):
        params = Heard(self.path, callsign_days=30).seed_params(now=31 * DAY)
        self.assertEqual(params["callsigns_since"], datetime(1970, 1, 2, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
