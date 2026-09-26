import tempfile
import unittest
from pathlib import Path
import sys

DECODER_DIR = Path(__file__).parents[1] / "decoder"
sys.path.insert(0, str(DECODER_DIR))
from patch_readsb import patch


class ReadsbPatchTests(unittest.TestCase):
    def test_patch_copies_header_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            source = source_dir / "net_io.c"
            source.write_text(
                '#include "readsb.h"\n\n'
                "static void outputMessage(struct modesMessage *mm) {\n"
                "    return;\n}\n"
            )

            patch(source_dir)
            first_result = source.read_text()
            self.assertIn('#include "readsb_event.h"', first_result)
            self.assertIn("    sky_emit(mm);\n", first_result)
            self.assertEqual(
                (source_dir / "readsb_event.h").read_bytes(),
                DECODER_DIR.joinpath("readsb_event.h").read_bytes(),
            )

            patch(source_dir)
            self.assertEqual(source.read_text(), first_result)

    def test_patch_refuses_changed_anchor_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            source = source_dir / "net_io.c"
            source.write_text("/* changed readsb source */\n")

            with self.assertRaises(SystemExit):
                patch(source_dir)
            self.assertEqual(source.read_text(), "/* changed readsb source */\n")


if __name__ == "__main__":
    unittest.main()
