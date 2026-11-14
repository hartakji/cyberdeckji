"""Real AP discovery followed by strictly simulated Wi-Fi demo activity."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import queue
import re
import shutil
import threading
import time

import pygame

from deckfw import app, Cancelled, Choice, Context, Form, Number, Widget
from apps._wifi_scan import AccessPoint, NetworkList, Snapshot, scan, wireless_interfaces, wrapped


COMMANDS = ("monairng", "monsniff", "kickerscript", "monairngreverse")
SCRIPT_TIMEOUT = 5
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


def setup():
    names = wireless_interfaces()
    return Form([
        Choice("interface", "Interface", [(name, name) for name in names], names[0]),
        Choice("band", "Band", [
            ("2.4 + 5 GHz", "both"), ("2.4 GHz", "2.4"), ("5 GHz", "5"),
        ], "both"),
        Number("interval", "Refresh (s)", default=15, minimum=5, maximum=120,
               help="Real AP scans; all clients and subsequent activity are fake."),
    ], submit_label="Start demo")


@dataclass(frozen=True)
class Client:
    mac: str
    name: str
    signal: int


@dataclass(frozen=True)
class View:
    phase: str = "networks"
    generation: int = 0
    title: str = ""
    ap: AccessPoint | None = None
    clients: tuple = ()
    client: Client | None = None
    elapsed: int = 0
    ticks: int = 0


@dataclass
class Session:
    interface: str
    commands: dict
    monitor_ap: AccessPoint | None = None


def parameters(interface, ap, client=None):
    values = {"interface": interface, "band": ap.band, "channel": ap.channel,
              "bssid": ap.bssid, "essid": ap.ssid}
    if client is not None:
        values["client"] = client.mac
    return values


def resolve_commands():
    commands = {}
    for name in COMMANDS:
        path = shutil.which(name)
        if not path:
            raise ValueError(f"Missing system demo command: {name}. Install it on PATH.")
        commands[name] = str(Path(path).absolute())
    return commands


def helper_command(name, values, commands):
    if name not in COMMANDS:
        raise ValueError("Unknown demo helper")
    return [commands[name], *[f"--{key}={value}" for key, value in values.items()]]


def event(line, expected, values):
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{expected}: invalid demo JSON") from exc
    if (not isinstance(data, dict) or data.get("demo") is not True
            or data.get("event") != expected or data.get("target") != values):
        raise ValueError(f"{expected}: invalid demo event or target")
    return data


def clients_from(data):
    records = data.get("clients")
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError("Invalid synthetic client list")
    clients, seen = [], set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Invalid synthetic client record")
        mac, name, signal = (record.get(key) for key in ("mac", "name", "signal"))
        if (not isinstance(mac, str) or not MAC.fullmatch(mac)
                or mac.upper() in seen or not isinstance(name, str) or not name
                or len(name) > 80 or type(signal) is not int or not 0 <= signal <= 100):
            raise ValueError("Invalid synthetic client fields")
        seen.add(mac.upper())
        clients.append(Client(mac.upper(), name, signal))
    return tuple(clients)


class ScriptTask:
    """Run through a child Context so local back/timeout can cancel and reap it."""

    def __init__(self, spec, command, stream=False):
        self.ctx = Context(spec)
        self.messages = queue.Queue()
        self.command, self.stream = command, stream
        self.worker = threading.Thread(target=self.work, daemon=True, name="wifi-demo-script")
        self.worker.start()

    def work(self):
        try:
            result = self.ctx.run(
                self.command, log=False,
                on_line=(lambda line: self.messages.put(("line", line))) if self.stream else None)
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
            raise RuntimeError("Demo script did not stop; cleanup is incomplete")


def helper(ctx, name, values, expected):
    task = ScriptTask(ctx.spec, helper_command(name, values, ctx.wifi_deauth_session.commands))
    deadline = time.monotonic() + SCRIPT_TIMEOUT
    try:
        while True:
            if time.monotonic() >= deadline:
                raise ValueError(f"{name} timed out after {SCRIPT_TIMEOUT}s")
            kind, result = task.message(ctx)
            if kind == "error":
                raise result
            if kind == "result":
                break
    finally:
        task.stop()
    if result.returncode != 0:
        raise ValueError(f"{name} failed ({result.returncode}): "
                         + (" ".join(result.lines[-4:]) or "no diagnostic output"))
    if len(result.lines) != 1:
        raise ValueError(f"{name}: expected one demo event")
    return event(result.lines[0], expected, values)


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
        self.top = 0
        self.interface = ""

    @property
    def handles_back(self):
        return self.state.phase in ("clients", "kick")

    @property
    def hints(self):
        return "Up/Down rows  L/R columns  A select  B back"

    def transition(self, phase, **values):
        self.state = View(phase, self.state.generation + 1, **values)
        self.top = self.horizontal = 0

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
            self.top = max(0, self.top + (1 if name == "down" else -1))
            return True
        return False

    def draw(self, surf, rect, theme):
        state, font, pad = self.state, theme.f_small, theme.pad
        line_h = font.get_linesize() + pad

        def text(value, y, color=theme.text):
            surf.blit(theme.render(font, theme.fit(font, value, rect.w), color), (rect.x, y))

        text("DEMO - real AP scan / fake activity", rect.y, theme.warn)
        body = pygame.Rect(rect.x, rect.y + line_h, rect.w, max(0, rect.h - line_h))
        if state.phase == "networks":
            self.networks.draw(surf, body, theme)
            return
        if state.phase == "clients":
            text("Invented clients - not observed devices", body.y, theme.warn)
            ap = state.ap
            text(f"{ap.bssid} | Ch {ap.channel} / {ap.band}G", body.y + line_h, theme.accent)
            row_h = line_h + pad
            table = pygame.Rect(body.x, body.y + 2 * line_h, body.w,
                                max(0, body.h - 3 * line_h))
            widths = [font.size("AA:BB:CC:DD:EE:FF")[0] + pad * 2, 150, 70]
            self.max_horizontal = max(0, sum(widths) - table.w)
            self.horizontal = min(self.horizontal, self.max_horizontal)
            selected = self.selection(state.clients)
            visible = max(1, table.h // row_h - 1)
            top = max(0, selected - visible + 1)
            clip = surf.get_clip()
            surf.set_clip(table.clip(clip))
            rows = [("Client MAC", "Demo name", "Signal")]
            rows.extend((client.mac, client.name, f"{client.signal}%")
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
                      else "No demo clients")
            text(footer + " | A select | B networks", rect.bottom - line_h, theme.dim)
            return
        if state.phase == "kick":
            ap, client = state.ap, state.client
            details = [
                "Simulated kick running - NO packets sent",
                f"Interface: {self.interface}",
                f"ESSID: {ap.ssid or '<hidden SSID>'}",
                f"BSSID: {ap.bssid}",
                f"Channel: {ap.channel} | Band: {ap.band} GHz",
                f"Client: {client.mac} ({client.name})",
                f"Elapsed: {state.elapsed}s | Fake cycles: {state.ticks}",
                "No real device is disconnected.",
            ]
            lines = [line for detail in details for line in wrapped(detail, font, body.w)]
            visible = max(1, body.h // line_h - 1)
            self.top = min(self.top, max(0, len(lines) - visible))
            for index, line in enumerate(lines[self.top:self.top + visible]):
                text(line, body.y + index * line_h)
            text("Up/Down details | B stop / clients", rect.bottom - line_h, theme.dim)
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


def kick(ctx, view, values):
    task = ScriptTask(ctx.spec, helper_command(
        "kickerscript", values, ctx.wifi_deauth_session.commands), stream=True)
    try:
        while True:
            action, _ = request(view)
            if action == "back":
                return
            kind, value = task.message(ctx)
            if kind is None:
                continue
            if kind == "error":
                raise value
            if kind != "line":
                raise ValueError(f"kickerscript ended unexpectedly ({value.returncode}): "
                                 + (" ".join(value.lines[-4:]) or "no diagnostic output"))
            data = event(value, "kick", values)
            if any(type(data.get(key)) is not int or data[key] < 0
                   for key in ("elapsed", "ticks")):
                raise ValueError("Invalid fake kick counters")
            view.state = replace(view.state, elapsed=data["elapsed"], ticks=data["ticks"])
    finally:
        task.stop()


def shutdown(ctx):
    session = getattr(ctx, "wifi_deauth_session", None)
    if session is None or session.monitor_ap is None:
        return
    helper(ctx, "monairngreverse", parameters(session.interface, session.monitor_ap),
           "monitor_stopped")
    session.monitor_ap = None
    ctx.log("DEMO: monairngreverse completed.", color="ok")


@app(name="Wifi Deauth", group="Network", icon="icons/network.png",
     setup=setup, shutdown=shutdown)
def wifi_deauth(ctx):
    view = ctx.add(DemoView())
    try:
        interface, band, interval = (ctx.params[key] for key in ("interface", "band", "interval"))
        view.interface = interface
        if interface not in wireless_interfaces():
            raise ValueError("Selected adapter disappeared; reopen setup.")
        nmcli = shutil.which("nmcli")
        if not nmcli:
            raise ValueError("Install NetworkManager (nmcli) and start its service.")
        session = ctx.wifi_deauth_session = Session(interface, resolve_commands())
        due = 0
        while True:
            ctx.check()
            state = view.state
            action, target = request(view)
            if state.phase == "networks":
                if action == "network" and any(ap.bssid == target.bssid
                                               for ap in view.networks.snapshot.networks):
                    view.transition("busy", title="DEMO: monairng - simulated monitor activation")
                    session.monitor_ap = target
                    helper(ctx, "monairng", parameters(interface, target), "monitor_started")
                    view.transition("clients", ap=target)
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
                if action == "back":
                    view.transition("networks")
                    due = 0
                    continue
                if action == "client" and target in state.clients:
                    view.transition("kick", ap=state.ap, client=target)
                    kick(ctx, view, parameters(interface, state.ap, target))
                    view.transition("clients", ap=state.ap, clients=state.clients)
                    due = 0
                    continue
                if time.monotonic() >= due:
                    data = helper(ctx, "monsniff", parameters(interface, state.ap), "clients")
                    view.state = replace(state, clients=clients_from(data))
                    due = time.monotonic() + interval
            ctx.sleep(0.03)
    except Cancelled:
        view.transition("stopped", title="DEMO stopped")
        raise
    except ValueError as exc:
        view.transition("error", title=str(exc))
        ctx.error, ctx.status = str(exc), "error"
