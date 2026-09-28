import gzip
import hashlib
import unittest

from sky_scanner import tar1090db


def _rows_text(rows, pad_count=100_000):
    """Build semicolon-CSV text: real sample ``rows`` plus cheap padding rows
    so the parsed table clears the 100,000-aircraft floor."""
    lines = list(rows)
    lines += [f"{i:06X};;;00;;;;" for i in range(pad_count)]
    return "\n".join(lines) + "\n"


def _gzip(text):
    return gzip.compress(text.encode("utf-8"))


SAMPLE_ROWS = [
    "A8F5D1;N677AZ;B763;00;BOEING 767-300;2022;AMAZON.COM SERVICES LLC;",
    "AE0000;63-8146;T38;10;NORTHROP T-38 Talon;;;",
    "000FFF;;;10;;;Miscode - VARIOUS;",
]


class ParseTests(unittest.TestCase):
    def test_parses_real_format_rows_and_returns_sha256_of_compressed_bytes(self):
        data = _gzip(_rows_text(SAMPLE_ROWS))
        digest, table = tar1090db.parse(data)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(
            table["A8F5D1"],
            ("N677AZ", "B763", "00", "BOEING 767-300", "2022", "AMAZON.COM SERVICES LLC"),
        )
        self.assertEqual(
            table["AE0000"],
            ("63-8146", "T38", "10", "NORTHROP T-38 Talon", None, None),
        )
        self.assertEqual(
            table["000FFF"],
            (None, None, "10", None, None, "Miscode - VARIOUS"),
        )
        self.assertGreaterEqual(len(table), 100_000)

    def test_lowercase_hex_address_is_keyed_uppercase(self):
        data = _gzip(_rows_text(["a8f5d1;N1;B738;00;TEST;;;"] + SAMPLE_ROWS[1:]))
        _, table = tar1090db.parse(data)
        self.assertIn("A8F5D1", table)
        self.assertNotIn("a8f5d1", table)

    def test_short_rows_are_skipped(self):
        data = _gzip(_rows_text(["AAAAAA;N1"] + SAMPLE_ROWS))
        _, table = tar1090db.parse(data)
        self.assertNotIn("AAAAAA", table)

    def test_first_duplicate_wins(self):
        data = _gzip(_rows_text([
            "bbbbbb;N1;B738;00;FIRST;;;",
            "BBBBBB;N2;B739;00;SECOND;;;",
        ] + SAMPLE_ROWS))
        _, table = tar1090db.parse(data)
        self.assertEqual(table["BBBBBB"], ("N1", "B738", "00", "FIRST", None, None))

    def test_raises_value_error_when_fewer_than_100000_rows(self):
        data = _gzip("\n".join(SAMPLE_ROWS) + "\n")
        with self.assertRaisesRegex(ValueError, r"only \d+ aircraft"):
            tar1090db.parse(data)


class FlagNamesTests(unittest.TestCase):
    def test_military_bit(self):
        self.assertEqual(tar1090db.flag_names("10"), ["military"])

    def test_privacy_icao_address_bit(self):
        self.assertEqual(tar1090db.flag_names("0010"), ["privacy ICAO address (PIA)"])

    def test_ladd_bit(self):
        self.assertEqual(tar1090db.flag_names("0001"), ["LADD"])

    def test_empty_or_none_gives_no_flags(self):
        self.assertEqual(tar1090db.flag_names(""), [])
        self.assertEqual(tar1090db.flag_names(None), [])


class RecordsTests(unittest.TestCase):
    def setUp(self):
        data = _gzip(_rows_text(SAMPLE_ROWS))
        self.digest, self.table = tar1090db.parse(data)

    def test_only_observed_addresses_present_in_table_are_returned_sorted(self):
        result = tar1090db.records(
            self.table, ["ae0000", "A8F5D1", "FFFFFF"], dataset_hash=self.digest)
        self.assertEqual([r["icao_address"] for r in result], ["A8F5D1", "AE0000"])

    def test_tags_from_flags(self):
        result = tar1090db.records(self.table, ["AE0000"], dataset_hash=self.digest)
        self.assertEqual(result[0]["tags"], "military")

    def test_no_tags_for_clean_flags(self):
        result = tar1090db.records(self.table, ["A8F5D1"], dataset_hash=self.digest)
        self.assertIsNone(result[0]["tags"])

    def test_miscode_row_gets_miscode_tag_not_military(self):
        result = tar1090db.records(self.table, ["000FFF"], dataset_hash=self.digest)
        self.assertEqual(result[0]["tags"], "miscode")
        self.assertNotIn("military", result[0]["tags"])

    def test_provenance_fields_are_set(self):
        result = tar1090db.records(self.table, ["A8F5D1"], dataset_hash=self.digest)
        record = result[0]
        self.assertEqual(record["dataset_hash"], self.digest)
        self.assertEqual(record["source_url"], tar1090db.URL)
        self.assertTrue(record["retrieved_at"].endswith("Z"))
        self.assertEqual(record["registration"], "N677AZ")
        self.assertEqual(record["icao_type_code"], "B763")
        self.assertEqual(record["model"], "BOEING 767-300")
        self.assertEqual(record["registered_owner"], "AMAZON.COM SERVICES LLC")


if __name__ == "__main__":
    unittest.main()
