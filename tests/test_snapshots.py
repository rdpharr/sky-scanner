import unittest

from sky_scanner.snapshots import SnapshotAccumulator


def obs(t, **kwargs):
    return {
        "received_at": t,
        "receiver_id": "rx1",
        "address": "ABC123",
        "address_type": "icao",
        "frequency_hz": 1_090_000_000,
        "fields": {},
        **kwargs,
    }


class SnapshotTests(unittest.TestCase):
    def test_bucket_boundaries_and_combines_latest_fields(self):
        acc = SnapshotAccumulator()
        acc.add_observation(obs(14.9, fields={"altitude_ft": 10000}))
        acc.add_observation(obs(15, fields={"altitude_ft": 11000, "callsign": "ABC"}))
        result = acc.flush_ready(30)
        self.assertEqual([r["bucket_start"] for r in result], [0, 15])
        self.assertEqual(result[0]["fields"]["altitude_ft"]["value"], 10000)
        self.assertEqual(result[1]["fields"]["altitude_ft"]["value"], 11000)
        self.assertEqual(result[1]["message_count"], 1)

    def test_field_carry_keeps_timestamp_source_then_expires(self):
        acc = SnapshotAccumulator(carry_seconds=30)
        acc.add_observation(obs(0, source="decoder", fields={"callsign": "A1"}))
        acc.add_observation(obs(15, fields={"altitude_ft": 9000}))
        acc.add_observation(obs(30, fields={"speed_kt": 210}))
        acc.add_observation(obs(45, fields={"track_deg": 90}))
        rows = acc.flush_all()
        self.assertEqual(rows[1]["fields"]["callsign"], {
            "value": "A1", "timestamp": 0.0, "source": "decoder"
        })
        self.assertNotIn("callsign", rows[3]["fields"])

    def test_frequency_address_type_receiver_are_independent(self):
        acc = SnapshotAccumulator()
        acc.add_observation(obs(1, fields={"callsign": "one"}))
        acc.add_observation(obs(2, frequency_hz=978_000_000, fields={"callsign": "two"}))
        acc.add_observation(obs(3, address_type="uat", fields={"callsign": "three"}))
        acc.add_observation(obs(4, receiver_id="rx2", fields={"callsign": "four"}))
        rows = acc.flush_all()
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["frequency_hz"] for r in rows}, {1_090_000_000, 978_000_000})

    def test_empty_intervals_are_not_emitted_and_flush_is_ordered(self):
        acc = SnapshotAccumulator()
        acc.add_observation(obs(31, address="B", fields={"x": 1}))
        acc.add_observation(obs(1, address="C", fields={"x": 0}))
        acc.add_observation(obs(2, address="A", fields={"x": 2}))
        acc.add_observation(obs(16, address="D", fields={"x": 3}))
        rows = acc.flush_all()
        self.assertEqual([r["bucket_start"] for r in rows], [0, 0, 15, 30])
        self.assertEqual([r["address"] for r in rows], ["A", "C", "D", "B"])
        self.assertEqual(acc.flush_all(), [])

    def test_ready_flush_keeps_partial_bucket_until_complete(self):
        acc = SnapshotAccumulator()
        acc.add_observation(obs(17, fields={"x": 1}))
        self.assertEqual(acc.flush_ready(29.99), [])
        self.assertEqual(acc.flush_ready(30)[0]["bucket_start"], 15)

    def test_field_timestamp_cannot_be_future_or_nonfinite(self):
        acc = SnapshotAccumulator()
        for timestamp in (2, float("inf"), float("nan")):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                acc.add_observation(obs(1, fields={"x": {
                    "value": 1, "timestamp": timestamp, "source": "decoder"
                }}))

    def test_expired_identity_carry_is_cleaned_and_late_data_stays_rejected(self):
        acc = SnapshotAccumulator(carry_seconds=15)
        acc.add_observation(obs(1, address="OLD", fields={"x": 1}))
        acc.flush_ready(15)
        acc.add_observation(obs(100, address="NEW", fields={"x": 2}))
        acc.flush_ready(10_000)
        self.assertEqual(acc._carry, {})
        with self.assertRaises(ValueError):
            acc.add_observation(obs(2, address="OLD", fields={"x": 3}))

    def test_late_data_for_flushed_bucket_is_rejected(self):
        acc = SnapshotAccumulator()
        acc.add_observation(obs(1, fields={}))
        acc.flush_ready(15)
        with self.assertRaises(ValueError):
            acc.add_observation(obs(14, fields={"x": 1}))

    def test_invalid_input_rejected(self):
        acc = SnapshotAccumulator()
        with self.assertRaises(ValueError):
            acc.add_observation({"received_at": float("nan")})


if __name__ == "__main__":
    unittest.main()
