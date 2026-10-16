import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish  # noqa: E402


class AuthorTests(unittest.TestCase):
    def test_first_link_text_is_the_credit(self):
        html = ('<a href="//commons.wikimedia.org/wiki/User:Fir0002">fir0002</a> | flagstaffotos [at] '
                'gmail.com<br>Canon 20D + Canon 400mm f/5.6 L')
        self.assertEqual(publish.author(html), "fir0002")

    def test_email_is_removed_from_plain_text(self):
        self.assertEqual(publish.author("Jane Doe jane@example.org"), "Jane Doe")

    def test_first_line_only(self):
        self.assertEqual(publish.author("Mike Burdett<br />from Bristol"), "Mike Burdett")


if __name__ == "__main__":
    unittest.main()
