"""Exercise the patched readsb event stream with valid synthetic AVR frames."""

import json
import os
import selectors
import shutil
import socket
import subprocess
import time
import unittest


def _readsb_binary():
    return os.environ.get("READSB_BIN") or shutil.which("readsb")


@unittest.skipUnless(_readsb_binary(), "set READSB_BIN or install readsb to run decoder integration")
class ReadsbEventIntegrationTests(unittest.TestCase):
    def test_raw_mode_s_frames_emit_decoded_json_and_bad_crc_is_rejected(self):
        binary = _readsb_binary()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        command = [
            binary,
            "--net-only",
            "--net",
            "--net-bind-address",
            "127.0.0.1",
            "--net-ri-port",
            str(port),
            "--quiet",
            "--no-fix",
        ]
        environment = {**os.environ, "SKY_SCANNER_JSON": "1"}
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, bufsize=0
        )
        selector = selectors.DefaultSelector()
        decoded = []
        pending = b""
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 5
            connected = None
            while time.monotonic() < deadline and connected is None:
                try:
                    connected = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                except OSError:
                    if process.poll() is not None:
                        self.fail(f"readsb exited during startup with status {process.returncode}")
                    time.sleep(0.05)
            if connected is None:
                self.fail("readsb did not open the localhost raw input port")

            # These CRC-valid examples include an identification message and
            # the even/odd CPR pair used by common Mode-S decoder examples.
            good = (
                "8D4840D6202CC371C32CE0576098",  # callsign KLM1023
                "8D40621D58C382D690C8AC2863A7",  # even airborne position
                "8D40621D58C386435CC412692AD6",  # odd airborne position
            )
            bad_crc = "8DABCDEF58C382D690C8AC2863A7"
            with connected:
                connected.sendall(("".join(f"*{frame};\n" for frame in (*good, bad_crc))).encode())

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ready = selector.select(timeout=0.1)
                for key, _ in ready:
                    block = os.read(key.fd, 65536)
                    if not block:
                        break
                    pending += block
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            item = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(item, dict) and "address" in item:
                            decoded.append(item)
                if any(item.get("callsign") for item in decoded) and any(
                    "latitude" in item and "longitude" in item for item in decoded
                ):
                    break

            callsign = next((item for item in decoded if item.get("callsign")), None)
            position = next((item for item in decoded if "latitude" in item and "longitude" in item), None)
            self.assertIsNotNone(callsign, f"no callsign event; decoded {decoded!r}")
            self.assertEqual(callsign["callsign"].strip(), "KLM1023")
            self.assertEqual(callsign["address"].upper(), "4840D6")
            self.assertEqual(callsign["address_non_icao"], False)
            self.assertGreater(callsign["received_at"], 0)
            self.assertIsNotNone(position, f"no CPR-decoded position event; decoded {decoded!r}")
            # The global CPR pair can resolve to either member's latitude in
            # this example; both decode to the same nearby aircraft position.
            self.assertAlmostEqual(position["latitude"], 52.26, delta=0.02)
            self.assertAlmostEqual(position["longitude"], 3.93, delta=0.03)
            self.assertFalse(any(item["address"].upper() == "ABCDEF" for item in decoded))
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
