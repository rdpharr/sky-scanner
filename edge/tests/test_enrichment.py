import csv
import io
import unittest
import zipfile

from sky_scanner.enrichment import normalize_icao, normalize_registration, parse_faa_registry


def make_zip(master: str, acftref: str = "") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("MASTER.txt", master)
        zf.writestr("ACFTREF.txt", acftref)
    return out.getvalue()


class EnrichmentParserTests(unittest.TestCase):
    def test_header_normalization_join_and_provenance(self):
        master = (
            'n-number,"Mfr Mdl Code","Registrant Name","Mode S Code Hex"\n'
            '123AB,0012345,"  ACME AVIATION, LLC ",00a001\n'
            '123AC,9999999,Unknown,ABCDEF\n'
            'anonymous,0012345,Private,123456\n'
        )
        acftref = 'Code,Manufacturer,Model,Type Aircraft\n0012345,Cessna,172S,4\n'
        rows = parse_faa_registry(
            make_zip(master, acftref), source_url="https://example.test/registry.zip",
            retrieved_at="2026-09-26T08:00:00-07:00",
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["icao_address"], "00A001")
        self.assertEqual(rows[0]["registration"], "N123AB")
        self.assertEqual(rows[0]["manufacturer"], "Cessna")
        self.assertEqual(rows[0]["model"], "172S")
        self.assertEqual(rows[0]["aircraft_type"], "4")
        self.assertEqual(rows[0]["registered_owner"], "ACME AVIATION, LLC")
        self.assertIsNone(rows[0]["operator"])
        self.assertEqual(rows[0]["retrieved_at"], "2026-09-26T15:00:00Z")
        self.assertEqual(len(rows[0]["dataset_hash"]), 64)
        self.assertIsNone(rows[1]["manufacturer"])

    def test_quoted_commas_and_header_whitespace(self):
        master = ' N Number , Aircraft Manufacturer Model Code , Owner , Mode S Code Hex \n4567,"0000001","Example, Inc.",abcdef\n'
        ref = 'Aircraft Manufacturer Model Code,Manufacturer Name,Model Name,Type Aircraft\n0000001,ACME,Model X,6\n'
        row = parse_faa_registry(make_zip(master, ref))[0]
        self.assertEqual(row["registration"], "N4567")
        self.assertEqual(row["registered_owner"], "Example, Inc.")
        self.assertEqual(row["aircraft_type"], "6")

    def test_current_faa_headers_join_mfr_and_type_acft_columns(self):
        master = (
            'N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,TYPE REGISTRANT,NAME,MODE S CODE HEX,\n'
            '12345,serial,0000001,00000,1975,1,Owner Name,00a123,\n'
        )
        acftref = (
            'CODE,MFR,MODEL,TYPE-ACFT,TYPE-ENG,AC-CAT\n'
            '0000001,Cessna,172S,4,1,1\n'
        )
        rows = parse_faa_registry(make_zip(master, acftref))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["manufacturer"], "Cessna")
        self.assertEqual(rows[0]["model"], "172S")
        self.assertEqual(rows[0]["aircraft_type"], "4")

    def test_malformed_detected_headers_raise_and_long_n_number_is_rejected(self):
        malformed_master = 'N-NUMBER,MFR MDL CODE,NAME\n12345,0000001,Owner\n'
        with self.assertRaisesRegex(ValueError, "malformed FAA MASTER header"):
            parse_faa_registry(make_zip(malformed_master, 'CODE,MFR,MODEL,TYPE-ACFT\n'))
        malformed_ref = 'CODE,MFR,MODEL\n0000001,Cessna,172S\n'
        with self.assertRaisesRegex(ValueError, "malformed FAA REFERENCE header"):
            parse_faa_registry(make_zip('12345,x,x,' + ','.join([''] * 29) + 'ABCDEF\n', malformed_ref))
        self.assertIsNone(normalize_registration("N12345AB"))

    def test_normalizers_reject_unknown_and_preserve_zeroes(self):
        self.assertEqual(normalize_icao(" 00000a "), "00000A")
        self.assertIsNone(normalize_icao("anonymous"))
        self.assertEqual(normalize_registration("N123AB"), "N123AB")
        self.assertEqual(normalize_registration("123ab"), "N123AB")
        self.assertIsNone(normalize_registration("withheld"))

    def test_missing_members_and_invalid_zip_raise_value_error(self):
        with self.assertRaisesRegex(ValueError, "invalid"):
            parse_faa_registry(b"not a zip")
        with self.assertRaisesRegex(ValueError, "missing"):
            parse_faa_registry(_zip_with_only_master())


def _zip_with_only_master():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("MASTER.txt", "")
    return out.getvalue()


if __name__ == "__main__":
    unittest.main()
