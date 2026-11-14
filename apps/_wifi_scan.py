"""Ordinary Linux Wi-Fi discovery through NetworkManager; no connection changes."""

from dataclasses import dataclass
from pathlib import Path
import re
import queue
import sys

import pygame

from deckfw import Widget


ENV = {"LC_ALL": "C", "LANG": "C"}
OPTIONS = ["--terse", "--escape", "yes", "--colors", "no", "--wait", "20"]


@dataclass(frozen=True)
class AccessPoint:
    ssid: str
    security: str
    bssid: str
    channel: int
    frequency: int
    signal: int

    @property
    def band(self):
        if 2400 <= self.frequency <= 2500:
            return "2.4"
        if 4900 <= self.frequency < 5925:
            return "5"
        return None


@dataclass(frozen=True)
class Snapshot:
    networks: tuple = ()
    message: str = "Preparing scan..."
    error: str = ""
    updated: str = ""
    interfaces: tuple = ()


def wireless_interfaces():
    if sys.platform != "linux":
        raise ValueError("Wi-Fi scanner requires Linux and NetworkManager.")
    try:
        interfaces = tuple(sorted(
            device.name for device in Path("/sys/class/net").iterdir()
            if (device / "wireless").is_dir() or (device / "phy80211").exists()
        ))
    except OSError as exc:
        raise ValueError(f"Cannot detect wireless interfaces: {exc}") from exc
    if not interfaces:
        raise ValueError("No wireless interfaces detected; check adapter hardware and drivers.")
    return interfaces


def split_fields(line):
    """nmcli terse mode escapes only colons and backslashes."""
    fields, chars = [], []
    escaped = False
    for char in line:
        if escaped:
            if char not in (":", "\\"):
                raise ValueError("Invalid nmcli escape")
            chars.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(chars))
            chars = []
        else:
            chars.append(char)
    if escaped:
        raise ValueError("Incomplete nmcli escape")
    fields.append("".join(chars))
    return fields


def parse_networks(lines, band="both"):
    if band not in ("both", "2.4", "5"):
        raise ValueError("Unknown band filter")
    networks = {}
    for number, line in enumerate(lines, 1):
        if not line:
            continue
        try:
            fields = split_fields(line)
            if len(fields) != 6:
                raise ValueError("Expected SSID,SECURITY,BSSID,CHAN,FREQ,SIGNAL")
            ssid, security, bssid, channel, frequency, signal = fields
            if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", bssid):
                raise ValueError("Invalid BSSID")
            if not channel.isdecimal() or not signal.isdecimal():
                raise ValueError("Channel and signal must be integers")
            freq = re.fullmatch(r"([0-9]+)(?: MHz)?", frequency)
            if not freq:
                raise ValueError("Frequency must be in MHz")
            ap = AccessPoint(ssid, security, bssid.upper(), int(channel),
                             int(freq[1]), int(signal))
            if not 1 <= ap.channel <= 233 or not 0 <= ap.signal <= 100:
                raise ValueError("Channel or signal out of range")
            if ap.frequency <= 0:
                raise ValueError("Frequency must be positive")
        except ValueError as exc:
            raise ValueError(f"nmcli row {number}: {exc}") from exc
        if ap.band is None or (band != "both" and ap.band != band):
            continue
        previous = networks.get(ap.bssid)
        if previous is None or ap.signal > previous.signal:
            networks[ap.bssid] = ap
    return tuple(sorted(networks.values(), key=lambda ap: (-ap.signal, ap.bssid)))


def command(nmcli, fields, *args):
    return [nmcli, *OPTIONS, "--fields", fields, *args]


def run_nmcli(ctx, args):
    result = ctx.run(args, log=False, env=ENV)
    ctx.check()
    if result.returncode != 0:
        detail = " ".join(line.strip() for line in result.lines if line.strip())
        raise ValueError(f"nmcli failed ({result.returncode}): {detail or 'no diagnostic output'}")
    return result.lines


