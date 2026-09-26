import io
import json
import unittest
import zipfile
from unittest.mock import patch

from sky_scanner.enrich_worker import DOWNLOAD_HEADERS, DOWNLOAD_URL, EnrichmentWorker, download_registry


def registry_zip(n=2):
    master = ["N-NUMBER,Mfr Mdl Code,Registrant Name,Mode S Code Hex"]
    for i in range(n):
        master.append(f"{1000 + i},0000001,Owner {i},{i + 1:06X}")
    ref = "Code,Manufacturer,Model,Type Aircraft\n0000001,Maker,Model,4\n"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("MASTER.txt", "\n".join(master) + "\n")
        zf.writestr("ACFTREF.txt", ref)
    return archive.getvalue()


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def execute(self, sql, params=()):
        self.connection.queries.append((sql, params))
        if "SELECT EXISTS" in sql:
            self.result = (self.connection.exists,)
        else:
            self.result = (1,)

    def fetchone(self):
        return self.result

    def close(self):
        pass


class Connection:
    def __init__(self, exists=False):
        self.exists = exists
        self.queries = []
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


class Storage:
    def __init__(self, exists=False):
        self.connection = Connection(exists)
        self.saved = []

    def save_metadata_import(self, records, **kwargs):
        self.saved.append((records, kwargs))
        return "import-uuid"


class EnrichmentWorkerTests(unittest.TestCase):
    @patch("sky_scanner.enrich_worker.urllib.request.urlopen")
    def test_download_uses_faa_page_referrer_and_bounds_read(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, limit):
                self.limit = limit
                return b"zip"

        response = Response()
        urlopen.return_value = response
        self.assertEqual(download_registry(), b"zip")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, DOWNLOAD_URL)
        self.assertEqual(request.get_header("Referer"), DOWNLOAD_HEADERS["Referer"])
        self.assertIn("Sky-Scanner/1.0", request.get_header("User-agent"))
        self.assertEqual(response.limit, 150 * 1024 * 1024 + 1)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    def test_download_failure_never_attempts_import(self):
        storage = Storage()

        def fail_download():
            raise OSError("offline")

        worker = EnrichmentWorker(storage_factory=lambda: storage, download=fail_download, min_records=1, emit=lambda _: None)
        with self.assertRaisesRegex(OSError, "offline"):
            worker.process_once()
        self.assertEqual(storage.saved, [])
        self.assertTrue(storage.connection.closed)

    def test_empty_registry_rejected_without_import(self):
        storage = Storage()
        worker = EnrichmentWorker(
            storage_factory=lambda: storage, download=lambda: registry_zip(0), min_records=1,
            emit=lambda _: None,
        )
        with self.assertRaisesRegex(ValueError, "only 0 records"):
            worker.process_once()
        self.assertEqual(storage.saved, [])

    def test_duplicate_dataset_hash_is_skipped(self):
        storage = Storage(exists=True)
        output = []
        worker = EnrichmentWorker(
            storage_factory=lambda: storage, download=lambda: registry_zip(), min_records=2,
            emit=output.append,
        )
        summary = worker.process_once()
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["records"], 2)
        self.assertEqual(len(summary["dataset_hash"]), 64)
        self.assertEqual(storage.saved, [])
        self.assertNotIn("Owner", output[0])
        self.assertEqual(json.loads(output[0])["status"], "skipped")

    def test_valid_registry_saved_atomically_via_storage_adapter(self):
        storage = Storage()
        worker = EnrichmentWorker(
            storage_factory=lambda: storage, download=lambda: registry_zip(), min_records=2,
            emit=lambda _: None,
        )
        summary = worker.process_once()
        self.assertEqual(summary["status"], "imported")
        self.assertEqual(summary["import_id"], "import-uuid")
        records, kwargs = storage.saved[0]
        self.assertEqual(len(records), 2)
        self.assertEqual(kwargs["source_url"], DOWNLOAD_URL)
        self.assertEqual(kwargs["dataset_hash"], summary["dataset_hash"])


if __name__ == "__main__":
    unittest.main()
