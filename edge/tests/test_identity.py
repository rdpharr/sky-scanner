import unittest
from datetime import UTC, datetime

from sky_scanner import identity

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
        types = identity.parse_doc8643(DOC8643_CSV.encode("utf-8"))
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
                self.assertEqual(identity.faa_type(model), expected)

    def test_unknown_or_empty_returns_none(self):
        self.assertIsNone(identity.faa_type(None))
        self.assertIsNone(identity.faa_type(""))
        self.assertIsNone(identity.faa_type("   "))
        self.assertIsNone(identity.faa_type("SOME-UNKNOWN-MODEL-9999"))


class AircraftClassTests(unittest.TestCase):
    def setUp(self):
        self.types = identity.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_wide_body_jet(self):
        self.assertEqual(identity.aircraft_class("B77W", self.types), "Wide-body jet")

    def test_narrow_body_airliner(self):
        self.assertEqual(identity.aircraft_class("B738", self.types), "Narrow-body / regional jet")

    def test_business_jet_non_airliner(self):
        self.assertEqual(identity.aircraft_class("CL30", self.types), "Business jet")

    def test_piston(self):
        self.assertEqual(identity.aircraft_class("C172", self.types), "Piston")

    def test_turboprop(self):
        self.assertEqual(identity.aircraft_class("PC12", self.types), "Turboprop")

    def test_helicopter(self):
        self.assertEqual(identity.aircraft_class("H500", self.types), "Helicopter")

    def test_none_designator_unidentified(self):
        self.assertEqual(identity.aircraft_class(None, self.types), "Unidentified")

    def test_unknown_designator_starting_b7_is_narrow_body(self):
        self.assertEqual(identity.aircraft_class("B712", self.types), "Narrow-body / regional jet")

    def test_unknown_designator_other(self):
        self.assertEqual(identity.aircraft_class("ZZZZ", self.types), "Other")


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
    first_seen = first_seen or datetime(2024, 1, 1, tzinfo=UTC)
    last_seen = last_seen or datetime(2024, 1, 2, tzinfo=UTC)
    return (
        address,
        address_type,
        first_seen,
        last_seen,
        list(bands),
        snapshots,
        messages,
        max_alt,
        callsign,
        faa_reg,
        faa_mfr,
        faa_model,
        faa_owner,
        os_reg,
        os_model,
        os_type,
        os_operator,
        os_operator_icao,
        os_owner,
        os_country,
    )


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.types = identity.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_faa_row_wins_type_over_opensky(self):
        row = _row(
            faa_reg="N123AB",
            faa_mfr="BOEING",
            faa_model="737-823",
            os_type="C172",  # OpenSky disagrees; FAA model should still win.
        )
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["src"], "F")
        self.assertEqual(result["tc"], "B738")
        self.assertEqual(result["reg"], "N123AB")

    def test_opensky_only_row(self):
        row = _row(
            os_reg="D-ABCD",
            os_model="A320",
            os_type="A320",
            os_operator="Lufthansa",
            os_operator_icao="DLH",
            os_owner="Lufthansa",
            os_country="Germany",
        )
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["src"], "O")
        self.assertEqual(result["tc"], "A320")
        self.assertEqual(result["reg"], "D-ABCD")
        self.assertEqual(result["op"], "Lufthansa")
        self.assertEqual(result["country"], "Germany")

    def test_no_metadata_row(self):
        row = _row()
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["src"], "-")
        self.assertEqual(result["class"], "Unidentified")
        self.assertEqual(result["kind"], "Other")

    def test_band_ordering(self):
        row = _row(bands=(1_090_000_000, 978_000_000))
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["band"], "978,1090")

    def test_iso_datetime_strings(self):
        first_seen = datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
        last_seen = datetime(2024, 5, 2, 8, 0, tzinfo=UTC)
        row = _row(first_seen=first_seen, last_seen=last_seen)
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["first"], first_seen.isoformat())
        self.assertEqual(result["last"], last_seen.isoformat())

    def test_military_by_address_prefix(self):
        row = _row(address="AE1234")
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Military")

    def test_airline_by_callsign(self):
        row = _row(callsign="SWA123")
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Airline")

    def test_airline_by_owner_name(self):
        row = _row(faa_reg="N1", faa_owner="SOME AIRLINES INC")
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Airline")

    def test_airline_narrow_body_owned_by_bank_trustee(self):
        row = _row(
            faa_reg="N1",
            faa_mfr="BOEING",
            faa_model="737-823",
            faa_owner="WELLS FARGO BANK TRUST CO",
        )
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["class"], "Narrow-body / regional jet")
        self.assertEqual(result["kind"], "Airline")

    def test_other_for_cessna_owned_by_llc(self):
        row = _row(faa_reg="N1", faa_mfr="CESSNA", faa_model="172N", faa_owner="SOME LLC")
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["tc"], "C172")
        self.assertEqual(result["class"], "Piston")
        self.assertEqual(result["kind"], "Other")