def scan(ctx, nmcli, interface, band, on_interfaces=None):
    radio = run_nmcli(ctx, command(nmcli, "WIFI", "radio"))
    if radio != ["enabled"]:
        raise ValueError("Wi-Fi radio is disabled or blocked. Enable it in System > Controls.")
    devices = run_nmcli(ctx, command(nmcli, "DEVICE,TYPE,STATE", "device", "status"))
    wifi = {}
    for line in devices:
        fields = split_fields(line)
        if len(fields) != 3:
            raise ValueError("Invalid nmcli device status output")
        name, kind, state = fields
        if kind == "wifi":
            wifi[name] = state
    if interface:
        if interface not in wifi:
            raise ValueError(f"No NetworkManager Wi-Fi interface named {interface}")
        if wifi[interface] in ("unavailable", "unmanaged"):
            raise ValueError(f"{interface} is {wifi[interface]}; check adapter/rfkill/NetworkManager")
    elif not any(state not in ("unavailable", "unmanaged") for state in wifi.values()):
        raise ValueError("No available managed Wi-Fi adapter; check hardware, rfkill and NetworkManager")
    interfaces = ((interface,) if interface else
                  tuple(sorted(name for name, state in wifi.items()
                               if state not in ("unavailable", "unmanaged"))))
    if on_interfaces is not None:
        on_interfaces(interfaces)
    args = command(nmcli, "SSID,SECURITY,BSSID,CHAN,FREQ,SIGNAL",
                   "device", "wifi", "list", "--rescan", "yes")
    if interface:
        args.extend(["ifname", interface])
    return parse_networks(run_nmcli(ctx, args), band)


def wrapped(text, font, width):
    """Character wrapping keeps long diagnostics and unbroken names readable."""
    line = ""
    for char in text:
        if line and font.size(line + char)[0] > width:
            yield line
            line = ""
        line += char
    if line:
        yield line


