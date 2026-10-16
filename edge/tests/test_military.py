import hashlib
import unittest
import urllib.error
from unittest.mock import patch

from sky_scanner import military

# --- parse_plane_alert_mil ---------------------------------------------------

PLANE_ALERT_HEADER = "$ICAO,Registration,Operator,Type,$ICAO Type,Category,Tag 1,$#Tag 2,Tag 3"
PLANE_ALERT_CSV = (
    "\n".join(
        [
            PLANE_ALERT_HEADER,
            "AE1001,N100AF,US Air Force,C-17,C17,Military,Cargo,USAF,",
            "ae1002,????,US Navy,P-8,P8,Military,,Patrol,",
            "invalidx,N999,Bad Row,Bad,BAD,Bad,,,",
            "AE1001,DUPLICATE,Duplicate Row,X,X,X,,,",
        ]
    )
    + "\n"
)


class ParsePlaneAlertMilTests(unittest.TestCase):
    def test_header_prefixes_are_stripped(self):
        records = military.parse_plane_alert_mil(PLANE_ALERT_CSV.encode())
        self.assertEqual(records[0]["icao_type_code"], "C17")

    def test_addresses_are_uppercased(self):
        records = military.parse_plane_alert_mil(PLANE_ALERT_CSV.encode())
        addresses = [r["icao_address"] for r in records]
        self.assertIn("AE1002", addresses)
        self.assertNotIn("ae1002", addresses)

    def test_invalid_address_is_skipped(self):
        records = military.parse_plane_alert_mil(PLANE_ALERT_CSV.encode())
        addresses = [r["icao_address"] for r in records]
        self.assertNotIn("INVALIDX", addresses)

    def test_duplicate_address_is_skipped(self):
        records = military.parse_plane_alert_mil(PLANE_ALERT_CSV.encode())
        addresses = [r["icao_address"] for r in records]
        self.assertEqual(addresses.count("AE1001"), 1)
        self.assertEqual(records[0]["operator"], "US Air Force")

    def test_tags_are_joined(self):
        records = military.parse_plane_alert_mil(PLANE_ALERT_CSV.encode())
        self.assertEqual(records[0]["tags"], "Cargo, USAF")

    def test_placeholder_registration_becomes_none(self):
        records = military.parse_plane_alert_mil(PLANE_ALERT_CSV.encode())
        record = next(r for r in records if r["icao_address"] == "AE1002")
        self.assertIsNone(record["registration"])

    def test_missing_icao_column_raises(self):
        data = b"Registration,Type\nN1,C17\n"
        with self.assertRaisesRegex(ValueError, "ICAO"):
            military.parse_plane_alert_mil(data)

    def test_dataset_hash_is_sha256_of_bytes(self):
        data = PLANE_ALERT_CSV.encode()
        records = military.parse_plane_alert_mil(data)
        expected = hashlib.sha256(data).hexdigest()
        self.assertTrue(all(r["dataset_hash"] == expected for r in records))


# --- parse_military_operators ------------------------------------------------

OPERATOR_HEADER = "Code,Name,ICAO,IATA,PositioningFlightPattern,CharterFlightPattern"
OPERATOR_ROWS = [
    "1,Air Mobility Command,AMC,,,",
    "2,United States Navy,USN,,,",
    "3,German Air Force,GAF,,,",
    "4,Southwest Airlines,SWA,,,",
    "5,Air Mobility Command Duplicate,AMC,,,",
]
OPERATOR_CSV = "\n".join([OPERATOR_HEADER, *OPERATOR_ROWS]) + "\n"


class ParseMilitaryOperatorsTests(unittest.TestCase):
    def test_keeps_only_military_three_letter_codes(self):
        codes = {c["code"] for c in military.parse_military_operators(OPERATOR_CSV.encode())}
        self.assertEqual(codes, {"AMC", "USN", "GAF"})

    def test_drops_airlines(self):
        codes = {c["code"] for c in military.parse_military_operators(OPERATOR_CSV.encode())}
        self.assertNotIn("SWA", codes)

    def test_dedupes_by_code_keeping_first(self):
        result = military.parse_military_operators(OPERATOR_CSV.encode())
        amc = next(c for c in result if c["code"] == "AMC")
        self.assertEqual(amc["name"], "Air Mobility Command")
        self.assertEqual(len(result), 3)

    def test_missing_columns_raises(self):
        data = b"Code,ICAO,IATA\n1,AMC,\n"
        with self.assertRaisesRegex(ValueError, "ICAO/Name"):
            military.parse_military_operators(data)

    def test_utf8_sig_bom_is_allowed(self):
        data = ("﻿" + OPERATOR_CSV).encode("utf-8-sig")
        codes = {c["code"] for c in military.parse_military_operators(data)}
        self.assertEqual(codes, {"AMC", "USN", "GAF"})


