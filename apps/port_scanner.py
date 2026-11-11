"""Explicit local IPv4 discovery and selected-host TCP inspection."""

from dataclasses import dataclass
import ipaddress
import queue
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

import psutil
import pygame

from deckfw import app, Choice, Form, Widget


RANGES = ("1-1024", "1-10000", "1-65535")
ENV = {"LC_ALL": "C", "LANG": "C"}


def interfaces():
    if sys.platform != "linux":
        raise ValueError("Port scanner requires Linux.")
    stats = psutil.net_if_stats()
    result = {}
    for name, addresses in psutil.net_if_addrs().items():
        if name not in stats or not stats[name].isup:
            continue
        networks = []
        for address in addresses:
            if address.family == socket.AF_INET and address.netmask:
                network = ipaddress.IPv4Interface(f"{address.address}/{address.netmask}")
                if not network.ip.is_loopback:
                    networks.append(network.network)
        if networks:
            result[name] = tuple(networks)
    if not result:
        raise ValueError("No active IPv4 network interface; check your LAN connection.")
    return result


def setup():
    names = sorted(interfaces())
    return Form([
        Choice("interface", "Interface", [(name, name) for name in names], names[0]),
        Choice("ports", "TCP ports", [("1-1024", RANGES[0]), ("1-10000", RANGES[1]),
                                     ("All 1-65535", RANGES[2])], RANGES[0],
               help="Only scan networks you own or have permission to inspect."),
    ], submit_label="Discover devices")


@dataclass(frozen=True)
class Host:
    ip: str
    mac: str
    vendor: str


def parse_hosts(lines, networks):
    hosts = {}
    for line in lines:
        fields = line.split(None, 2)
        if not fields:
            continue
        try:
            address = ipaddress.IPv4Address(fields[0])
        except ipaddress.AddressValueError:
            if re.match(r"^\d+\.", fields[0]):
                raise ValueError("Malformed arp-scan IPv4 record")
            continue
        if len(fields) < 2 or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", fields[1]):
            raise ValueError("Malformed arp-scan MAC record")
        if not any(address in network and address not in
                   (network.network_address, network.broadcast_address) for network in networks):
            raise ValueError(f"Discovered address outside selected LAN: {address}")
        hosts[str(address)] = Host(str(address), fields[1].upper(),
                                   fields[2] if len(fields) > 2 else "Unknown")
    return tuple(sorted(hosts.values(), key=lambda host: int(ipaddress.IPv4Address(host.ip))))


