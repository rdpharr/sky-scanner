import unittest

from sky_scanner.uat import normalize_uat


class UatNormalizationTests(unittest.TestCase):
    def test_direct_icao_maps_canonical_fields_and_keeps_decoded_fields(self):
        payload = {
            "address_qualifier": "adsb_icao",
            "address": "00a123",
            "position": {"lat": 37.5, "lon": -122.2},
            "pressure_altitude": 0,
            "geometric_altitude": 1250,
            "ground_speed": 91.2,
            "true_track": 180.0,
            "vertical_velocity_barometric": -640,
            "vertical_velocity_geometric": 64,
            "airground_state": "ground",
            "callsign": "N123AB",
            "flightplan_id": "1200",
            "nic": 8,
            "metadata": {"received_at": 1727000000.125, "raw_timestamp": 999999},
        }
        observation = normalize_uat(payload, "rx-1", received_at=10)
        self.assertEqual(observation["received_at"], 1727000000.125)
        self.assertEqual(observation["receiver_id"], "rx-1")
        self.assertEqual(observation["address"], "00A123")
        self.assertEqual(observation["address_type"], "icao")
        self.assertEqual(observation["frequency_hz"], 978000000)
        self.assertEqual(observation["source"], "direct")
        fields = observation["fields"]
        self.assertEqual(fields["altitude_baro_ft"], 0)
        self.assertEqual(fields["altitude_geom_ft"], 1250)
        self.assertEqual(fields["ground_speed_kt"], 91.2)
        self.assertEqual(fields["track_deg"], 180.0)
        self.assertEqual(fields["vertical_rate_baro_fpm"], -640)
        self.assertEqual(fields["vertical_rate_geom_fpm"], 64)
        self.assertEqual(fields["latitude"], 37.5)
        self.assertEqual(fields["longitude"], -122.2)
        self.assertEqual(fields["on_ground"], True)
        self.assertEqual(fields["squawk"], "1200")
        self.assertEqual(fields["nic"], 8)
        self.assertNotIn("metadata", fields)

    def test_qualifiers_set_source_and_keep_non_icao_identities_distinct(self):
        cases = [
            ("tisb_icao", "tisb", "icao"),
            ("tisb_trackfile", "tisb", "uat:tisb_trackfile"),
            ("adsr_other", "adsr", "uat:adsr_other"),
            ("adsb_other", "direct", "uat:adsb_other"),
        ]
        for qualifier, source, address_type in cases:
            with self.subTest(qualifier=qualifier):
                row = normalize_uat({"address_qualifier": qualifier, "address": "abc"}, "rx")
                self.assertEqual(row["source"], source)
                self.assertEqual(row["address_type"], address_type)
                self.assertEqual(row["address"], "000ABC")

    def test_missing_position_and_received_time_fallback(self):
        row = normalize_uat(
            {"address_qualifier": "adsb_icao", "address": "ABCDEF", "metadata": {"rssi": -22}},
            "receiver", received_at=123.25,
        )
        self.assertEqual(row["received_at"], 123.25)
        self.assertNotIn("latitude", row["fields"])
        self.assertNotIn("longitude", row["fields"])

    def test_decoder_airground_enum_names_map_to_boolean(self):
        for state, expected in (("airborne", False), ("supersonic", False), ("ground", True)):
            with self.subTest(state=state):
                row = normalize_uat(
                    {"address_qualifier": "adsb_icao", "address": "ABCDEF",
                     "airground_state": state},
                    "rx", received_at=1,
                )
                self.assertEqual(row["fields"]["on_ground"], expected)

    def test_unknown_reserved_invalid_and_uplink_are_ignored(self):
        for payload in (
            {"address_qualifier": "invalid", "address": "ABCDEF"},
            {"address_qualifier": "reserved", "address": "ABCDEF"},
            {"address_qualifier": "vehicle", "address": "ABCDEF"},
            {"address_qualifier": "fixed_beacon", "address": "ABCDEF"},
            {"address_qualifier": "adsb_icao", "address": "ABCDEF", "type": "uplink"},
            {"address_qualifier": "adsb_icao", "address": "not-hex"},
            {"address_qualifier": "adsb_icao"},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(normalize_uat(payload, "receiver", received_at=1))


if __name__ == "__main__":
    unittest.main()
