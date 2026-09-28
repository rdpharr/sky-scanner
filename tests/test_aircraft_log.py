import decimal
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


class IdentifyTests(unittest.TestCase):
    def setUp(self):
        self.types = aircraft_log.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_faa_preferred_over_opensky(self):
        faa = {"registration": "N123AB", "manufacturer": "BOEING", "model": "737-823",
               "registered_owner": "SOME AIRLINE"}
        opensky = {"registration": "D-ABCD", "model": "A320", "icao_type_code": "A320",
                   "registered_owner": "LUFTHANSA", "operator": "Lufthansa",
                   "operator_icao": "DLH", "country": "Germany"}
        result = aircraft_log.identify("A1B2C3", "AAL1", faa, opensky, self.types)
        self.assertEqual(result["src"], "F")
        self.assertEqual(result["reg"], "N123AB")
        self.assertEqual(result["tc"], "B738")

    def test_opensky_only_gives_src_o(self):
        opensky = {"registration": "D-ABCD", "model": "A320", "icao_type_code": "A320"}
        result = aircraft_log.identify("A1B2C3", None, None, opensky, self.types)
        self.assertEqual(result["src"], "O")
        self.assertEqual(result["reg"], "D-ABCD")

    def test_empty_metadata_gives_dash_and_unidentified(self):
        result = aircraft_log.identify("A1B2C3", None, None, None, self.types)
        self.assertEqual(result["src"], "-")
        self.assertEqual(result["class"], "Unidentified")
        self.assertEqual(result["reg"], "")

    def test_military_by_ae_prefix(self):
        result = aircraft_log.identify("AE1234", None, None, None, self.types)
        self.assertEqual(result["kind"], "Military")

    def test_non_military_prefix(self):
        result = aircraft_log.identify("A01234", None, None, None, self.types)
        self.assertNotEqual(result["kind"], "Military")


def _sighting_row(start, end, snapshots=5, messages=50, bands=(1_090_000_000,),
                   callsigns=("UAL123",), squawks=("1200",), alt_min=30000, alt_max=35000,
                   gs_max=450, positions=5, on_ground=False):
    return (start, end, snapshots, messages, list(bands), list(callsigns), list(squawks),
            alt_min, alt_max, gs_max, positions, on_ground)