def parse_ports(path, target, port_range=None):
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"Invalid Nmap XML: {exc}") from exc
    finished = root.find("runstats/finished")
    if root.tag != "nmaprun" or finished is None or finished.get("exit") != "success":
        raise ValueError("Nmap did not produce a completed successful scan")
    hosts = root.findall("host")
    if len(hosts) != 1 or not any(a.get("addr") == target and a.get("addrtype") == "ipv4"
                                for a in hosts[0].findall("address")):
        raise ValueError("Nmap result target does not match selected device")
    ports = hosts[0].find("ports")
    if ports is None:
        raise ValueError("Nmap result has no port data")
    rows, counts = [], {}
    try:
        for extra in ports.findall("extraports"):
            count = int(extra.get("count", ""))
            if count < 0 or not extra.get("state"):
                raise ValueError("Invalid port summary")
            state = extra.get("state")
            counts[state] = counts.get(state, 0) + count
        seen = set()
        for port in ports.findall("port"):
            number = int(port.get("portid", ""))
            state = port.find("state")
            if (port.get("protocol") != "tcp" or not 1 <= number <= 65535
                    or number in seen or state is None or not state.get("state")):
                raise ValueError("Invalid TCP port record")
            seen.add(number)
            if port_range:
                minimum, maximum = map(int, port_range.split("-"))
                if not minimum <= number <= maximum:
                    raise ValueError("Port outside requested range")
            status = state.get("state")
            counts[status] = counts.get(status, 0) + 1
            if status == "open":
                service = port.find("service")
                rows.append((str(number), "TCP",
                             service.get("name", "unknown") if service is not None else "unknown"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid Nmap port data: {exc}") from exc
    if port_range:
        minimum, maximum = map(int, port_range.split("-"))
        if sum(counts.values()) != maximum - minimum + 1:
            raise ValueError("Nmap port summary is incomplete")
    return tuple(sorted(rows, key=lambda row: int(row[0]))), counts


def command(ctx, args):
    result = ctx.run(args, log=False, env=ENV)
    ctx.check()
    if result.returncode:
        raise ValueError(f"{Path(args[0]).name} failed ({result.returncode}): "
                         + (" ".join(result.lines[-8:]) or "no diagnostic output"))
    return result.lines


@dataclass(frozen=True)
class View:
    phase: str = "busy"
    title: str = "Discovering devices..."
    headers: tuple = ("IPv4", "MAC", "Vendor")
    rows: tuple = ()
    error: str = ""


class ScanTable(Widget):
    focusable = True

    @property
    def hints(self):
        return "Up/Down rows  Left/Right columns  B devices"

    def __init__(self):
        super().__init__(flex=1)
        self.view = View()
        self.requests = queue.Queue()
        self.selected = 0
        self.offset = 0
        self._max_offset = 0

    @property
    def handles_back(self):
        return self.view.phase == "results"

    def action(self, name):
        view = self.view
        if name == "b" and self.handles_back:
            self.requests.put(("back", None))
            return True
        if name in ("up", "down"):
            self.selected = max(0, min(len(view.rows) - 1,
                                      self.selected + (1 if name == "down" else -1)))
            return True
        if name in ("left", "right"):
            self.offset = max(0, min(self._max_offset,
                                    self.offset + (80 if name == "right" else -80)))
            return True
        if name == "a" and view.phase == "devices" and view.rows:
            self.requests.put(("scan", view.rows[self.selected][0]))
            return True
        return False

    def draw(self, surf, rect, theme):
        view, font, pad = self.view, theme.f_small, theme.pad
        row_h = font.get_linesize() + pad * 2
        title = view.title + ((" | " + view.error) if view.error else "")
        lines, current = [], ""
        for word in title.split():
            trial = (current + " " + word).strip()
            if current and font.size(trial)[0] > rect.w:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
        for index, line in enumerate(lines):
            surf.blit(theme.render(font, theme.fit(font, line, rect.w),
                                   theme.crit if view.error else theme.accent),
                      (rect.x, rect.y + index * font.get_linesize()))
        head_h = len(lines) * font.get_linesize() + pad
        table = pygame.Rect(rect.x, rect.y + head_h, rect.w,
                            max(0, rect.h - head_h - row_h))
        widths = ([font.size("255.255.255.255")[0] + pad * 2,
                   font.size("AA:BB:CC:DD:EE:FF")[0] + pad * 2, 220]
                  if view.headers[0] == "IPv4" else [80, 80, 220])
        self._max_offset = max(0, sum(widths) - table.w)
        self.offset = min(self.offset, self._max_offset)
        visible = max(1, table.h // row_h - 1)
        selected = max(0, min(self.selected, len(view.rows) - 1))
        top = max(0, selected - visible + 1)
        clip = surf.get_clip()
        surf.set_clip(table.clip(clip))
        for index, values in enumerate((view.headers, *view.rows[top:top + visible])):
            y = table.y + index * row_h
            highlight = index > 0 and top + index - 1 == selected
            pygame.draw.rect(surf, theme.track if index == 0 else
                             (theme.panel if highlight or index % 2 else theme.bg),
                             (table.x, y, table.w, row_h))
            x = table.x - self.offset
            for value, width in zip(values, widths):
                image = theme.render(font, theme.fit(font, str(value), width - pad * 2),
                                     theme.accent if index == 0 else theme.text)
                surf.blit(image, (x + pad, y + (row_h - image.get_height()) // 2))
                x += width
                pygame.draw.line(surf, theme.line, (x - 1, y), (x - 1, y + row_h))
            if highlight:
                pygame.draw.rect(surf, theme.accent, (table.x, y, 3, row_h))
        surf.set_clip(clip)
        footer = ("No rows | " if not view.rows else f"{selected + 1}/{len(view.rows)} | ")
        footer += "B devices" if view.phase == "results" else "A scan | B back"
        surf.blit(theme.render(font, theme.fit(font, footer, rect.w), theme.dim),
                  (rect.x, rect.bottom - row_h))


@app(name="Port scanner", group="Network", icon="icons/network.png", setup=setup)
def port_scanner(ctx):
    table = ctx.add(ScanTable())
    try:
        available = interfaces()
        name, ports = ctx.params["interface"], ctx.params["ports"]
        if name not in available or ports not in RANGES:
            raise ValueError("Interface or port range is no longer valid; reopen setup.")
        arp, nmap = shutil.which("arp-scan"), shutil.which("nmap")
        if not arp or not nmap:
            raise ValueError("Install arp-scan and nmap. arp-scan requires raw-packet permissions.")
        hosts = parse_hosts(command(ctx, [arp, "--interface", name, "--localnet"]), available[name])
        devices = View("devices", f"{name}: {len(hosts)} devices",
                       rows=tuple((host.ip, host.mac, host.vendor) for host in hosts))
        table.view = devices
        saved_selection = 0
        while True:
            ctx.check()
            try:
                action, target = table.requests.get_nowait()
            except queue.Empty:
                ctx.sleep(0.05)
                continue
            if action == "back" and table.view.phase == "results":
                table.selected = saved_selection
                table.offset = 0
                table.view = devices
            elif action == "scan" and table.view.phase == "devices":
                if target not in {host.ip for host in hosts}:
                    raise ValueError("Target is not a discovered LAN device")
                saved_selection = next(i for i, host in enumerate(hosts) if host.ip == target)
                table.view = View(title=f"Scanning {target}, TCP {ports}...")
                ctx.note = "scanning..."
                try:
                    with tempfile.TemporaryDirectory(prefix="deck-nmap-") as directory:
                        output = str(Path(directory) / "scan.xml")
                        command(ctx, [nmap, "-sT", "-n", "-Pn", "-e", name,
                                      "-p", ports, "-oX", output, target])
                        rows, counts = parse_ports(output, target, ports)
                    summary = ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))
                    result = View("results", f"{target}: {summary}",
                                  ("Port", "Protocol", "Usual service"), rows)
                    ctx.status = "running"
                    ctx.error = None
                except ValueError as exc:
                    result = View("results", f"{target}: scan failed",
                                  ("Port", "Protocol", "Usual service"), error=str(exc))
                    ctx.error = str(exc)
                    ctx.status = "error"
                while not table.requests.empty():
                    table.requests.get_nowait()
                table.selected = table.offset = 0
                table.view = result
                ctx.note = ""
    except ValueError as exc:
        table.view = View("error", "Discovery failed", error=str(exc))
        ctx.error = str(exc)
        ctx.status = "error"
