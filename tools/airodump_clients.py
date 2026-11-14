#!/usr/bin/env python3
"""Passively list clients observed on one BSSID; no launcher dependencies."""

import argparse
import csv
from dataclasses import dataclass, replace
from datetime import datetime
import io
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


FIELDS = ("Station MAC", "First time seen", "Last time seen", "Power",
          "# packets", "BSSID", "Probed ESSIDs")


class SnapshotError(ValueError):
    """A snapshot is incomplete or contains invalid required data."""


class WaitingForFile(SnapshotError):
    """The first CSV has not appeared yet."""


def mac_address(value):
    value = value.strip().upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value):
        raise ValueError("expected a MAC address (AA:BB:CC:DD:EE:FF)")
    if int(value[:2], 16) & 1 or value == "00:00:00:00:00:00":
        raise ValueError("expected a nonzero unicast MAC address")
    return value


def interface_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}", value):
        raise ValueError("expected an interface name of 1-15 safe characters")
    return value


def channel_number(value):
    try:
        channel = int(value)
    except ValueError:
        raise ValueError("channel must be an integer from 1 to 233") from None
    if not 1 <= channel <= 233:
        raise ValueError("channel must be an integer from 1 to 233")
    return channel


@dataclass(frozen=True)
class Client:
    mac: str
    bssid: str
    first_seen: str
    last_seen: str
    power: int
    packets: int


def merge_clients(observed, clients):
    for client in clients:
        previous = observed.get(client.mac)
        if previous is not None:
            latest = client if client.last_seen >= previous.last_seen else previous
            client = replace(
                latest, first_seen=min(previous.first_seen, client.first_seen),
                packets=max(previous.packets, client.packets),
            )
        observed[client.mac] = client


def parse_snapshot(text, bssid):
    """Parse a complete station section atomically, ignoring the AP section."""
    if not text.endswith("\n"):
        raise SnapshotError("CSV is empty or truncated (no final newline)")
    bssid = mac_address(bssid)
    columns = None
    clients = {}
    try:
        for line, row in enumerate(csv.reader(io.StringIO(text),
                                            skipinitialspace=True, strict=True), 1):
            row = [value.strip() for value in row]
            if not any(row):
                continue
            if row[0] == "Station MAC":
                if columns is not None:
                    raise SnapshotError("duplicate station header")
                if any(row.count(field) != 1 for field in FIELDS):
                    raise SnapshotError("station header is missing or duplicates required fields")
                columns = {field: row.index(field) for field in FIELDS}
                continue
            if columns is None:
                continue
            if len(row) <= max(columns.values()):
                raise SnapshotError(f"station row {line}: truncated fields")
            fields = {field: row[index] for field, index in columns.items()}
            try:
                mac = mac_address(fields["Station MAC"])
                association = fields["BSSID"]
                if association.lower() == "(not associated)":
                    continue
                association = mac_address(association)
                if association != bssid:
                    continue
                first = fields["First time seen"]
                last = fields["Last time seen"]
                for stamp in (first, last):
                    datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                    if not re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", stamp):
                        raise ValueError("invalid timestamp format")
                if first > last:
                    raise ValueError("first seen is after last seen")
                power = int(fields["Power"])
                packets = int(fields["# packets"])
                if packets < 0:
                    raise ValueError("packet count must be nonnegative")
            except ValueError as exc:
                raise SnapshotError(f"station row {line}: {exc}") from exc
            merge_clients(clients, [Client(mac, association, first, last, power, packets)])
    except csv.Error as exc:
        raise SnapshotError(f"invalid CSV: {exc}") from exc
    if columns is None:
        raise SnapshotError("CSV has no Station MAC header yet")
    return list(clients.values())


def read_snapshot(path, bssid, attempts=3):
    """Reopen rewritten files; never commit observations from a partial read."""
    for attempt in range(attempts):
        try:
            before = path.stat()
            text = path.read_text(encoding="utf-8", errors="replace")
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns):
                raise SnapshotError("CSV changed while reading")
            return parse_snapshot(text, bssid)
        except FileNotFoundError:
            error = WaitingForFile("waiting for the initial CSV file")
        except SnapshotError as exc:
            error = exc
        if attempt + 1 < attempts:
            time.sleep(0.1)
    raise error


