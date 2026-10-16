import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verifier  # noqa: E402

CALLS = {
    "c1": {"confirmed_aircraft": 2985, "snapshots": 53616, "first_report": "2026-09-26T17:56:43.758000+00:00"},
    "c2": {
        "rows": [
            {
                "registration": "N273RH",
                "time_pt": "03:06",
                "max_altitude_baro_ft": 14000.0,
                "callsigns": ["JANET46", "JANET43"],
            }
        ]
    },
    "c3": [{"type_name": "Boeing E-6 Mercury", "wikipedia_description": "Airborne command post"}],
}


def statement(text, *evidence):
    return {"text": text, "evidence": [{"call": c, "path": p, "value": v} for c, p, v in evidence]}


class PathTests(unittest.TestCase):
    def test_resolves_keys_and_indexes(self):
        self.assertEqual(verifier.resolve(CALLS["c2"], "rows[0].time_pt"), "03:06")
        self.assertEqual(verifier.resolve(CALLS["c3"], "[0].type_name"), "Boeing E-6 Mercury")

    def test_rejects_bad_paths(self):
        for path in ("rows[5].time_pt", "nope", "rows[0]..x", ""):
            with self.assertRaises(verifier.Missing):
                verifier.resolve(CALLS["c2"], path)


class StatementTests(unittest.TestCase):
    def check(self, s):
        return verifier.check_statement(s, CALLS)

    def test_accepts_correct_citations_with_formatting(self):
        ok, why = self.check(
            statement(
                "The receiver logged 2,985 aircraft and 53,616 snapshots.",
                ("c1", "confirmed_aircraft", 2985),
                ("c1", "snapshots", "53,616"),
            )
        )
        self.assertTrue(ok, why)

    def test_accepts_a_pacific_time_for_a_utc_timestamp(self):
        ok, why = self.check(statement("Data starts at 10:56 am on 26 September.", ("c1", "first_report", "10:56 am")))
        self.assertTrue(ok, why)

    def test_accepts_a_value_from_a_list(self):
        ok, why = self.check(statement("It flew as JANET46.", ("c2", "rows[0].callsigns", "JANET46")))
        self.assertTrue(ok, why)

    def test_accepts_numbers_written_as_words_and_full_precision(self):
        calls = {"c1": {"aircraft": 7, "positions": 0, "visits": 1, "kt": 454.881866}}
        for text, path, value in [
            ("Seven aircraft flew.", "aircraft", 7),
            ("It reported no position.", "positions", 0),
            ("It was heard once.", "visits", 1),
            ("It flew at 454.881866 kt.", "kt", 454.881866),
            ("It flew at 455 kt.", "kt", 454.881866),
        ]:
            ok, why = verifier.check_statement(statement(text, ("c1", path, value)), calls)
            self.assertTrue(ok, f"{text}: {why}")

    def test_accepts_a_paraphrased_reason(self):
        calls = {"c1": {"reasons": ["Only Boeing P-8A Poseidon heard here"]}}
        ok, why = verifier.check_statement(
            statement(
                "It is the only Boeing P-8A Poseidon this station has heard.",
                ("c1", "reasons[0]", "Only Boeing P-8A Poseidon heard here"),
            ),
            calls,
        )
        self.assertTrue(ok, why)

    def test_still_rejects_a_number_that_is_absent(self):
        calls = {"c1": {"aircraft": 7}}
        self.assertFalse(verifier.check_statement(statement("Many aircraft flew.", ("c1", "aircraft", 7)), calls)[0])

    def test_rejects_a_wrong_value(self):
        ok, why = self.check(statement("It climbed to 41,000 feet.", ("c2", "rows[0].max_altitude_baro_ft", 41000)))
        self.assertFalse(ok)
        self.assertIn("14000", why)

    def test_rejects_a_value_missing_from_the_text(self):
        ok, why = self.check(statement("It climbed steeply.", ("c2", "rows[0].max_altitude_baro_ft", 14000)))
        self.assertFalse(ok)
        self.assertIn("not in the sentence", why)

    def test_rejects_uncited_and_unknown_calls(self):
        self.assertFalse(self.check({"text": "A fact."})[0])
        self.assertFalse(self.check(statement("A fact 3.", ("c9", "x", 3)))[0])


class EditionTests(unittest.TestCase):
    def test_removes_failed_statements_and_empty_entries(self):
        draft = {
            "title": "T",
            "summary": [
                statement("2,985 aircraft.", ("c1", "confirmed_aircraft", 2985)),
                statement("3,000 aircraft.", ("c1", "confirmed_aircraft", 3000)),
            ],
            "sections": [
                {
                    "heading": "Military",
                    "intro": [],
                    "entries": [
                        {
                            "title": "E-6",
                            "designator": "E6",
                            "addresses": ["AF1D5B"],
                            "what_it_is": statement(
                                "An airborne command post.",
                                ("c3", "[0].wikipedia_description", "Airborne command post"),
                            ),
                            "details": [
                                statement("Seen at 99,999 feet.", ("c2", "rows[0].max_altitude_baro_ft", 99999))
                            ],
                        }
                    ],
                }
            ],
            "privacy": [],
            "caveats": [],
        }
        edition, report = verifier.verify(draft, CALLS)
        self.assertEqual([s["text"] for s in edition["summary"]], ["2,985 aircraft."])
        self.assertEqual(edition["sections"], [])
        self.assertEqual(report["checked"], 4)
        self.assertEqual(report["verified"], 2)


class TitleTests(unittest.TestCase):
    calls = {
        "c1": [
            {"address": "AF1D5B", "type_name": "Boeing E-6 Mercury"},
            {"address": "A2B320", "type_name": "Boeing 737-600"},
        ]
    }

    def test_keeps_a_title_containing_the_type_name(self):
        names = verifier.type_names(self.calls)
        title, fix = verifier.checked_title({"title": "Boeing 737-600 (JANET fleet)", "addresses": ["A2B320"]}, names)
        self.assertEqual((title, fix), ("Boeing 737-600 (JANET fleet)", None))

    def test_replaces_a_title_the_data_does_not_support(self):
        names = verifier.type_names(self.calls)
        title, fix = verifier.checked_title({"title": "Boeing E-6B Mercury", "addresses": ["AF1D5B"]}, names)
        self.assertEqual(title, "Boeing E-6 Mercury")
        self.assertIn("replaced", fix)


if __name__ == "__main__":
    unittest.main()


class TidyTests(unittest.TestCase):
    def test_iso_timestamp_in_parentheses_is_removed(self):
        text = "Last seen at 2:37 pm on Thursday 15 October (2026-10-15T21:37:46.452000+00:00)."
        self.assertEqual(verifier.tidy(text), "Last seen at 2:37 pm on Thursday 15 October.")

    def test_other_parentheses_are_kept(self):
        self.assertEqual(verifier.tidy("HC-130J 09-0108 (AE29D8) climbed"), "HC-130J 09-0108 (AE29D8) climbed")