# --- callword -----------------------------------------------------------------


class CallwordTests(unittest.TestCase):
    def test_talon95_gives_talon(self):
        self.assertEqual(military.callword("TALON95"), "TALON")

    def test_stljw21_gives_stljw(self):
        self.assertEqual(military.callword("STLJW21"), "STLJW")

    def test_rch123_with_operator_code_is_none(self):
        self.assertIsNone(military.callword("RCH123", {"RCH"}))

    def test_rch123_without_operator_code_is_rch(self):
        self.assertEqual(military.callword("RCH123"), "RCH")

    def test_registration_n18ny_is_none(self):
        self.assertIsNone(military.callword("N18NY"))

    def test_c6597_is_none(self):
        self.assertIsNone(military.callword("C6597"))

    def test_empty_or_none_is_none(self):
        self.assertIsNone(military.callword(""))
        self.assertIsNone(military.callword(None))


# --- us_callsign_service --------------------------------------------------


class UsCallsignServiceTests(unittest.TestCase):
    def test_coast_guard_on_us_military_address(self):
        self.assertEqual(
            military.us_callsign_service("AE26B7", "C6597"),
            "US Coast Guard (aircraft number)",
        )

    def test_non_us_address_returns_none(self):
        self.assertIsNone(military.us_callsign_service("A01234", "C6597"))

    def test_other_callsign_returns_none(self):
        self.assertIsNone(military.us_callsign_service("AE26B7", "RCH123"))

    def test_no_callsign_returns_none(self):
        self.assertIsNone(military.us_callsign_service("AE26B7", None))


# --- parse_wwi_family / fetch_wwi_family --------------------------------------


def _wwi_page(title="TALON · Intel Codex"):
    lines = [
        "INTEL CODEX · CALLSIGN FAMILIES",
        "TALON",
        "Callsign families",
        "OBSERVED",
        "War Watch ADS-B recordings",
        "Seen on 25 days, 77 airframes: ...",
        "Days",
        "25",
        "Airframes",
        "77",
        "Types",
        "HAWK",
        "36",
        "H60",
        "27",
        "Countries",
        "United States",
        "71",
        "Examples",
        "TALON11, TALON21",
        "first seen",
        "2026-07-17",
        "last seen",
        "2026-09-28",
    ]
    body = "".join(f"<div>{line}</div>\n" for line in lines)
    return f"""<html><head><title>{title}</title></head>
<body>
<script>var notFound = "This page could not be found.";</script>
<main>
{body}
</main>
</body></html>"""


class ParseWwiFamilyTests(unittest.TestCase):
    def test_parses_family_page_despite_embedded_not_found_script(self):
        page = _wwi_page()
        result = military.parse_wwi_family(page, "talon")
        self.assertIsNotNone(result)
        self.assertEqual(result["days"], 25)
        self.assertEqual(result["airframes"], 77)
        self.assertEqual(result["types"], [{"name": "HAWK", "count": 36}, {"name": "H60", "count": 27}])
        self.assertEqual(result["countries"], [{"name": "United States", "count": 71}])
        self.assertEqual(result["examples"], ["TALON11", "TALON21"])
        self.assertEqual(result["first_seen"], "2026-07-17")
        self.assertEqual(result["last_seen"], "2026-09-28")

    def test_404_title_returns_none(self):
        page = (
            "<html><head><title>404: This page could not be found.</title></head>"
            "<body><main>nothing here</main></body></html>"
        )
        self.assertIsNone(military.parse_wwi_family(page, "GHOST"))

    def test_missing_word_raises(self):
        page = _wwi_page()
        with self.assertRaisesRegex(ValueError, "GHOST"):
            military.parse_wwi_family(page, "GHOST")


class FetchWwiFamilyTests(unittest.TestCase):
    class _Response:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return self.body

    @patch("sky_scanner.military.urllib.request.urlopen")
    def test_404_returns_none_without_raising(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        url, family = military.fetch_wwi_family("talon")
        self.assertIsNone(family)
        self.assertIn("TALON", url)

    @patch("sky_scanner.military.urllib.request.urlopen")
    def test_other_http_error_is_reraised(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            military.fetch_wwi_family("talon")

    @patch("sky_scanner.military.urllib.request.urlopen")
    def test_success_parses_page_uses_uppercased_word_and_sets_user_agent(self, urlopen):
        page = _wwi_page().encode("utf-8")
        urlopen.return_value = self._Response(page)
        url, family = military.fetch_wwi_family("talon")
        self.assertIn("TALON", url)
        self.assertEqual(family["word"], "TALON")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), military.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
