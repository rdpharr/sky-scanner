"""One USB owner alternating decoders; only decoded events leave this process."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import selectors
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from .uat import normalize_uat

LOG = logging.getLogger(__name__)


def decoder_command(frequency: int) -> list[str]:
    serial = os.environ.get("HACKRF_SERIAL", "")
    if frequency == 1090000000:
        command = [
            os.environ.get("READSB_BIN", "readsb"),
            "--device-type",
            "hackrf",
            "--gain",
            os.environ.get("LNA_GAIN", "40"),
            "--hackrf-vgagain",
            os.environ.get("VGA_GAIN", "48"),
            "--sdr-buffer-size",
            "256",
            "--no-fix",
            "--quiet",
        ]
        if serial:
            command += ["--device", serial]
        if os.environ.get("RF_AMP") == "1":
            command += ["--hackrf-enable-ampgain"]
        return command
    return [
        os.environ.get("DUMP978_BIN", "dump978-fa"),
        "--sdr",
        "driver=hackrf" + (f",serial={serial}" if serial else ""),
        "--sdr-gain",
        os.environ.get("UAT_GAIN", "64"),
        "--json-stdout",
    ]


def normalize_readsb(payload: dict, receiver_id: str) -> dict:
    """Convert one flat readsb event into the snapshot observation shape."""
    if not isinstance(payload, dict):
        return None
    address_value = payload.get("address")
    if not isinstance(address_value, str):
        return None
    address = address_value.strip().upper()
    if len(address) != 6 or any(ch not in "0123456789ABCDEF" for ch in address):
        return None
    received_at = payload.get("received_at")
    if isinstance(received_at, bool):
        return None
    try:
        received_at = float(received_at)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(received_at):
        return None
    # readsb marks messages it could not trust; they are not valid reports.
    if payload.get("garbage") is True:
        return None

    decoder_type = str(payload.get("address_type", "unknown")).strip().lower()
    non_icao = payload.get("address_non_icao")
    if isinstance(non_icao, bool):
        address_type = f"readsb:{decoder_type}" if non_icao else "icao"
    elif decoder_type in {"icao", "icao_nontransponder", "mode_s", "adsr_icao", "tisb_icao"}:
        address_type = "icao"
    else:
        # Older event emitters lack an explicit ICAO bit. Keep ambiguous and
        # non-ICAO decoder classes isolated rather than merging by hex alone.
        address_type = f"readsb:{decoder_type}"

    decoder_source = payload.get("source")
    decoder_source = str(decoder_source).strip().lower() if decoder_source is not None else None
    origin = payload.get("origin")
    if origin not in {"direct", "rebroadcast"}:
        if decoder_source in {"adsr", "tisb"} or decoder_type.startswith(("adsr_", "tisb_")):
            origin = "rebroadcast"
        elif decoder_source in {"adsb", "adsb_nontransponder", "mode_s", "mode_s_checked"}:
            origin = "direct"
        else:
            origin = decoder_source

    excluded = {
        "received_at",
        "address",
        "address_type",
        "address_non_icao",
        "origin",
        "source",
        "receiver_id",
        "frequency_hz",
    }
    fields = {
        ("readsb_address_type" if key == "address_type" else key): value
        for key, value in payload.items()
        if key not in excluded
    }
    fields["readsb_address_type"] = decoder_type
    if payload.get("position_bad") is True:
        fields.pop("latitude", None)
        fields.pop("longitude", None)
    squawk = fields.get("squawk")
    if isinstance(squawk, int) and not isinstance(squawk, bool) and 0 <= squawk <= 7777:
        # readsb emits the octal digits as a decimal integer; keep leading zeroes.
        fields["squawk"] = f"{squawk:04d}"
    if isinstance(non_icao, bool):
        fields["readsb_address_non_icao"] = non_icao
    if origin is not None:
        fields["readsb_origin"] = origin
    if decoder_source is not None:
        fields["readsb_source"] = decoder_source

    return {
        "received_at": received_at,
        "receiver_id": str(receiver_id),
        "address": address,
        "address_type": address_type,
        "frequency_hz": 1090000000,
        "source": origin,
        "fields": fields,
    }


class EventSender:
    """Bounded memory forwarding. Failed batches retry; overflow drops newest."""

    def __init__(self, url: str):
        self.url = url
        self.queue = queue.Queue(maxsize=10000)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def put(self, event):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            LOG.error("forwarding buffer full; dropping decoded event")

    def run(self):
        batch = []
        while not self.stop.is_set():
            if not batch:
                try:
                    batch.append(self.queue.get(timeout=0.5))
                except queue.Empty:
                    continue
                while len(batch) < 250:
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
            request = urllib.request.Request(self.url, json.dumps(batch).encode(), {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    if response.status != 202:
                        raise RuntimeError(f"ingestion response {response.status}")
                batch.clear()
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    # A rejected batch will never succeed; retrying it would block all later data.
                    LOG.error("ingestion rejected %d events: HTTP %s", len(batch), exc.code)
                    batch.clear()
                else:
                    LOG.warning("ingestion unavailable: HTTP %s", exc.code)
                    self.stop.wait(2)
            except Exception as exc:
                LOG.warning("ingestion unavailable: %s", exc)
                self.stop.wait(2)


def capture(frequency: int, seconds: float, emit, stop: threading.Event):
    """Run a single decoder turn and close it before another band starts."""
    receiver_id = os.environ.get("RECEIVER_ID", "home-hackrf")
    command = decoder_command(frequency)
    env = {**os.environ, "SKY_SCANNER_JSON": "1"}
    started = time.time()
    interval = {
        "id": str(uuid.uuid4()),
        "receiver_id": receiver_id,
        "frequency_hz": frequency,
        "started_at": started,
        "decoder_version": "readsb:3b5368a+sky1" if frequency == 1090000000 else "dump978:f40aa9a",
        "sample_config": {"command": command, "timing": "decoder_process_lifetime"},
    }
    # Process start bounds reception, not an assertion that RF initialization succeeded.
    LOG.info("starting %s MHz turn", frequency // 1000000)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=None, env=env, bufsize=0, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = b""
    count = 0
    deadline = time.monotonic() + seconds
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            ready = selector.select(timeout=0.25)
            for key, _ in ready:
                block = os.read(key.fd, 65536)
                if not block:
                    return count
                pending += block
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        raw = json.loads(line)
                        observation = (
                            normalize_readsb(raw, receiver_id)
                            if frequency == 1090000000
                            else normalize_uat(raw, receiver_id)
                        )
                        if observation:
                            emit({"type": "observation", "id": str(uuid.uuid4()), "data": observation})
                            count += 1
                    except (ValueError, KeyError, TypeError) as exc:
                        LOG.warning("invalid decoder event: %s", exc)
                if len(pending) > 1048576:
                    raise RuntimeError("decoder output exceeds line limit")
            if process.poll() is not None and not ready:
                break
    finally:
        selector.close()
        if process.poll() is None:
            # Reception ends around the stop signal; ended_at is process exit,
            # which can be later when a decoder's SDR teardown stalls.
            interval["sample_config"]["stop_signal_at"] = time.time()
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        interval["ended_at"] = time.time()
        interval["sample_config"]["exit_code"] = process.returncode
        interval["sample_config"]["decoded_messages"] = count
        emit({"type": "interval", "id": interval["id"], "data": interval})
        LOG.info("finished %s MHz: %s decoded messages", frequency // 1000000, count)
    return count


def band_dwell(frequency: int) -> float:
    """Per-band turn length; DWELL_SECONDS is the shared fallback."""
    default = os.environ.get("DWELL_SECONDS", "30")
    return float(os.environ.get(f"DWELL_{frequency // 1000000}_SECONDS", default))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=0, help="zero runs indefinitely")
    parser.add_argument("--frequency", type=int, choices=[1090, 978])
    parser.add_argument("--stdout", action="store_true", help="decoded live demo, no forwarding")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    sender = None if args.stdout else EventSender(os.environ.get("INGEST_URL", "http://ingest:9000/events"))
    emit = (lambda event: print(json.dumps(event), flush=True)) if args.stdout else sender.put
    end = time.monotonic() + args.seconds if args.seconds else float("inf")
    frequencies = [args.frequency * 1000000] if args.frequency else [1090000000, 978000000]
    try:
        while not stop.is_set() and time.monotonic() < end:
            for frequency in frequencies:
                if stop.is_set() or time.monotonic() >= end:
                    break
                turn_started = time.monotonic()
                dwell = band_dwell(frequency)
                try:
                    capture(frequency, min(dwell, end - time.monotonic()), emit, stop)
                except (OSError, RuntimeError) as exc:
                    LOG.error("receiver failed: %s", exc)
                # A decoder that exits early usually means the HackRF is absent
                # or busy; back off instead of reopening USB in a tight loop.
                stop.wait(5 if time.monotonic() - turn_started < min(dwell, 5) else 0.5)
    finally:
        if sender:
            # A bounded graceful drain; abrupt outages may lose buffered data.
            deadline = time.monotonic() + 5
            while not sender.queue.empty() and time.monotonic() < deadline:
                time.sleep(0.1)
            sender.stop.set()
            sender.thread.join(timeout=6)


if __name__ == "__main__":
    main()