class SummarySqlTests(unittest.TestCase):
    def test_filters_by_source_name(self):
        self.assertIn("m.source_name = 'faa_registry'", identity.SUMMARY_SQL)
        self.assertIn("m.source_name = 'opensky'", identity.SUMMARY_SQL)

    def test_has_since_parameter(self):
        self.assertIn("%(since)s", identity.SUMMARY_SQL)


class IdentifyTests(unittest.TestCase):
    def setUp(self):
        self.types = identity.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_faa_preferred_over_opensky(self):
        faa = {
            "registration": "N123AB",
            "manufacturer": "BOEING",
            "model": "737-823",
            "registered_owner": "SOME AIRLINE",
        }
        opensky = {
            "registration": "D-ABCD",
            "model": "A320",
            "icao_type_code": "A320",
            "registered_owner": "LUFTHANSA",
            "operator": "Lufthansa",
            "operator_icao": "DLH",
            "country": "Germany",
        }
        result = identity.identify("A1B2C3", "AAL1", faa, opensky, self.types)
        self.assertEqual(result["src"], "F")
        self.assertEqual(result["reg"], "N123AB")
        self.assertEqual(result["tc"], "B738")

    def test_opensky_only_gives_src_o(self):
        opensky = {"registration": "D-ABCD", "model": "A320", "icao_type_code": "A320"}
        result = identity.identify("A1B2C3", None, None, opensky, self.types)
        self.assertEqual(result["src"], "O")
        self.assertEqual(result["reg"], "D-ABCD")

    def test_empty_metadata_gives_dash_and_unidentified(self):
        result = identity.identify("A1B2C3", None, None, None, self.types)
        self.assertEqual(result["src"], "-")
        self.assertEqual(result["class"], "Unidentified")
        self.assertEqual(result["reg"], "")

    def test_military_by_ae_prefix(self):
        result = identity.identify("AE1234", None, None, None, self.types)
        self.assertEqual(result["kind"], "Military")

    def test_non_military_prefix(self):
        result = identity.identify("A01234", None, None, None, self.types)
        self.assertNotEqual(result["kind"], "Military")


class MilitaryEvidenceTests(unittest.TestCase):
    def test_ae_address_gives_us_block_evidence(self):
        evidence = identity.military_evidence("AE1234", None, None)
        self.assertIn("US military ICAO address block", evidence)

    def test_af_address_gives_us_block_evidence(self):
        evidence = identity.military_evidence("AF1234", None, None)
        self.assertIn("US military ICAO address block", evidence)

    def test_non_military_address_and_no_other_signal_is_empty(self):
        self.assertEqual(identity.military_evidence("A01234", None, None), [])

    def test_plane_alert_gives_evidence(self):
        evidence = identity.military_evidence("A01234", {"operator": "US Air Force"}, None)
        self.assertIn("plane-alert-db military list", evidence)

    def test_operator_code_name_gives_evidence(self):
        evidence = identity.military_evidence("A01234", None, "Air Mobility Command")
        self.assertIn("military operator code (Air Mobility Command)", evidence)


# --- identify: military ------------------------------------------------------


