#!/usr/bin/env python3
"""Stream passively observed clients as JSON Lines without recording files."""

from __future__ import annotations

import argparse
import codecs
from dataclasses import asdict, dataclass
import errno
import fcntl
import json
import os
import pty
import re
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time


ROWS, COLUMNS = 1000, 200
DISPLAY_TIMEOUT = 15
MAC = r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}"
STATION = re.compile(
    rf"^\s*({MAC})\s+({MAC})\s+(-?\d+)\s+"
    r"\d+[e ]*-\s*\d+[e ]*\s+(\d+)\s+(\d+)(?:\s|$)")
CSI = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")


class SnapshotError(ValueError):
    """A live display is incomplete, incompatible, or truncated."""


def mac_address(value):
    value = value.strip().upper()
    if not re.fullmatch(MAC, value):
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
    power_dbm: int | None
    packets: int


def parse_snapshot(lines, bssid):
    """Accept only a complete airodump station table, never AP/probe records."""
    stations = False
    clients = {}
    for line in lines:
        fields = line.split()
        if fields[:2] == ["BSSID", "STATION"]:
            if stations or fields[2:] != ["PWR", "Rate", "Lost", "Frames", "Notes", "Probes"]:
                raise SnapshotError("incompatible station header")
            stations = True
            continue
        if not stations or not line.strip():
            continue
        if line.lstrip().startswith("(not associated)"):
            continue
        match = STATION.match(line)
        if match is None:
            raise SnapshotError("malformed station row")
        association, mac, power, _, packets = match.groups()
        if mac_address(association) != bssid:
            continue
        mac, power, packets = mac_address(mac), int(power), int(packets)
        if not -127 <= power <= 0:
            raise SnapshotError("invalid station power")
        clients[mac] = Client(mac, None if power == -1 else power, packets)
    if not stations:
        raise SnapshotError("live display has no station header")
    return list(clients.values())


class Display:
    """Decode the cursor-home / erase-to-end frames emitted by airodump."""

    def __init__(self, bssid):
        self.bssid = mac_address(bssid)
        self.pending = ""
        self.lines = None
        self.line = ""
        self.row = 2
        self.size = 0

    def feed(self, text):
        self.pending += text
        snapshots = []
        while self.pending:
            if self.pending[0] == "\x1b":
                match = CSI.match(self.pending)
                if match is None:
                    if len(self.pending) > 64 or not "\x1b[".startswith(self.pending[:2]):
                        raise SnapshotError("unsupported terminal escape")
                    break
                self.pending = self.pending[match.end():]
                args, _, command = match.groups()
                if command == "H" and args in ("2;1", "1;1", ""):
                    if self.lines is not None:
                        raise SnapshotError("incomplete terminal redraw")
                    self.lines, self.line, self.row, self.size = [], "", 2, 0
                elif command == "J" and args in ("", "0") and self.lines is not None:
                    if self.line:
                        self.lines.append(self.line)
                    if self.row >= ROWS - 2:
                        raise SnapshotError("terminal display is full; client list may be truncated")
                    snapshots.append(parse_snapshot(self.lines, self.bssid))
                    self.lines = None
                    self.line = ""
                elif command == "B" and self.lines is not None:
                    self.newline(int(args or "1"))
                elif command not in ("m", "K", "h", "l", "J", "H"):
                    raise SnapshotError(f"unsupported terminal cursor command: {command}")
                continue
            end = self.pending.find("\x1b")
            if end < 0:
                end = len(self.pending)
            plain, self.pending = self.pending[:end], self.pending[end:]
            if self.lines is None:
                continue
            self.size += len(plain)
            if self.size > ROWS * COLUMNS * 2:
                raise SnapshotError("oversized terminal display")
            for char in plain:
                if char == "\n":
                    self.newline()
                elif char != "\r":
                    self.line += char
                    if len(self.line) > COLUMNS:
                        raise SnapshotError("terminal row exceeds configured width")
        return snapshots

    def newline(self, count=1):
        if not 1 <= count <= ROWS:
            raise SnapshotError("invalid terminal row movement")
        self.lines.append(self.line)
        self.lines.extend([""] * (count - 1))
        self.line = ""
        self.row += count
        if self.row > ROWS:
            raise SnapshotError("terminal display overflow")


def emit(observed, target, output):
    data = {"event": "clients", "source": "airodump-ng", "target": target,
            "clients": [asdict(observed[mac]) for mac in sorted(observed)]}
    print(json.dumps(data, separators=(",", ":")), file=output, flush=True)


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


def capture(interface, bssid, channel, output):
    executable = shutil.which("airodump-ng")
    if executable is None:
        raise RuntimeError("airodump-ng not found; install aircrack-ng and check PATH")
    master, slave = pty.openpty()
    process = None
    diagnostics = bytearray()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
        command = [executable, "--bssid", bssid, "--channel", str(channel),
                   "--update", "2", "--background", "0", interface]
        try:
            process = subprocess.Popen(
                command, stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                env={**os.environ, "LC_ALL": "C", "TERM": "xterm"},
                start_new_session=True)
        except OSError as exc:
            raise RuntimeError(f"cannot launch airodump-ng: {exc}") from exc
        os.close(slave)
        slave = None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        display, observed = Display(bssid), {}
        target = {"interface": interface, "bssid": bssid, "channel": channel}
        deadline = time.monotonic() + DISPLAY_TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(master, selectors.EVENT_READ, "display")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while True:
                for key, _ in selector.select(timeout=0.2):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except OSError as exc:
                        if key.data != "display" or exc.errno != errno.EIO:
                            raise
                        chunk = b""
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    diagnostics.extend(chunk)
                    del diagnostics[:-8192]
                    if key.data == "display":
                        for clients in display.feed(decoder.decode(chunk)):
                            for client in clients:
                                previous = observed.get(client.mac)
                                if previous is not None and previous.packets > client.packets:
                                    client = Client(client.mac, client.power_dbm, previous.packets)
                                observed[client.mac] = client
                            emit(observed, target, output)
                            deadline = time.monotonic() + DISPLAY_TIMEOUT
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"airodump-ng exited unexpectedly (status {code})")
                if time.monotonic() >= deadline:
                    raise RuntimeError("no complete live station display within 15 seconds")
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, ValueError) as exc:
        detail = bytes(diagnostics).decode("utf-8", errors="replace")
        detail = CSI.sub("", detail)
        detail = "".join(char for char in detail if char.isprintable() or char in "\n\t")
        raise RuntimeError(
            f"{exc}\n{detail.strip()}\nCheck airodump compatibility, the existing monitor "
            "interface and capture permissions.") from exc
    finally:
        # Context cancellation may send SIGTERM more than once while we reap.
        handlers = {sig: signal.signal(sig, signal.SIG_IGN)
                    for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            if process is not None:
                try:
                    stop_capture(process)
                finally:
                    process.stderr.close()
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stream clients OBSERVED on one BSSID as JSON Lines; no capture files.",
        epilog="Linux, an existing monitor-mode interface and capture permissions are "
               "required. No sudo, monitor configuration, probes or deauthentication.")
    parser.add_argument("--interface", required=True, type=interface_name)
    parser.add_argument("--bssid", required=True, type=mac_address)
    parser.add_argument("--channel", required=True, type=channel_number)
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        print("Error: this capture tool requires Linux.", file=sys.stderr)
        return 1

    stopping = False

    def interrupted(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupted)
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        capture(args.interface, args.bssid, args.channel, sys.stdout)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
