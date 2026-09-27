import unittest
from datetime import datetime, timezone

from sky_scanner import aircraft_log


DOC8643_CSV = (
    '"AircraftDescription","Description","Designator","EngineCount","EngineType",'
    '"ManufacturerCode","ModelFullName","WTC"\r\n'
    'Wide,"L2J",B738,2,Jet,BOEING,"737-800",M\r\n'
    'Wide,"L2J",B738,2,Jet,BOEING,"737-800 DUPLICATE",M\r\n'
    'WideHeavy,"L2J",B77W,2,Jet,BOEING,"777-300ER",H\r\n'
    'Biz,"L2J",CL30,2,Jet,BOMBARDIER,"Challenger 300",M\r\n'
    'Piston,"L1P",C172,1,Piston,CESSNA,"172",L\r\n'
    'Turboprop,"L1T",PC12,1,Turboprop,PILATUS,"PC-12",L\r\n'
    'Heli,"H1T",H500,1,Turboshaft,MD,"500",L\r\n'
)


class ParseDoc8643Tests(unittest.TestCase):
    def test_keeps_first_row_per_designator(self):
        types = aircraft_log.parse_doc8643(DOC8643_CSV.encode("utf-8"))
        self.assertEqual(types["B738"]["ModelFullName"], "737-800")
        self.assertEqual(len(types), 6)
        self.assertIn("B77W", types)
        self.assertIn("CL30", types)
        self.assertIn("C172", types)
        self.assertIn("PC12", types)
        self.assertIn("H500", types)