def render(observed, bssid, status, output):
    if output.isatty():
        output.write("\033[2J\033[H")
    print(f"Clients OBSERVED since startup on {bssid} ({len(observed)})", file=output)
    print("Not an authoritative list of currently connected devices. Ctrl+C to stop.",
          file=output)
    print(status, file=output)
    print(f"{'STATION MAC':17}  {'BSSID':17}  {'FIRST SEEN':19}  "
          f"{'LAST SEEN':19}  {'POWER':>11}  PACKETS", file=output)
    for mac in sorted(observed):
        client = observed[mac]
        power = "unavailable" if client.power == -1 else f"{client.power} dBm"
        print(f"{client.mac:17}  {client.bssid:17}  {client.first_seen:19}  "
              f"{client.last_seen:19}  {power:>11}  {client.packets}", file=output)
    output.flush()


def stop_capture(process):
    if process.poll() is not None:
        process.wait()
        return
    for send_signal in (lambda: process.send_signal(signal.SIGINT),
                        process.terminate, process.kill):
        try:
            send_signal()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue
    raise RuntimeError("capture did not stop after SIGINT, SIGTERM and SIGKILL")


def diagnostics(stderr):
    stderr.flush()
    stderr.seek(0, io.SEEK_END)
    size = stderr.tell()
    stderr.seek(max(0, size - 8192))
    return stderr.read().decode("utf-8", errors="replace").strip()


def capture(interface, bssid, channel, output):
    executable = shutil.which("airodump-ng")
    if executable is None:
        raise RuntimeError("airodump-ng not found; install aircrack-ng and check PATH")
    observed = {}
    with tempfile.TemporaryDirectory(prefix="airodump-clients-") as directory:
        prefix = Path(directory) / "capture"
        with tempfile.TemporaryFile() as stderr:
            command = [executable, "--bssid", bssid, "--channel", str(channel),
                       "--output-format", "csv", "--write", str(prefix),
                       "--write-interval", "2", "--background", "1", interface]
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=stderr,
                                           start_new_session=True)
            except OSError as exc:
                raise RuntimeError(f"cannot launch airodump-ng: {exc}") from exc
            try:
                failures = 0
                while True:
                    code = process.poll()
                    if code is not None:
                        raise RuntimeError(f"airodump-ng exited unexpectedly (status {code})")
                    try:
                        clients = read_snapshot(Path(str(prefix) + "-01.csv"), bssid)
                    except SnapshotError as exc:
                        failures += 1
                        status = f"{exc}; retaining previous observations (retry {failures}/5)"
                        render(observed, bssid, status, output)
                        if failures >= 5:
                            raise RuntimeError(f"no usable capture snapshot: {exc}") from exc
                    else:
                        failures = 0
                        merge_clients(observed, clients)
                        status = ("Snapshot loaded; no matching clients observed yet."
                                  if not observed else "Snapshot loaded; absent clients are retained.")
                        render(observed, bssid, status, output)
                    time.sleep(2)
            except KeyboardInterrupt:
                print("\nStopping capture.", file=output, flush=True)
            except (OSError, RuntimeError) as exc:
                detail = diagnostics(stderr)
                raise RuntimeError(f"{exc}" + (f"\nairodump-ng stderr: {detail}" if detail else "")
                                   + "\nCheck the existing monitor interface and capture permissions.") from exc
            finally:
                stop_capture(process)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Passively list clients OBSERVED on one BSSID since startup.",
        epilog="Linux, an existing monitor-mode interface and capture permissions are "
               "required. No sudo, monitor configuration, probes or deauthentication.")
    parser.add_argument("--interface", required=True, type=interface_name)
    parser.add_argument("--bssid", required=True, type=mac_address)
    parser.add_argument("--channel", required=True, type=channel_number)
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        print("Error: this capture tool requires Linux.", file=sys.stderr)
        return 1

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        capture(args.interface, args.bssid, args.channel, sys.stdout)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