def _metadata_row(source_name, registry_status, registration="", manufacturer="", model="",
                   aircraft_type="", icao_type_code="", registered_owner="", operator="",
                   operator_icao="", country="", source_url="", retrieved_at=None,
                   dataset_hash="hash"):
    retrieved_at = retrieved_at or datetime(2024, 1, 1, tzinfo=timezone.utc)
    return (source_name, registry_status, registration, manufacturer, model, aircraft_type,
            icao_type_code, registered_owner, operator, operator_icao, country, source_url,
            retrieved_at, dataset_hash)


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.types = aircraft_log.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_returns_empty_dict_when_no_sightings(self):
        self.assertEqual(
            aircraft_log.detail("A1B2C3", [], [], None, self.types), {}
        )

    def test_full_detail_payload(self):
        newest_start = datetime(2024, 6, 2, tzinfo=timezone.utc)
        newest_end = datetime(2024, 6, 2, 1, 0, tzinfo=timezone.utc)
        oldest_start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        oldest_end = datetime(2024, 6, 1, 1, 0, tzinfo=timezone.utc)

        # Rows come back newest-first, per SIGHTINGS_SQL's ORDER BY.
        sighting_rows = [
            _sighting_row(newest_start, newest_end, snapshots=5, messages=50,
                          bands=(1_090_000_000,), callsigns=("UAL123",), squawks=("1200",),
                          alt_min=decimal.Decimal("30000"), alt_max=decimal.Decimal("35000"),
                          gs_max=decimal.Decimal("450"), positions=5, on_ground=False),
            _sighting_row(oldest_start, oldest_end, snapshots=3, messages=30,
                          bands=(978_000_000,), callsigns=(), squawks=(), alt_min=None,
                          alt_max=None, gs_max=None, positions=0, on_ground=True),
        ]
        metadata_rows = [
            # Latest FAA version is 'absent' -> the source should be ignored for identity,
            # even though an older 'present' version exists.
            _metadata_row("faa_registry", "absent",
                          retrieved_at=datetime(2024, 3, 1, tzinfo=timezone.utc)),
            _metadata_row("faa_registry", "present", registration="N123AB",
                          manufacturer="BOEING", model="737-823",
                          registered_owner="SOME OWNER",
                          retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc)),
            _metadata_row("opensky", "present", registration="D-ABCD", model="A320",
                          icao_type_code="A320", operator="Lufthansa",
                          operator_icao="DLH", country="Germany",
                          retrieved_at=datetime(2024, 2, 1, tzinfo=timezone.utc)),
        ]
        latest_row = (newest_end, 1_090_000_000, {"squawk": "1200"})

        result = aircraft_log.detail("A1B2C3", metadata_rows, sighting_rows, latest_row,
                                     self.types)

        # first == last sighting's start (rows are newest first).
        self.assertEqual(result["first"], oldest_start.isoformat())
        # last == first row's end.
        self.assertEqual(result["last"], newest_end.isoformat())

        totals = result["totals"]
        self.assertEqual(totals["sightings"], 2)
        self.assertEqual(totals["snapshots"], 8)
        self.assertEqual(totals["messages"], 80)
        self.assertEqual(totals["bands"], [978, 1090])
        self.assertEqual(totals["alt_max"], 35000.0)
        self.assertIsInstance(totals["alt_max"], float)
        self.assertEqual(totals["gs_max"], 450.0)
        self.assertEqual(totals["callsigns"], ["UAL123"])

        # FAA's latest version is 'absent', so identity should fall back to OpenSky.
        identity = result["identity"]
        self.assertEqual(identity["src"], "O")
        self.assertEqual(identity["reg"], "D-ABCD")

        # Metadata versions retain both FAA rows and the OpenSky row, ISO timestamps.
        self.assertEqual(len(result["metadata"]), 3)
        self.assertEqual(result["metadata"][0]["retrieved_at"],
                         datetime(2024, 3, 1, tzinfo=timezone.utc).isoformat())

        self.assertEqual(result["latest_fields"]["band"], 1090)
        self.assertEqual(result["latest_fields"]["bucket_start"], newest_end.isoformat())
        self.assertEqual(result["latest_fields"]["fields"], {"squawk": "1200"})

    def test_ignores_source_whose_latest_version_is_absent_entirely(self):
        sighting_rows = [_sighting_row(
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, 1, tzinfo=timezone.utc)
        )]
        metadata_rows = [_metadata_row("faa_registry", "absent")]
        result = aircraft_log.detail("A1B2C3", metadata_rows, sighting_rows, None, self.types)
        self.assertEqual(result["identity"]["src"], "-")

    def test_latest_fields_none_without_latest_row(self):
        sighting_rows = [_sighting_row(
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, 1, tzinfo=timezone.utc)
        )]
        result = aircraft_log.detail("A1B2C3", [], sighting_rows, None, self.types)
        self.assertIsNone(result["latest_fields"])


