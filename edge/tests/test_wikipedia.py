import unittest
import urllib.error
import urllib.parse

from sky_scanner import wikipedia


ROW = ('<tr><td>B738</td><td>738</td><td><a href="/wiki/Boeing_737_Next_Generation" '
       'title="Boeing 737 Next Generation">Boeing 737-800</a></td></tr>')


class ParseDesignatorListTests(unittest.TestCase):
    def test_parses_designator_to_title(self):
        result = wikipedia.parse_designator_list(f"<table>{ROW}</table>")
        self.assertEqual(result, {"B738": "Boeing 737 Next Generation"})

    def test_first_title_per_designator_is_kept(self):
        second = ('<tr><td>B738</td><td>738</td><td><a href="/wiki/Other" '
                  'title="Some Other Title">Other</a></td></tr>')
        result = wikipedia.parse_designator_list(f"<table>{ROW}{second}</table>")
        self.assertEqual(result, {"B738": "Boeing 737 Next Generation"})

    def test_header_row_is_skipped(self):
        header = "<tr><th>ICAO</th><th>IATA</th><th>Aircraft</th></tr>"
        result = wikipedia.parse_designator_list(f"<table>{header}{ROW}</table>")
        self.assertEqual(result, {"B738": "Boeing 737 Next Generation"})

    def test_row_without_link_is_skipped(self):
        no_link = "<tr><td>ABC</td><td>AB</td><td>Just plain text, no anchor</td></tr>"
        result = wikipedia.parse_designator_list(f"<table>{no_link}{ROW}</table>")
        self.assertEqual(result, {"B738": "Boeing 737 Next Generation"})

    def test_ampersand_is_unescaped(self):
        row = ('<tr><td>H60</td><td>H60</td><td><a href="/wiki/Sikorsky" '
               'title="Sikorsky UH-60 &amp; variants">UH-60</a></td></tr>')
        result = wikipedia.parse_designator_list(f"<table>{row}</table>")
        self.assertEqual(result, {"H60": "Sikorsky UH-60 & variants"})


def make_get_json(summary_data=None, search_hits=None, calls=None):
    """A fake get_json: dispatches on 'list=search' vs the summary endpoint."""
    summary_data = summary_data or {}
    calls = calls if calls is not None else []

    def get_json(url):
        calls.append(url)
        if "list=search" in url:
            return {"query": {"search": search_hits or []}}
        title = urllib.parse.unquote(url.rsplit("/", 1)[-1]).replace("_", " ")
        data = summary_data.get(title)
        if data is None:
            raise urllib.error.HTTPError(url, 404, "not found", None, None)
        return data

    return get_json


def summary_payload(description="Boeing 737-800", thumbnail_query=True, original_query=True,
                    doc_type=None, image_domain="commons"):
    thumb = f"https://thumb.wikimedia.org/wikipedia/{image_domain}/thumb/a/ae/X.jpg/330px-X.jpg"
    if thumbnail_query:
        thumb += "?utm_source=test"
    original = f"https://upload.wikimedia.org/wikipedia/{image_domain}/a/ae/X.jpg"
    if original_query:
        original += "?utm_source=test"
    data = {
        "title": "X",
        "description": description,
        "type": doc_type or "standard",
        "thumbnail": {"source": thumb},
        "originalimage": {"source": original},
        "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/X"}},
    }
    return data


class SummaryTests(unittest.TestCase):
    def test_commons_image_strips_query_and_builds_file_url(self):
        get_json = make_get_json({"Boeing 737-800": summary_payload()})
        result = wikipedia.summary("Boeing 737-800", get_json)
        self.assertEqual(result["image_url"],
                         "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ae/X.jpg/330px-X.jpg")
        self.assertEqual(result["file_url"], "https://commons.wikimedia.org/wiki/File:X.jpg")
        self.assertEqual(result["article_url"], "https://en.wikipedia.org/wiki/X")

    def test_non_commons_image_gives_none_image_and_file_url(self):
        get_json = make_get_json({"X": summary_payload(image_domain="en")})
        result = wikipedia.summary("X", get_json)
        self.assertIsNone(result["image_url"])
        self.assertIsNone(result["file_url"])

    def test_disambiguation_returns_none(self):
        get_json = make_get_json({"X": summary_payload(doc_type="disambiguation")})
        self.assertIsNone(wikipedia.summary("X", get_json))

    def test_http_404_returns_none(self):
        get_json = make_get_json({})
        self.assertIsNone(wikipedia.summary("Missing Article", get_json))

    def test_other_http_error_is_reraised(self):
        def get_json(url):
            raise urllib.error.HTTPError(url, 500, "server error", None, None)

        with self.assertRaises(urllib.error.HTTPError):
            wikipedia.summary("X", get_json)


class ResolveTests(unittest.TestCase):
    def test_designator_in_list_uses_designator_list_method_without_search(self):
        calls = []
        get_json = make_get_json(
            {"Boeing 737 Next Generation": summary_payload()}, calls=calls)
        designators = {"B738": "Boeing 737 Next Generation"}
        method, result = wikipedia.resolve("B738", "Boeing 737-800", designators, get_json)
        self.assertEqual(method, "designator_list")
        self.assertIsNotNone(result)
        self.assertFalse(any("list=search" in url for url in calls))

    def test_designator_not_in_list_uses_search(self):
        calls = []
        get_json = make_get_json(
            {"Cessna 172": summary_payload(description="single-engine light aircraft")},
            search_hits=[{"title": "Cessna 172"}], calls=calls)
        method, result = wikipedia.resolve("C172", "Cessna 172", {}, get_json)
        self.assertEqual(method, "search")
        self.assertIsNotNone(result)
        self.assertTrue(any("list=search" in url for url in calls))

    def test_search_hit_without_aircraft_words_is_rejected(self):
        get_json = make_get_json(
            {"Nirvana": summary_payload(description="American rock band")},
            search_hits=[{"title": "Nirvana"}])
        method, result = wikipedia.resolve("XXXX", "Nirvana", {}, get_json)
        self.assertEqual(method, "search")
        self.assertIsNone(result)

    def test_business_jet_description_is_accepted(self):
        get_json = make_get_json(
            {"Bombardier Challenger 300": summary_payload(
                description="Business jet made by Bombardier")},
            search_hits=[{"title": "Bombardier Challenger 300"}])
        method, result = wikipedia.resolve("CL30", "Challenger 300", {}, get_json)
        self.assertEqual(method, "search")
        self.assertIsNotNone(result)

    def test_model_name_with_slash_uses_part_before_slash(self):
        calls = []
        get_json = make_get_json(
            {"Airbus A320": summary_payload(description="Airbus A320 family airliner")},
            search_hits=[{"title": "Airbus A320"}], calls=calls)
        wikipedia.resolve("A320", "A320/A321", {}, get_json)
        search_calls = [c for c in calls if "list=search" in c]
        self.assertEqual(len(search_calls), 1)
        self.assertIn("A320", urllib.parse.unquote_plus(search_calls[0]))
        self.assertNotIn("A320%2FA321", search_calls[0])
        self.assertNotIn("A321", urllib.parse.unquote_plus(search_calls[0]).split("aircraft")[0])

    def test_empty_model_returns_search_none_without_any_call(self):
        calls = []
        get_json = make_get_json(calls=calls)
        method, result = wikipedia.resolve("ZZZZ", "", {}, get_json)
        self.assertEqual((method, result), ("search", None))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
