import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.fmt import ago, present  # noqa: E402


class PresentTests(unittest.TestCase):
    def test_missing_values(self):
        for value in (None, float("nan"), pd.NA, pd.NaT, np.nan, ""):
            self.assertFalse(present(value), repr(value))

    def test_real_values(self):
        for value in (0, 1975, "N1", ["military_block"], np.array([1090]), False):
            self.assertTrue(present(value), repr(value))

    def test_nullable_integer_column(self):
        # BigQuery INT64 columns arrive as pandas Int64 with pd.NA for null.
        years = pd.Series([1975, None], dtype="Int64")
        self.assertTrue(present(years.iloc[0]))
        self.assertFalse(present(years.iloc[1]))


class AgoTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(ago(30), "30 s ago")
        self.assertEqual(ago(600), "10 min ago")
        self.assertEqual(ago(7200), "2 h ago")
        self.assertEqual(ago(None), "never")


if __name__ == "__main__":
    unittest.main()