class SnapshotRowsTests(unittest.TestCase):
    def test_converts_timestamps_band_and_decimals(self):
        bucket = datetime(2024, 1, 1, tzinfo=timezone.utc)
        first = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        last = datetime(2024, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
        row = (
            bucket, 1_090_000_000, first, last, 12, "UAL123",
            decimal.Decimal("40.1"), decimal.Decimal("-73.2"), decimal.Decimal("35000"),
            decimal.Decimal("35100"), decimal.Decimal("450"), decimal.Decimal("270"),
            decimal.Decimal("0"), decimal.Decimal("0"), "1200", False, {"nav": "on"},
        )
        result = aircraft_log.snapshot_rows([row])
        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual(item["bucket"], bucket.isoformat())
        self.assertEqual(item["first"], first.isoformat())
        self.assertEqual(item["last"], last.isoformat())
        self.assertEqual(item["band"], 1090)
        self.assertIsInstance(item["band"], int)
        for key in ("lat", "lon", "alt_baro", "alt_geom", "gs", "track", "vr_baro", "vr_geom"):
            self.assertIsInstance(item[key], float)
        self.assertEqual(item["lat"], 40.1)
        self.assertEqual(item["squawk"], "1200")
        self.assertEqual(item["on_ground"], False)
        self.assertEqual(item["fields"], {"nav": "on"})

    def test_empty_rows(self):
        self.assertEqual(aircraft_log.snapshot_rows([]), [])


class SightingsAndSnapshotsSqlTests(unittest.TestCase):
    def test_sightings_sql_uses_gap_and_filters(self):
        self.assertIn(f"interval '{aircraft_log.SIGHTING_GAP_MINUTES} minutes'",
                      aircraft_log.SIGHTINGS_SQL)
        self.assertIn("%(address_type)s", aircraft_log.SIGHTINGS_SQL)
        self.assertIn("%(address)s", aircraft_log.SIGHTINGS_SQL)

    def test_snapshots_sql_has_limit(self):
        self.assertIn("LIMIT 5000", aircraft_log.SNAPSHOTS_SQL)
        self.assertIn("%(address_type)s", aircraft_log.SNAPSHOTS_SQL)
        self.assertIn("%(address)s", aircraft_log.SNAPSHOTS_SQL)


# --- military_evidence -------------------------------------------------------

class MilitaryEvidenceTests(unittest.TestCase):
    def test_ae_address_gives_us_block_evidence(self):
        evidence = aircraft_log.military_evidence("AE1234", None, None)
        self.assertIn("US military ICAO address block", evidence)

    def test_af_address_gives_us_block_evidence(self):
        evidence = aircraft_log.military_evidence("AF1234", None, None)
        self.assertIn("US military ICAO address block", evidence)

    def test_non_military_address_and_no_other_signal_is_empty(self):
        self.assertEqual(aircraft_log.military_evidence("A01234", None, None), [])

    def test_plane_alert_gives_evidence(self):
        evidence = aircraft_log.military_evidence("A01234", {"operator": "US Air Force"}, None)
        self.assertIn("plane-alert-db military list", evidence)

    def test_operator_code_name_gives_evidence(self):
        evidence = aircraft_log.military_evidence("A01234", None, "Air Mobility Command")
        self.assertIn("military operator code (Air Mobility Command)", evidence)


# --- identify: military ------------------------------------------------------

class MilitaryIdentifyTests(unittest.TestCase):
    def setUp(self):
        self.types = aircraft_log.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_plane_alert_sets_operator_and_model_and_evidence(self):
        plane_alert = {"operator": "United States Navy", "model": "P-8A Poseidon",
                       "icao_type_code": "P8"}
        result = aircraft_log.identify("A01234", None, None, None, self.types,
                                       plane_alert=plane_alert)
        self.assertIn("plane-alert-db military list", result["military_evidence"])
        self.assertEqual(result["op"], "United States Navy")
        self.assertEqual(result["model"], "P-8A Poseidon")
        self.assertEqual(result["kind"], "Military")

    def test_operator_code_name_evidence_and_operator(self):
        result = aircraft_log.identify("A01234", "RCH123", None, None, self.types,
                                       operator_code_name="Air Mobility Command")
        self.assertIn("military operator code (Air Mobility Command)", result["military_evidence"])
        self.assertEqual(result["op"], "Air Mobility Command")
        self.assertEqual(result["kind"], "Military")

    def test_military_is_never_classified_airline_despite_airline_callsign(self):
        result = aircraft_log.identify("AE1234", "AAL1", None, None, self.types)
        self.assertEqual(result["kind"], "Military")

    def test_non_military_civil_aircraft_has_empty_evidence(self):
        result = aircraft_log.identify("A01234", "N12345", None, None, self.types)
        self.assertEqual(result["military_evidence"], [])
        self.assertNotEqual(result["kind"], "Military")

    def test_coast_guard_callsign_on_us_military_address_adds_evidence(self):
        result = aircraft_log.identify("AE26B7", "C6597", None, None, self.types)
        self.assertIn("callsign convention (US Coast Guard (aircraft number))",
                      result["military_evidence"])


# --- detail: military ---------------------------------------------------------

def _full_metadata_row(source_name, registry_status, registration="", manufacturer="", model="",
                       aircraft_type="", icao_type_code="", registered_owner="", operator="",
                       operator_icao="", country="", source_url="", retrieved_at=None,
                       dataset_hash="hash", category="", tags=""):
    retrieved_at = retrieved_at or datetime(2024, 1, 1, tzinfo=timezone.utc)
    return (source_name, registry_status, registration, manufacturer, model, aircraft_type,
            icao_type_code, registered_owner, operator, operator_icao, country, source_url,
            retrieved_at, dataset_hash, category, tags)


class MilitaryDetailTests(unittest.TestCase):
    def setUp(self):
        self.types = aircraft_log.parse_doc8643(DOC8643_CSV.encode("utf-8"))
        self.rch_sighting_rows = [_sighting_row(
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
            callsigns=("RCH123",),
        )]

    def test_plane_alert_populated_and_placeholder_values_cleaned(self):
        metadata_rows = [_full_metadata_row(
            "plane_alert_mil", "present", registration="????", operator="US Air Force",
            model="C-17A", icao_type_code="C17", category="Military", tags="Cargo",
        )]
        result = aircraft_log.detail("AE1234", metadata_rows, self.rch_sighting_rows, None, self.types)
        pa = result["military"]["plane_alert"]
        self.assertIsNone(pa["registration"])
        self.assertEqual(pa["operator"], "US Air Force")
        self.assertEqual(pa["model"], "C-17A")

    def test_operator_codes_match_from_callsign(self):
        result = aircraft_log.detail("A01234", [], self.rch_sighting_rows, None, self.types,
                                     operator_codes={"RCH": "Air Mobility Command"})
        self.assertEqual(result["military"]["operator_codes"], [
            {"callsign": "RCH123", "code": "RCH", "name": "Air Mobility Command"},
        ])

    def test_callword_type_match_against_opensky_type(self):
        metadata_rows = [_full_metadata_row("opensky", "present", icao_type_code="H60")]
        talon_sighting_rows = [_sighting_row(
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
            callsigns=("RCH123", "TALON11"),
        )]
        callword_rows = [(
            "TALON", "found", "https://wwi/TALON", datetime(2024, 1, 1, tzinfo=timezone.utc),
            {"types": [{"name": "H60", "count": 27}]},
        )]
        result = aircraft_log.detail("A01234", metadata_rows, talon_sighting_rows, None, self.types,
                                     operator_codes={"RCH": "Air Mobility Command"},
                                     callword_rows=callword_rows)
        lookups = result["military"]["callwords"]
        self.assertEqual(lookups[0]["type_match"], {"name": "H60", "count": 27})
        self.assertEqual(lookups[0]["callsigns"], ["TALON11"])

    def test_civil_aircraft_has_no_military_section(self):
        result = aircraft_log.detail("A01234", [], self.rch_sighting_rows, None, self.types)
        self.assertIsNone(result["military"])


# --- summarize: military columns ---------------------------------------------

def _row_with_military(pa_operator=None, pa_model=None, pa_type=None, pa_category=None,
                       pa_tags=None, pa_reg=None, operator_code_name=None, **kwargs):
    return _row(**kwargs) + (pa_operator, pa_model, pa_type, pa_category, pa_tags, pa_reg,
                             operator_code_name)


class SummarizeMilitaryColumnsTests(unittest.TestCase):
    def setUp(self):
        self.types = aircraft_log.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_still_accepts_old_20_column_rows(self):
        result = aircraft_log.summarize([_row()], self.types)[0]
        self.assertEqual(result["kind"], "Other")

    def test_plane_alert_columns_produce_military_kind_and_fields(self):
        row = _row_with_military(
            address="A01234", pa_operator="US Air Force", pa_model="C-17A",
            pa_type="C17", pa_category="Military", pa_tags="Cargo", pa_reg="12-3456",
        )
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Military")
        self.assertEqual(result["op"], "US Air Force")
        self.assertEqual(result["model"], "C-17A")

    def test_operator_code_name_column_produces_military_kind(self):
        row = _row_with_military(callsign="RCH123", operator_code_name="Air Mobility Command")
        result = aircraft_log.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Military")
        self.assertEqual(result["op"], "Air Mobility Command")


if __name__ == "__main__":
    unittest.main()