class MilitaryIdentifyTests(unittest.TestCase):
    def setUp(self):
        self.types = identity.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_plane_alert_sets_operator_and_model_and_evidence(self):
        plane_alert = {"operator": "United States Navy", "model": "P-8A Poseidon", "icao_type_code": "P8"}
        result = identity.identify("A01234", None, None, None, self.types, plane_alert=plane_alert)
        self.assertIn("plane-alert-db military list", result["military_evidence"])
        self.assertEqual(result["op"], "United States Navy")
        self.assertEqual(result["model"], "P-8A Poseidon")
        self.assertEqual(result["kind"], "Military")

    def test_operator_code_name_evidence_and_operator(self):
        result = identity.identify(
            "A01234", "RCH123", None, None, self.types, operator_code_name="Air Mobility Command"
        )
        self.assertIn("military operator code (Air Mobility Command)", result["military_evidence"])
        self.assertEqual(result["op"], "Air Mobility Command")
        self.assertEqual(result["kind"], "Military")

    def test_military_is_never_classified_airline_despite_airline_callsign(self):
        result = identity.identify("AE1234", "AAL1", None, None, self.types)
        self.assertEqual(result["kind"], "Military")

    def test_non_military_civil_aircraft_has_empty_evidence(self):
        result = identity.identify("A01234", "N12345", None, None, self.types)
        self.assertEqual(result["military_evidence"], [])
        self.assertNotEqual(result["kind"], "Military")

    def test_coast_guard_callsign_on_us_military_address_adds_evidence(self):
        result = identity.identify("AE26B7", "C6597", None, None, self.types)
        self.assertIn("callsign convention (US Coast Guard (aircraft number))", result["military_evidence"])


# --- detail: military ---------------------------------------------------------


def _row_with_military(
    pa_operator=None,
    pa_model=None,
    pa_type=None,
    pa_category=None,
    pa_tags=None,
    pa_reg=None,
    operator_code_name=None,
    **kwargs,
):
    return _row(**kwargs) + (pa_operator, pa_model, pa_type, pa_category, pa_tags, pa_reg, operator_code_name)


class SummarizeMilitaryColumnsTests(unittest.TestCase):
    def setUp(self):
        self.types = identity.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_still_accepts_old_20_column_rows(self):
        result = identity.summarize([_row()], self.types)[0]
        self.assertEqual(result["kind"], "Other")

    def test_plane_alert_columns_produce_military_kind_and_fields(self):
        row = _row_with_military(
            address="A01234",
            pa_operator="US Air Force",
            pa_model="C-17A",
            pa_type="C17",
            pa_category="Military",
            pa_tags="Cargo",
            pa_reg="12-3456",
        )
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Military")
        self.assertEqual(result["op"], "US Air Force")
        self.assertEqual(result["model"], "C-17A")

    def test_operator_code_name_column_produces_military_kind(self):
        row = _row_with_military(callsign="RCH123", operator_code_name="Air Mobility Command")
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["kind"], "Military")
        self.assertEqual(result["op"], "Air Mobility Command")


# --- summarize/identify: tar1090-db columns and source precedence ------------


def _row_with_tar1090(t_reg=None, t_type=None, t_model=None, t_owner=None, t_tags=None, **kwargs):
    return _row_with_military(**kwargs) + (t_reg, t_type, t_model, t_owner, t_tags)


class SummarizeTar1090ColumnsTests(unittest.TestCase):
    def setUp(self):
        self.types = identity.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_32_column_row_with_tar1090_fields(self):
        row = _row_with_tar1090(
            address="A01234", t_reg="N1AB", t_type="B738", t_model="Boeing 737-800", t_owner="ACME LLC"
        )
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["src"], "T")
        self.assertEqual(result["reg"], "N1AB")
        self.assertEqual(result["tc"], "B738")

    def test_tar1090_military_tag_adds_evidence_and_military_kind(self):
        row = _row_with_tar1090(
            address="A01234", t_reg="63-8146", t_type="T38", t_model="NORTHROP T-38 Talon", t_tags="military"
        )
        result = identity.summarize([row], self.types)[0]
        self.assertIn("tar1090-db military flag", result["military_evidence"])
        self.assertEqual(result["kind"], "Military")

    def test_still_accepts_27_column_rows_without_tar1090(self):
        row = _row_with_military(address="A01234")
        result = identity.summarize([row], self.types)[0]
        self.assertEqual(result["src"], "-")

    def test_unconfirmed_true_below_two_messages(self):
        row = _row(messages=1)
        result = identity.summarize([row], self.types)[0]
        self.assertTrue(result["unconfirmed"])

    def test_unconfirmed_false_at_two_messages(self):
        row = _row(messages=2)
        result = identity.summarize([row], self.types)[0]
        self.assertFalse(result["unconfirmed"])


