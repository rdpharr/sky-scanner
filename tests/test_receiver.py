import json
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, Mock, call, patch

from sky_scanner.receiver import EventSender, capture, decoder_command, normalize_readsb


class ReadsbNormalizationTests(unittest.TestCase):
    def test_flat_per_message_event_becomes_snapshot_observation(self):
        observation = normalize_readsb(
            {
                "received_at": 1727000000.125,
                "address": "00a123",
                "address_type": "adsr_icao",
                "origin": "rebroadcast",
                "source": "adsr",
                "message_type": 18,
                "message_bits": 112,
                "corrected_bits": 0,
                "callsign": "N123AB",
                "altitude_baro_ft": 0,
                "ground_speed_kt": 91.2,
            },
            "rx-1",
        )

        self.assertEqual(observation["received_at"], 1727000000.125)
        self.assertEqual(observation["receiver_id"], "rx-1")
        self.assertEqual(observation["frequency_hz"], 1090000000)
        self.assertEqual(observation["address"], "00A123")
        self.assertEqual(observation["address_type"], "icao")
        self.assertEqual(observation["source"], "rebroadcast")
        self.assertEqual(observation["fields"]["altitude_baro_ft"], 0)
        self.assertEqual(observation["fields"]["callsign"], "N123AB")
        self.assertEqual(observation["fields"]["readsb_address_type"], "adsr_icao")
        self.assertEqual(observation["fields"]["readsb_source"], "adsr")
        self.assertEqual(observation["fields"]["message_type"], 18)

    def test_non_icao_and_unrecognized_types_keep_distinct_identity(self):
        non_icao = normalize_readsb(
            {"received_at": 1.0, "address": "abc123", "address_type": "adsb_other",
             "address_non_icao": True},
            "rx",
        )
        unknown = normalize_readsb(
            {"received_at": 1.0, "address": "abc123", "address_type": "other"},
            "rx",
        )

        self.assertEqual(non_icao["address_type"], "readsb:adsb_other")
        self.assertEqual(unknown["address_type"], "readsb:other")
        self.assertNotEqual(non_icao["address_type"], unknown["address_type"])

    def test_icao_origins_share_icao_identity_and_explicit_bit_wins(self):
        for decoder_type in ("icao", "icao_nontransponder", "mode_s", "adsr_icao", "tisb_icao"):
            with self.subTest(decoder_type=decoder_type):
                row = normalize_readsb(
                    {"received_at": 1, "address": "123456", "address_type": decoder_type,
                     "address_non_icao": False},
                    "rx",
                )
                self.assertEqual(row["address_type"], "icao")
                self.assertEqual(row["fields"]["readsb_address_type"], decoder_type)
        explicit_non_icao = normalize_readsb(
            {"received_at": 1, "address": "123456", "address_type": "adsr_icao",
             "address_non_icao": True},
            "rx",
        )
        self.assertEqual(explicit_non_icao["address_type"], "readsb:adsr_icao")

    def test_missing_or_invalid_identity_and_time_are_rejected(self):
        for payload in (
            {"address": "123456"},
            {"received_at": "nan", "address": "123456"},
            {"received_at": 1, "address": "not-hex"},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(normalize_readsb(payload, "rx"))

    def test_garbage_flagged_messages_are_rejected(self):
        payload = {"received_at": 1, "address": "123456", "address_type": "icao",
                   "garbage": True}
        self.assertIsNone(normalize_readsb(payload, "rx"))

    def test_position_bad_strips_latitude_and_longitude_but_keeps_other_fields(self):
        observation = normalize_readsb(
            {"received_at": 1, "address": "123456", "address_type": "icao",
             "position_bad": True, "latitude": 37.5, "longitude": -122.2,
             "altitude_baro_ft": 1000},
            "rx",
        )
        self.assertNotIn("latitude", observation["fields"])
        self.assertNotIn("longitude", observation["fields"])
        self.assertEqual(observation["fields"]["altitude_baro_ft"], 1000)

    def test_position_bad_false_keeps_latitude_and_longitude(self):
        observation = normalize_readsb(
            {"received_at": 1, "address": "123456", "address_type": "icao",
             "position_bad": False, "latitude": 37.5, "longitude": -122.2},
            "rx",
        )
        self.assertEqual(observation["fields"]["latitude"], 37.5)
        self.assertEqual(observation["fields"]["longitude"], -122.2)

    def test_integer_squawk_becomes_zero_padded_four_char_string(self):
        for raw, expected in ((123, "0123"), (7700, "7700"), (0, "0000"), (7777, "7777")):
            with self.subTest(raw=raw):
                observation = normalize_readsb(
                    {"received_at": 1, "address": "123456", "address_type": "icao",
                     "squawk": raw},
                    "rx",
                )
                self.assertEqual(observation["fields"]["squawk"], expected)

    def test_squawk_outside_octal_range_or_non_int_is_left_untouched(self):
        out_of_range = normalize_readsb(
            {"received_at": 1, "address": "123456", "address_type": "icao", "squawk": 7778},
            "rx",
        )
        already_string = normalize_readsb(
            {"received_at": 1, "address": "123456", "address_type": "icao", "squawk": "1200"},
            "rx",
        )
        self.assertEqual(out_of_range["fields"]["squawk"], 7778)
        self.assertEqual(already_string["fields"]["squawk"], "1200")


class EventSenderTests(unittest.TestCase):
    def _make_response(self):
        response = MagicMock()
        response.status = 202
        response.__enter__.return_value = response
        return response

    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_4xx_drops_batch_without_retry_and_next_batch_is_still_delivered(self):
        http_error = urllib.error.HTTPError("http://ingest.test/events", 400,
                                             "bad request", {}, None)
        success = self._make_response()
        with patch("sky_scanner.receiver.urllib.request.urlopen",
                   side_effect=[http_error, success]) as urlopen:
            sender = EventSender("http://ingest.test/events")
            try:
                sender.put({"id": "1"})
                self.assertTrue(self._wait_for(lambda: urlopen.call_count >= 1))
                # The rejected batch must be cleared, not retried, so the queue
                # keeps draining and the next batch gets its own urlopen call.
                sender.put({"id": "2"})
                self.assertTrue(self._wait_for(lambda: urlopen.call_count >= 2))
                self.assertTrue(sender.thread.is_alive())
            finally:
                sender.stop.set()
                sender.thread.join(timeout=2)
        self.assertEqual(urlopen.call_count, 2)

    def test_5xx_retries_same_batch_after_wait_then_delivers(self):
        server_error = urllib.error.HTTPError("http://ingest.test/events", 503,
                                               "unavailable", {}, None)
        success = self._make_response()
        with patch("sky_scanner.receiver.urllib.request.urlopen",
                   side_effect=[server_error, success]) as urlopen:
            sender = EventSender("http://ingest.test/events")
            # Skip the real 2-second backoff so the test stays fast; the
            # production wait duration is exercised via the call arguments.
            sender.stop.wait = Mock(wraps=lambda *_a, **_k: False)
            try:
                sender.put({"id": "1"})
                self.assertTrue(self._wait_for(lambda: urlopen.call_count >= 2))
            finally:
                sender.stop.set()
                sender.thread.join(timeout=2)
        self.assertEqual(urlopen.call_count, 2)
        sender.stop.wait.assert_any_call(2)


class CaptureTests(unittest.TestCase):
    class _Selector:
        def __init__(self):
            self.registered = []

        def register(self, fileobj, events):
            self.registered.append((fileobj, events))

        def select(self, timeout=None):
            return [(Mock(fd=17), 1)]

        def close(self):
            pass

    class _Process:
        def __init__(self):
            self.stdout = Mock()
            self.returncode = None
            self.signals = []

        def poll(self):
            return self.returncode

        def send_signal(self, signum):
            self.signals.append(signum)
            self.returncode = -signum

        def wait(self, timeout=None):
            return self.returncode

    def test_decoder_commands_keep_one_hackrf_owner_with_band_specific_decoders(self):
        with patch.dict("os.environ", {"HACKRF_SERIAL": "serial-7"}, clear=False):
            command1090 = decoder_command(1090000000)
            command978 = decoder_command(978000000)

        self.assertIn("hackrf", command1090)
        self.assertIn("--device", command1090)
        self.assertIn("serial-7", command1090)
        self.assertIn("--sdr", command978)
        self.assertIn("driver=hackrf,serial=serial-7", command978)
        self.assertIn("--json-stdout", command978)

    def test_capture_emits_decoded_event_and_interval_then_closes_process(self):
        process = self._Process()
        raw = {"received_at": 10.25, "address": "abcdef", "address_type": "icao"}
        encoded = json.dumps(raw).encode() + b"\n"
        emitted = []
        selector = self._Selector()

        with (
            patch("sky_scanner.receiver.subprocess.Popen", return_value=process) as popen,
            patch("sky_scanner.receiver.selectors.DefaultSelector", return_value=selector),
            patch("sky_scanner.receiver.os.read", side_effect=[encoded, b""]),
            patch("sky_scanner.receiver.time.monotonic", side_effect=[0, 0, 0]),
            patch("sky_scanner.receiver.time.time", side_effect=[100.0, 100.5, 101.0]),
            patch.dict("os.environ", {"RECEIVER_ID": "rx"}, clear=False),
        ):
            count = capture(1090000000, 10, emitted.append, Mock(is_set=Mock(return_value=False)))

        self.assertEqual(count, 1)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(process.signals, [15])
        self.assertTrue(process.stdout.close.called)
        self.assertEqual(emitted[0]["type"], "observation")
        self.assertEqual(emitted[0]["data"]["frequency_hz"], 1090000000)
        self.assertEqual(emitted[0]["data"]["received_at"], 10.25)
        self.assertEqual(emitted[1]["type"], "interval")
        self.assertEqual(emitted[1]["data"]["sample_config"]["decoded_messages"], 1)
        self.assertEqual(emitted[1]["data"]["sample_config"]["exit_code"], -15)

    def test_band_turns_do_not_overlap_and_previous_process_is_closed_first(self):
        first, second = self._Process(), self._Process()
        processes = [first, second]
        emitted = []
        selectors = [self._Selector(), self._Selector()]
        encoded = json.dumps({"received_at": 10, "address": "123456", "address_type": "icao"}).encode() + b"\n"
        encoded_uat = json.dumps({"address_qualifier": "adsb_icao", "address": "123456",
                                  "metadata": {"received_at": 10}}).encode() + b"\n"
        popen_calls = []

        def start_process(*args, **kwargs):
            if popen_calls:
                self.assertIsNotNone(first.returncode, "1090 process must exit before 978 process starts")
            popen_calls.append((args, kwargs))
            return processes[len(popen_calls) - 1]

        with (
            patch("sky_scanner.receiver.subprocess.Popen", side_effect=start_process),
            patch("sky_scanner.receiver.selectors.DefaultSelector", side_effect=selectors),
            patch("sky_scanner.receiver.os.read", side_effect=[encoded, b"", encoded_uat, b""]),
            patch("sky_scanner.receiver.time.monotonic", side_effect=[0] * 12),
            patch("sky_scanner.receiver.time.time", side_effect=[100, 100.5, 101, 102, 102.5, 103]),
            patch.dict("os.environ", {"RECEIVER_ID": "rx"}, clear=False),
        ):
            self.assertEqual(capture(1090000000, 10, emitted.append, Mock(is_set=Mock(return_value=False))), 1)
            self.assertEqual(capture(978000000, 10, emitted.append, Mock(is_set=Mock(return_value=False))), 1)

        self.assertEqual(len(popen_calls), 2)
        self.assertEqual([event["type"] for event in emitted], ["observation", "interval", "observation", "interval"])
        self.assertTrue(first.stdout.close.called)
        self.assertTrue(second.stdout.close.called)

    def test_stdout_failure_terminates_process_and_still_emits_interval(self):
        process = self._Process()
        emitted = []
        selector = self._Selector()
        with (
            patch("sky_scanner.receiver.subprocess.Popen", return_value=process),
            patch("sky_scanner.receiver.selectors.DefaultSelector", return_value=selector),
            patch("sky_scanner.receiver.os.read", side_effect=OSError("decoder pipe failed")),
            patch("sky_scanner.receiver.time.monotonic", side_effect=[0, 0]),
            patch("sky_scanner.receiver.time.time", side_effect=[100, 100.5, 101]),
        ):
            with self.assertRaisesRegex(OSError, "decoder pipe failed"):
                capture(1090000000, 10, emitted.append, Mock(is_set=Mock(return_value=False)))

        self.assertEqual(process.signals, [15])
        self.assertTrue(process.stdout.close.called)
        self.assertEqual([event["type"] for event in emitted], ["interval"])

    def test_decoder_nonzero_exit_still_emits_interval_and_no_observation(self):
        process = self._Process()
        process.returncode = 2
        emitted = []
        selector = self._Selector()
        selector.select = Mock(return_value=[])

        with (
            patch("sky_scanner.receiver.subprocess.Popen", return_value=process),
            patch("sky_scanner.receiver.selectors.DefaultSelector", return_value=selector),
            patch("sky_scanner.receiver.time.monotonic", side_effect=[0, 0]),
            patch("sky_scanner.receiver.time.time", side_effect=[100.0, 101.0]),
        ):
            count = capture(978000000, 10, emitted.append, Mock(is_set=Mock(return_value=False)))

        self.assertEqual(count, 0)
        self.assertEqual([event["type"] for event in emitted], ["interval"])
        self.assertEqual(emitted[0]["data"]["sample_config"]["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