class NetworkList(Widget):
    focusable = True

    def __init__(self, selectable=False):
        super().__init__(flex=1)
        self.selectable = selectable
        self.requests = queue.Queue()
        self.selected_bssid = None
        self.snapshot = Snapshot()
        self._seen = None
        self._networks = ()
        self._top = 0
        self._rows = 1
        self._horizontal = 0
        self._max_horizontal = 0

    def _sync(self):
        if self.snapshot.networks == self._networks:
            return
        anchor = self._networks[self._top].bssid if self._networks else None
        self._networks = self.snapshot.networks
        self._top = next((i for i, ap in enumerate(self._networks)
                          if ap.bssid == anchor), 0)
        if self.selectable and not any(ap.bssid == self.selected_bssid
                                       for ap in self._networks):
            self.selected_bssid = self._networks[0].bssid if self._networks else None

    def action(self, name):
        self._sync()
        if name in ("left", "right"):
            self._horizontal = max(0, min(self._max_horizontal,
                                         self._horizontal + (80 if name == "right" else -80)))
            return True
        if name in ("up", "down"):
            if self.selectable:
                index = next((i for i, ap in enumerate(self._networks)
                              if ap.bssid == self.selected_bssid), 0)
                index = max(0, min(len(self._networks) - 1,
                                   index + (1 if name == "down" else -1)))
                if self._networks:
                    self.selected_bssid = self._networks[index].bssid
                return True
            self._top = max(0, min(max(0, len(self._networks) - self._rows),
                                  self._top + (1 if name == "down" else -1)))
            return True
        if name == "a" and self.selectable and not self.snapshot.error:
            selected = next((ap for ap in self._networks
                             if ap.bssid == self.selected_bssid), None)
            if selected is not None:
                self.requests.put(selected)
            return True
        return False

    def draw(self, surf, rect, theme):
        snapshot = self.snapshot
        pad, font = theme.pad, theme.f_small
        line_h = font.get_linesize() + pad
        width = max(1, rect.w - 2 * pad - 5)
        self._sync()

        def text(value, y, color=theme.text):
            surf.blit(theme.render(font, theme.fit(font, value, width), color),
                      (rect.x + pad, y))

        y = rect.y
        # Wrap interface names instead of truncating the adapter identifier.
        interfaces = "Interface: " + (", ".join(snapshot.interfaces) or "detecting...")
        for line in wrapped(interfaces, font, width):
            text(line, y, theme.accent)
            y += line_h
        text(snapshot.message + ((" | " + snapshot.updated) if snapshot.updated else ""),
             y, theme.warn if snapshot.error else theme.dim)
        y += line_h
        if snapshot.error:
            for line in list(wrapped(snapshot.error, font, width))[:2]:
                text(line, y, theme.crit)
                y += line_h
            text("B back to setup to retry", y, theme.warn)
            y += line_h
        footer_h = line_h + pad
        row_h = font.get_linesize() * 2 + pad * 2
        table = pygame.Rect(rect.x + pad, y + pad, width,
                            max(0, rect.bottom - y - pad - footer_h))
        self._rows = max(1, (table.h - row_h) // row_h)
        if self.selectable:
            selected = next((i for i, ap in enumerate(self._networks)
                             if ap.bssid == self.selected_bssid), 0)
            self._top = min(self._top, selected)
            self._top = max(self._top, selected - self._rows + 1)
        self._top = min(self._top, max(0, len(self._networks) - self._rows))
        column_pad = min(pad, max(3, pad // 2))
        bssid = "AA:BB:CC:DD:EE:FF"
        encryption_width = font.size("WPA1 WPA2")[0] + column_pad * 2
        channel_width = max(font.size("Ch + Band")[0],
                            font.size("233 / 2.4G")[0]) + column_pad * 2
        signal_width = max(font.size("Signal")[0], font.size("100%")[0]) + column_pad * 2
        combined_width = font.size(f"{bssid} ({'M' * 18})")[0] + column_pad * 2
        first_width = min(combined_width,
                          max(font.size(bssid)[0] + column_pad * 2,
                              table.w - encryption_width - channel_width - signal_width))
        width_shift = first_width // 5
        first_width -= width_shift
        encryption_width += width_shift
        columns = [
            ("BSSID / SSID", first_width),
            ("Enc", encryption_width),
            ("Ch + Band", channel_width),
            ("Signal", signal_width),
        ]
        total_width = sum(w for _, w in columns)
        self._max_horizontal = max(0, total_width - table.w)
        self._horizontal = min(self._horizontal, self._max_horizontal)
        clip = surf.get_clip()
        surf.set_clip(table.clip(clip))

        def draw_row(values, row_y, background, header=False):
            pygame.draw.rect(surf, background, (table.x, row_y, table.w, row_h))
            x = table.x - self._horizontal
            for (name, cell_w), value in zip(columns, values):
                if isinstance(value, tuple):
                    for line, line_color, line_index in (
                            (value[0], theme.accent, 0), (value[1], theme.text, 1)):
                        image = theme.render(
                            font, theme.fit(font, line, cell_w - 2 * column_pad), line_color)
                        surf.blit(image, (x + column_pad,
                                          row_y + column_pad + line_index * font.get_linesize()))
                else:
                    color = theme.accent if header else theme.text
                    image = theme.render(font, theme.fit(font, value,
                                                         cell_w - 2 * column_pad), color)
                    surf.blit(image, (x + column_pad,
                                      row_y + (row_h - image.get_height()) // 2))
                pygame.draw.line(surf, theme.line, (x + cell_w - 1, row_y),
                                 (x + cell_w - 1, row_y + row_h))
                x += cell_w
            pygame.draw.line(surf, theme.line, (table.x, row_y + row_h - 1),
                             (table.right, row_y + row_h - 1))

        draw_row([name for name, _ in columns], table.y, theme.track, header=True)
        for index, ap in enumerate(self._networks[self._top:self._top + self._rows]):
            draw_row([(ap.bssid, ap.ssid or "<hidden SSID>"), ap.security,
                      f"{ap.channel} / {ap.band}G", f"{ap.signal}%"],
                     table.y + (index + 1) * row_h,
                     theme.panel if index % 2 == 0 else theme.bg)
            if self.selectable and ap.bssid == self.selected_bssid:
                pygame.draw.rect(surf, theme.accent,
                                 (table.x, table.y + (index + 1) * row_h, 3, row_h))
        if not self._networks:
            text("No matching networks" if snapshot.updated else "Waiting for scan",
                 table.y + row_h + pad, theme.dim)
        surf.set_clip(clip)
        if len(self._networks) > self._rows:
            height = max(6, int(table.h * self._rows / len(self._networks)))
            thumb_y = table.y + int((table.h - height) * self._top
                                   / (len(self._networks) - self._rows))
            pygame.draw.rect(surf, theme.dim, (rect.right - 3, thumb_y, 3, height))
        position = (selected + 1 if self._networks else 0) if self.selectable else (
            self._top + 1 if self._networks else 0)
        controls = " | A select | L/R columns" if self.selectable else (
            " | Up/Down rows | Left/Right columns")
        text(f"{position}/{len(self._networks)}" + controls, rect.bottom - footer_h, theme.dim)