class Tar1090IdentifyTests(unittest.TestCase):
    def setUp(self):
        self.types = identity.parse_doc8643(DOC8643_CSV.encode("utf-8"))

    def test_tar1090_reg_or_type_gives_src_t_when_no_faa(self):
        tar1090 = {"registration": "N1AB", "icao_type_code": "B738"}
        result = identity.identify("A01234", None, None, None, self.types, tar1090=tar1090)
        self.assertEqual(result["src"], "T")
        self.assertEqual(result["reg"], "N1AB")
        self.assertEqual(result["tc"], "B738")

    def test_tar1090_type_preferred_over_opensky_type(self):
        opensky = {"icao_type_code": "C172", "registration": "D-ABCD"}
        tar1090 = {"icao_type_code": "B738"}
        result = identity.identify("A01234", None, None, opensky, self.types, tar1090=tar1090)
        self.assertEqual(result["tc"], "B738")
        self.assertEqual(result["src"], "T")

    def test_tar1090_reg_preferred_over_opensky_reg(self):
        opensky = {"registration": "D-ABCD"}
        tar1090 = {"registration": "N1AB", "icao_type_code": "B738"}
        result = identity.identify("A01234", None, None, opensky, self.types, tar1090=tar1090)
        self.assertEqual(result["reg"], "N1AB")

    def test_faa_reg_wins_over_tar1090(self):
        faa = {"registration": "N999FA", "model": "737-823", "manufacturer": "BOEING"}
        tar1090 = {"registration": "N1AB", "icao_type_code": "B738"}
        result = identity.identify("A01234", None, faa, None, self.types, tar1090=tar1090)
        self.assertEqual(result["src"], "F")
        self.assertEqual(result["reg"], "N999FA")

    def test_only_opensky_gives_src_o(self):
        opensky = {"registration": "D-ABCD", "icao_type_code": "A320"}
        result = identity.identify("A01234", None, None, opensky, self.types)
        self.assertEqual(result["src"], "O")

    def test_only_plane_alert_gives_src_m(self):
        plane_alert = {"registration": "12-3456", "model": "C-17A"}
        result = identity.identify("A01234", None, None, None, self.types, plane_alert=plane_alert)
        self.assertEqual(result["src"], "M")

    def test_nothing_gives_dash(self):
        result = identity.identify("A01234", None, None, None, self.types)
        self.assertEqual(result["src"], "-")

    def test_owner_precedence_faa_over_tar1090_over_opensky(self):
        faa = {"registered_owner": "FAA OWNER"}
        tar1090 = {"registered_owner": "TAR OWNER"}
        opensky = {"registered_owner": "OS OWNER"}
        result = identity.identify("A01234", None, faa, opensky, self.types, tar1090=tar1090)
        self.assertEqual(result["owner"], "FAA OWNER")

    def test_owner_tar1090_over_opensky_when_no_faa(self):
        tar1090 = {"registered_owner": "TAR OWNER"}
        opensky = {"registered_owner": "OS OWNER"}
        result = identity.identify("A01234", None, None, opensky, self.types, tar1090=tar1090)
        self.assertEqual(result["owner"], "TAR OWNER")

    def test_tar1090_military_tag_adds_evidence(self):
        tar1090 = {"tags": "military"}
        result = identity.identify("A01234", None, None, None, self.types, tar1090=tar1090)
        self.assertIn("tar1090-db military flag", result["military_evidence"])

    def test_miscode_tags_gives_miscode_true_and_special_model(self):
        tar1090 = {"tags": "miscode"}
        result = identity.identify("000FFF", None, None, None, self.types, tar1090=tar1090)
        self.assertTrue(result["miscode"])
        self.assertEqual(result["model"], "Invalid transponder address (miscode)")
        self.assertEqual(result["src"], "-")
        self.assertEqual(result["military_evidence"], [])

    def test_miscode_via_registered_owner_text_with_military_tag(self):
        tar1090 = {"registered_owner": "Miscode - VARIOUS", "tags": "military"}
        result = identity.identify("000FFF", None, None, None, self.types, tar1090=tar1090)
        self.assertTrue(result["miscode"])
        self.assertEqual(result["model"], "Invalid transponder address (miscode)")
        self.assertNotIn("tar1090-db military flag", result["military_evidence"])