class FaaTypeTests(unittest.TestCase):
    def test_known_models(self):
        cases = {
            "737-823": "B738",
            "737-8": "B38M",
            "737-9": "B39M",
            "737-7H4": "B737",
            "737-924ER": "B739",
            "A321-271NX": "A21N",
            "A321-231": "A321",
            "A320-251N": "A20N",
            "BD-100-1A10": "CL30",
            "ERJ 170-200 LR": "E75L",
            "TBM 700": "TBM7",
            "172N": "C172",
            "PA-28-180": "P28A",
            "PA-28R-200": "P28R",
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(aircraft_log.faa_type(model), expected)

    def test_unknown_or_empty_returns_none(self):
        self.assertIsNone(aircraft_log.faa_type(None))
        self.assertIsNone(aircraft_log.faa_type(""))
        self.assertIsNone(aircraft_log.faa_type("   "))
        self.assertIsNone(aircraft_log.faa_type("SOME-UNKNOWN-MODEL-9999"))


class AircraftClassTests(unittest.TestCase):
    def setUp(self):
        self.types = aircraft_log.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_wide_body_jet(self):
        self.assertEqual(aircraft_log.aircraft_class("B77W", self.types), "Wide-body jet")

    def test_narrow_body_airliner(self):
        self.assertEqual(
            aircraft_log.aircraft_class("B738", self.types), "Narrow-body / regional jet"
        )

    def test_business_jet_non_airliner(self):
        self.assertEqual(aircraft_log.aircraft_class("CL30", self.types), "Business jet")

    def test_piston(self):
        self.assertEqual(aircraft_log.aircraft_class("C172", self.types), "Piston")

    def test_turboprop(self):
        self.assertEqual(aircraft_log.aircraft_class("PC12", self.types), "Turboprop")

    def test_helicopter(self):
        self.assertEqual(aircraft_log.aircraft_class("H500", self.types), "Helicopter")

    def test_none_designator_unidentified(self):
        self.assertEqual(aircraft_log.aircraft_class(None, self.types), "Unidentified")

    def test_unknown_designator_starting_b7_is_narrow_body(self):
        self.assertEqual(
            aircraft_log.aircraft_class("B712", self.types), "Narrow-body / regional jet"
        )

    def test_unknown_designator_other(self):
        self.assertEqual(aircraft_log.aircraft_class("ZZZZ", self.types), "Other")


def _row(
    address="A1B2C3",
    address_type="icao",
    first_seen=None,
    last_seen=None,
    bands=(1_090_000_000, 978_000_000),
    snapshots=10,
    messages=100,
    max_alt=35000,
    callsign="",
    faa_reg="",
    faa_mfr="",
    faa_model="",
    faa_owner="",
    os_reg="",
    os_model="",
    os_type="",
    os_operator="",
    os_operator_icao="",
    os_owner="",
    os_country="",
):
    first_seen = first_seen or datetime(2024, 1, 1, tzinfo=timezone.utc)
    last_seen = last_seen or datetime(2024, 1, 2, tzinfo=timezone.utc)
    return (
        address, address_type, first_seen, last_seen, list(bands), snapshots, messages,
        max_alt, callsign, faa_reg, faa_mfr, faa_model, faa_owner, os_reg, os_model,
        os_type, os_operator, os_operator_icao, os_owner, os_country,
    )


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.types = aircraft_log.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_faa_row_wins_type_over_opensky(self):
        row = _row(
            faa_reg="N123AB", faa_mfr="BOEING", faa_model="737-823",
            os_type="C172",  # OpenSky disagrees; FAA model should still win.
        )
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["src"], "F")
        self.assertEqual(result["tc"], "B738")
        self.assertEqual(result["reg"], "N123AB")

    def test_opensky_only_row(self):
        row = _row(
            os_reg="D-ABCD", os_model="A320", os_type="A320",
            os_operator="Lufthansa", os_operator_icao="DLH", os_owner="Lufthansa",
            os_country="Germany",
        )
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["src"], "O")
        self.assertEqual(result["tc"], "A320")
        self.assertEqual(result["reg"], "D-ABCD")
        self.assertEqual(result["op"], "Lufthansa")
        self.assertEqual(result["country"], "Germany")

    def test_no_metadata_row(self):
        row = _row()
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["src"], "-")
        self.assertEqual(result["class"], "Unidentified")
        self.assertEqual(result["kind"], "Other")

    def test_band_ordering(self):
        row = _row(bands=(1_090_000_000, 978_000_000))
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["band"], "978,1090")

    def test_iso_datetime_strings(self):
        first_seen = datetime(2024, 5, 1, 12, 30, tzinfo=timezone.utc)
        last_seen = datetime(2024, 5, 2, 8, 0, tzinfo=timezone.utc)
        row = _row(first_seen=first_seen, last_seen=last_seen)
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["first"], first_seen.isoformat())
        self.assertEqual(result["last"], last_seen.isoformat())

    def test_military_by_address_prefix(self):
        row = _row(address="AE1234")
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Military")

    def test_airline_by_callsign(self):
        row = _row(callsign="SWA123")
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Airline")

    def test_airline_by_owner_name(self):
        row = _row(faa_reg="N1", faa_owner="SOME AIRLINES INC")
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Airline")

    def test_airline_narrow_body_owned_by_bank_trustee(self):
        row = _row(
            faa_reg="N1", faa_mfr="BOEING", faa_model="737-823",
            faa_owner="WELLS FARGO BANK TRUST CO",
        )
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["class"], "Narrow-body / regional jet")
        self.assertEqual(result["kind"], "Airline")

    def test_other_for_cessna_owned_by_llc(self):
        row = _row(faa_reg="N1", faa_mfr="CESSNA", faa_model="172N", faa_owner="SOME LLC")
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["tc"], "C172")
        self.assertEqual(result["class"], "Piston")
        self.assertEqual(result["kind"], "Other")


class SummarySqlTests(unittest.TestCase):
    def test_filters_by_source_name(self):
        self.assertIn("m.source_name = 'faa_registry'", aircraft_log.SUMMARY_SQL)
        self.assertIn("m.source_name = 'opensky'", aircraft_log.SUMMARY_SQL)

    def test_has_since_parameter(self):
        self.assertIn("%(since)s", aircraft_log.SUMMARY_SQL)


if __name__ == "__main__":
    unittest.main()
