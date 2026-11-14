from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import pygame

from deckfw import context
from deckfw.config import DEFAULTS
from deckfw.loader import discover
from deckfw.menu import default_menu
from deckfw.runner import Runner
from deckfw.screens import AppScreen, Navigator, Screen
from deckfw.theme import Theme
from apps._wifi_scan import AccessPoint, Snapshot
from tools import airodump_clients as scanner

with patch.object(context, "_REGISTRY", []):
    from apps import wifi_deauth as demo


AP = AccessPoint("--Example: Wi\\Fi; $(not-a-command)", "WPA2",
                 "02:00:00:00:00:AB", 36, 5180, 90)
CLIENT = demo.Client("12:DE:AD:00:00:01", -45, 10)
ADDRESS = "02:00:00:00:00:01"
TARGET = {"interface": "wlan0mon", "bssid": AP.bssid, "channel": AP.channel}


def event(clients=None, target=None):
    return {"event": "clients", "source": "airodump-ng", "target": target or TARGET,
            "clients": clients if clients is not None else [
                {"mac": CLIENT.mac, "power_dbm": CLIENT.power_dbm, "packets": CLIENT.packets}]}


def iw_lines(name="wlan0", mode="managed", index=3, phy=0):
    return [f"phy#{phy}", f"\tInterface {name}", f"\t\tifindex {index}",
            f"\t\taddr {ADDRESS}", f"\t\ttype {mode}"]


def system_stubs(directory):
    root = Path(directory)
    state = root / "state.json"
    state.write_text("{}")
    frame = (
        "\x1b[2;1H CH 36 ][ Elapsed: 0 s\x1b[0K\n"
        " BSSID PWR Beacons ESSID\x1b[0K\n"
        f" {AP.bssid} -30 20 Example\x1b[0K\n"
        " BSSID              STATION            PWR    Rate    Lost   Frames  Notes  Probes"
        "\x1b[0K\n"
        f" {AP.bssid}  {CLIENT.mac}   -45    6e- 1     0       10        \x1b[0K\n"
        "\x1b[0J")
    for name in ("nmcli", "airmon-ng", "iw", "airodump-ng", "ip"):
        script = root / name
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, os, signal, sys, time\n"
            "from pathlib import Path\n"
            "root = Path(__file__).parent\n"
            "name = Path(__file__).name\n"
            "state = json.loads((root / 'state.json').read_text())\n"
            "def log(value):\n"
            "    with (root / 'events').open('a') as f: f.write(value + '\\n')\n"
            "log(name + ' ' + ' '.join(sys.argv[1:]))\n"
            "if name == 'iw':\n"
            "    if state.get('removed'): sys.exit(0)\n"
            "    mode = 'monitor' if state.get('monitor') else state.get('initial_mode', 'managed')\n"
            "    iface = 'wlan0mon' if mode == 'monitor' and not state.get('same_name') else 'wlan0'\n"
            "    print('phy#0\\n\\tInterface ' + iface + '\\n\\t\\tifindex 3\\n'\n"
            f"          '\\t\\taddr {ADDRESS}\\n\\t\\ttype ' + mode)\n"
            "    if state.get('shared'):\n"
            f"        print('phy#0\\n\\tInterface extra\\n\\t\\tifindex 4\\n\\t\\taddr {ADDRESS}\\n\\t\\ttype managed')\n"
            "elif name == 'airmon-ng':\n"
            "    starting = sys.argv[1] == 'start'\n"
            "    if not starting and (root / 'capturing').exists():\n"
            "        print('capture still running'); sys.exit(9)\n"
            "    if not starting and state.get('stop_fail'):\n"
            "        print('restoration denied'); sys.exit(7)\n"
            "    if starting or not state.get('stop_noop'): state['monitor'] = starting\n"
            "    state['up'] = False\n"
            "    (root / 'next.json').write_text(json.dumps(state))\n"
            "    (root / 'next.json').replace(root / 'state.json')\n"
            "    if starting and state.get('start_fail'):\n"
            "        print('activation denied after mutation'); sys.exit(7)\n"
            "    if starting and state.get('start_sleep'): time.sleep(30)\n"
            "elif name == 'ip':\n"
            "    if sys.argv[1] == '-j':\n"
            "        print(json.dumps([{'ifname': 'wlan0', 'flags': ['UP'] if state.get('up', True) else []}]))\n"
            "    else:\n"
            "        state['up'] = sys.argv[-1] == 'up'\n"
            "        (root / 'next.json').write_text(json.dumps(state))\n"
            "        (root / 'next.json').replace(root / 'state.json')\n"
            "elif name == 'airodump-ng':\n"
            "    marker = root / 'capturing'\n"
            "    try:\n"
            "        marker.write_text(str(os.getpid()))\n"
            "        while True:\n"
            "            if state.get('capture_bad'): os.write(1, b'\\x1b[2;1Hbad\\x1b[0J')\n"
            "            elif not state.get('capture_silent'):\n"
            f"                os.write(1, {frame.encode()!r})\n"
            "            time.sleep(0.1)\n"
            "    except KeyboardInterrupt:\n"
            "        pass\n"
            "    finally:\n"
            "        if marker.exists(): marker.unlink()\n"
            "        log('capture_stopped')\n"
        )
        script.chmod(0o700)
    wrapper = root / "scanner.py"
    wrapper.write_text(
        "import runpy, sys\nsys.platform = 'linux'\n"
        f"runpy.run_path({str(demo.SCANNER)!r}, run_name='__main__')\n")
    return {name: str(root / name) for name in ("nmcli", "airmon-ng", "iw", "airodump-ng", "ip")}


class ContractTests(unittest.TestCase):
    def test_setup_and_tool_resolution(self):
        with patch.object(demo, "wireless_interfaces", return_value=("wlan0", "wlan1")):
            form = demo.setup()
        self.assertEqual(form.defaults(), {"interface": "wlan0", "band": "both", "interval": 15})
        for key, value in (("interface", ""), ("band", "6"), ("interval", 4), ("interval", 121)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                form.validate({**form.defaults(), key: value})
        with patch.object(demo.shutil, "which", side_effect=lambda name: "/stub/" + name):
            self.assertEqual(set(demo.resolve_commands()),
                             {"nmcli", "airmon-ng", "iw", "airodump-ng", "ip"})
        with patch.object(demo.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Missing command"):
                demo.resolve_commands()

    def test_scanner_output_round_trips_into_app(self):
        output = io.StringIO()
        scanner.emit({CLIENT.mac: scanner.Client(CLIENT.mac, None, 10)}, TARGET, output)
        self.assertEqual(demo.clients_from(output.getvalue(), TARGET),
                         (replace(CLIENT, power_dbm=None),))
        self.assertEqual(demo.clients_from(json.dumps(event([])), TARGET), ())

    def test_malformed_wrong_target_and_synthetic_events_are_rejected(self):
        valid = event()
        for data in ({}, [], {**valid, "source": "demo"}, {**valid, "event": "kick"},
                     {**valid, "target": {**TARGET, "channel": 1}},
                     {**valid, "target": "bad"}, {**valid, "clients": None}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                demo.clients_from(json.dumps(data), TARGET)
        record = valid["clients"][0]
        for clients in ([None], [record, record], [{**record, "mac": "bad"}],
                        [{**record, "mac": demo.BROADCAST.mac}],
                        [{**record, "mac": AP.bssid}], [{**record, "packets": True}],
                        [{**record, "power_dbm": True}], [{**record, "power_dbm": 10}],
                        [{**record, "packets": -1}]):
            with self.subTest(clients=clients), self.assertRaises(ValueError):
                demo.clients_from(json.dumps(event(clients)), TARGET)
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            demo.clients_from("permission denied", TARGET)

    def test_iw_interface_parser_rejects_incomplete_and_duplicate_information(self):
        self.assertEqual(demo.interfaces_from(iw_lines()),
                         [demo.Interface("wlan0", 0, 3, "managed", ADDRESS)])
        for lines in (iw_lines()[:-1], ["Interface wlan0"], ["phy#bad"],
                      iw_lines() + iw_lines(), iw_lines(name="wlan0;bad"),
                      iw_lines(index=-1)):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                demo.interfaces_from(lines)

    def test_cleanup_refuses_replaced_monitor_or_unstopped_capture(self):
        original = demo.Interface("wlan0", 0, 3, "managed", ADDRESS)
        monitor = replace(original, name="wlan0mon", index=4, mode="monitor")
        session = demo.Session("wlan0", {}, original=original, monitor="wlan0mon",
                               monitor_device=monitor, activation_attempted=True)
        ctx = Mock()
        with patch.object(session, "devices", return_value=[replace(monitor, index=5)]), \
                patch.object(demo, "command") as command:
            with self.assertRaisesRegex(ValueError, "changed externally"):
                session.restore(ctx)
            command.assert_not_called()
        session.capture_task = Mock()
        session.capture_task.stop.side_effect = RuntimeError("still running")
        with patch.object(session, "devices") as devices:
            with self.assertRaisesRegex(RuntimeError, "still running"):
                session.restore(ctx)
            devices.assert_not_called()
        self.assertTrue(session.activation_attempted)

    def test_selection_uses_mac_even_when_queued_measurements_are_old(self):
        ctx = context.Context(context.AppSpec("Wifi Deauth", None))
        view = demo.DemoView()
        updated = replace(CLIENT, packets=20)
        view.transition("clients", ap=AP, clients=(demo.BROADCAST, updated))
        view.requests.put((view.state.generation, "client", CLIENT))
        session = demo.Session("wlan0", {}, monitor="wlan0mon")
        with patch.object(demo, "ScriptTask") as task:
            self.assertEqual(demo.capture_clients(ctx, view, session), ("client", updated))
            task.return_value.stop.assert_called_once_with()
        self.assertIsNone(session.capture_task)

    def test_discovery_keeps_one_app_and_existing_menu_placement(self):
        root = str(Path(__file__).resolve().parents[1])
        with patch.object(context, "_REGISTRY", []):
            specs = discover(root, DEFAULTS)
        spec = next(spec for spec in specs if spec.name == "Wifi Deauth")
        self.assertEqual(sum(spec.name == "Wifi Deauth" for spec in specs), 1)
        self.assertEqual(spec.source, "native")
        self.assertIsNotNone(spec.shutdown)
        network = next(item for item in default_menu(specs).items[0].menu.items
                       if item.label == "Network")
        self.assertTrue(any(item.spec is spec for item in network.menu.items))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.commands = system_stubs(temporary.name)
        self.runners = []
        self.nav = Navigator()
        self.nav.push(Screen())
        self.addCleanup(self.nav.close_all)
        for patcher in (
            patch.object(demo, "wireless_interfaces", return_value=("wlan0",)),
            patch.object(demo.shutil, "which", side_effect=self.commands.get),
            patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ.get("PATH", "")}),
            patch.object(demo, "SCANNER", self.root / "scanner.py"),
            patch.object(demo, "scan", return_value=(AP,)),
            patch("deckfw.context.Runner", side_effect=self.runner),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def runner(self, *args, **kwargs):
        runner = Runner(*args, **kwargs)
        self.runners.append(runner)
        return runner

    def state(self, **values):
        path = self.root / "state.json"
        state = json.loads(path.read_text())
        if values:
            state.update(values)
            path.write_text(json.dumps(state))
        return state

    def events(self):
        path = self.root / "events"
        return path.read_text().splitlines() if path.exists() else []

    def wait(self, predicate, message="App did not become ready"):
        deadline = time.monotonic() + 6
        while not predicate():
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.01)

    def start(self):
        screen = AppScreen(context.AppSpec("Wifi Deauth", demo.wifi_deauth, shutdown=demo.shutdown),
                           {"interface": "wlan0", "band": "both", "interval": 5})
        self.nav.push(screen)
        self.wait(lambda: bool(screen.ctx.root.children))
        view = screen.ctx.root.children[0]
        self.wait(lambda: bool(view.networks.snapshot.networks) or not screen.alive)
        return screen, view

    def clients(self, screen, view):
        screen.action("a")
        self.wait(lambda: (view.state.phase == "clients" and len(view.state.clients) > 1)
                  or not screen.alive, "Client capture did not become ready")
        self.assertTrue(screen.alive, screen.ctx.error)
        self.assertEqual(view.state.clients, (demo.BROADCAST, CLIENT))

    def close(self, screen):
        screen.close()
        screen._thread.join(6)
        self.assertFalse(screen.alive)
        self.assertTrue(all(runner._proc.poll() is not None for runner in self.runners))

    def test_same_adapter_capture_selection_simulation_resume_and_restoration(self):
        screen, view = self.start()
        self.clients(screen, view)
        self.assertEqual(screen.ctx.wifi_deauth_session.monitor, "wlan0mon")
        self.assertIn("airmon-ng start wlan0 36", self.events())
        self.assertEqual(demo.scan.call_count, 1)
        screen.action("down")
        screen.action("a")
        self.wait(lambda: view.state.phase == "kick")
        self.assertEqual(view.state.client, CLIENT)
        self.assertFalse((self.root / "capturing").exists())
        self.assertEqual(self.events()[-1], "capture_stopped")
        runner_count = len(self.runners)
        time.sleep(0.1)
        self.assertEqual(len(self.runners), runner_count)
        self.assertEqual(demo.scan.call_count, 1)
        screen.action("b")
        self.wait(lambda: view.state.phase == "clients" and (self.root / "capturing").exists())
        screen.action("b")
        self.wait(lambda: view.state.phase == "networks")
        self.assertFalse(self.state()["monitor"])
        self.assertTrue(self.state()["up"])
        self.assertFalse(screen.ctx.wifi_deauth_session.activation_attempted)
        events = self.events()
        stop = events.index("airmon-ng stop wlan0mon")
        self.assertEqual(events[stop - 2], "capture_stopped")
        self.wait(lambda: demo.scan.call_count == 2)
        screen.action("b")
        screen._thread.join(4)
        self.assertFalse(screen.alive)
        self.assertEqual(self.events().count("airmon-ng stop wlan0mon"), 1)
        self.assertFalse(any("kick" in str(runner.cmd) for runner in self.runners))

    def test_broadcast_is_selectable_before_any_station_arrives(self):
        self.state(capture_silent=True)
        screen, view = self.start()
        screen.action("a")
        self.wait(lambda: view.state.phase == "clients" and (self.root / "capturing").exists())
        self.assertEqual(view.state.clients, (demo.BROADCAST,))
        screen.action("a")
        self.wait(lambda: view.state.phase == "kick")
        self.assertEqual(view.state.client, demo.BROADCAST)
        self.assertFalse((self.root / "capturing").exists())
        self.close(screen)
        self.assertFalse(self.state()["monitor"])

    def test_shutdown_stops_capture_before_restoring_and_is_idempotent(self):
        screen, view = self.start()
        self.clients(screen, view)
        self.close(screen)
        self.assertFalse(self.state()["monitor"])
        self.assertEqual(screen.ctx.status, "cancelled")
        count = len(self.events())
        demo.shutdown(screen.ctx)
        self.assertEqual(len(self.events()), count)
        self.assertFalse(view.handles_back)

    def test_driver_that_preserves_interface_name(self):
        self.state(same_name=True)
        screen, view = self.start()
        self.clients(screen, view)
        self.assertEqual(screen.ctx.wifi_deauth_session.monitor, "wlan0")
        self.close(screen)
        self.assertIn("airmon-ng stop wlan0", self.events())

    def test_original_down_state_is_preserved(self):
        self.state(up=False)
        screen, view = self.start()
        self.clients(screen, view)
        self.close(screen)
        self.assertFalse(self.state()["up"])
        self.assertNotIn("ip link set dev wlan0 up", self.events())

    def test_cancel_before_activation_never_changes_mode(self):
        screen, _ = self.start()
        self.close(screen)
        self.assertFalse(any("airmon-ng" in line for line in self.events()))

    def test_partial_activation_failure_still_restores(self):
        self.state(start_fail=True)
        screen, view = self.start()
        screen.action("a")
        screen._thread.join(6)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("activation denied", screen.ctx.error)
        self.assertFalse(self.state()["monitor"])
        self.assertIn("airmon-ng stop wlan0mon", self.events())

    def test_cancel_during_activation_still_restores(self):
        self.state(start_sleep=True)
        screen, view = self.start()
        screen.action("a")
        self.wait(lambda: self.state().get("monitor", False))
        self.close(screen)
        self.assertFalse(self.state()["monitor"])

    def test_activation_timeout_still_restores(self):
        self.state(start_sleep=True)
        screen, view = self.start()
        real_command = demo.command

        def timed(ctx, argv):
            timeout = 0.5 if argv[0] == self.commands["airmon-ng"] and argv[1] == "start" else 8
            with patch.object(demo, "SCRIPT_TIMEOUT", timeout):
                return real_command(ctx, argv)

        with patch.object(demo, "command", side_effect=timed):
            screen.action("a")
            screen._thread.join(6)
        self.assertFalse(screen.alive)
        self.assertIn("timed out", screen.ctx.error)
        self.assertIn("airmon-ng start wlan0 36", self.events())
        self.assertFalse(self.state()["monitor"])

    def test_capture_failure_restores_and_reports_error(self):
        self.state(capture_bad=True)
        screen, view = self.start()
        screen.action("a")
        screen._thread.join(6)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("Client scanner", screen.ctx.error)
        self.assertFalse(self.state()["monitor"])
        self.assertFalse((self.root / "capturing").exists())

    def test_cleanup_failure_remains_visible_and_preserves_recovery_state(self):
        self.state(stop_fail=True)
        screen, view = self.start()
        self.clients(screen, view)
        with patch("sys.stderr", io.StringIO()):
            self.close(screen)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("restoration denied", screen.ctx.error)
        self.assertTrue(screen.ctx.wifi_deauth_session.activation_attempted)
        self.state(stop_fail=False)
        # A shutdown context allows recovery even though the app was cancelled.
        screen.ctx._shutting_down = True
        try:
            demo.shutdown(screen.ctx)
        finally:
            screen.ctx._shutting_down = False
        self.assertFalse(self.state()["monitor"])

    def test_successful_stop_without_managed_mode_is_not_accepted(self):
        self.state(stop_noop=True)
        screen, view = self.start()
        self.clients(screen, view)
        with patch("sys.stderr", io.StringIO()):
            self.close(screen)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("was not restored", screen.ctx.error)
        self.assertTrue(screen.ctx.wifi_deauth_session.activation_attempted)

    def test_shared_adapter_or_initial_monitor_mode_is_not_owned(self):
        for values in ({"shared": True}, {"shared": False, "initial_mode": "monitor"}):
            with self.subTest(values=values):
                self.state(**values)
                screen, view = self.start()
                screen.action("a")
                screen._thread.join(4)
                self.assertFalse(screen.alive)
                self.assertEqual(screen.ctx.status, "error")
                self.assertFalse(screen.ctx.wifi_deauth_session.activation_attempted)
                self.assertFalse(any("airmon-ng" in line for line in self.events()))
                self.nav.pop()

    def test_missing_tool_and_failed_ap_scan_are_visible(self):
        self.commands.pop("airmon-ng")
        screen, view = self.start()
        screen._thread.join(3)
        self.assertIn("Missing command: airmon-ng", screen.ctx.error)
        self.assertEqual(self.events(), [])
        self.nav.pop()
        self.commands["airmon-ng"] = str(self.root / "airmon-ng")
        with patch.object(demo, "scan", side_effect=ValueError("scan denied")):
            screen, view = self.start()
            screen._thread.join(3)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("scan denied", screen.ctx.error)


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def test_handheld_layouts_broadcast_scroll_and_mac_selection(self):
        from deckfw.statusbar import StatusBar

        for size in ((320, 240), (640, 480)):
            with self.subTest(size=size):
                theme = Theme(*size)
                surface = pygame.Surface(size)
                bar = StatusBar(theme, DEFAULTS)
                line_h = theme.f_small.get_linesize() + theme.pad * 2
                app_rect = pygame.Rect(0, bar.height + 1, size[0],
                                       size[1] - bar.height - 1 - line_h)
                body = pygame.Rect(theme.pad, app_rect.y + line_h + theme.pad,
                                   app_rect.w - theme.pad * 2,
                                   app_rect.h - line_h - theme.pad * 2)
                view = demo.DemoView()
                view.interface = "wlan0"
                clients = (demo.BROADCAST, *[
                    replace(CLIENT, mac=f"12:DE:AD:00:00:{i:02X}", power_dbm=None)
                    for i in range(20)])
                view.transition("clients", ap=AP, clients=clients)
                with patch.object(theme, "render", wraps=theme.render) as render:
                    view.draw(surface, body, theme)
                texts = [call.args[1] for call in render.call_args_list]
                self.assertTrue(any("Broadcast" in text for text in texts))
                self.assertTrue(any("Observed" in text for text in texts))
                for _ in range(20):
                    view.action("down")
                view.action("right")
                view.draw(surface, body, theme)
                self.assertEqual(view.horizontal, min(80, view.max_horizontal))
                view.action("a")
                self.assertEqual(view.requests.get_nowait()[2], clients[-1])
                view.state = replace(view.state, clients=tuple(reversed(clients)))
                view.action("a")
                self.assertEqual(view.requests.get_nowait()[2], clients[-1])
                for client in (CLIENT, demo.BROADCAST):
                    view.transition("kick", ap=replace(AP, ssid="Long SSID " * 20), client=client)
                    for _ in range(100):
                        view.action("down")
                    with patch.object(theme, "render", wraps=theme.render) as render:
                        view.draw(surface, body, theme)
                    texts = [call.args[1] for call in render.call_args_list]
                    self.assertIn("No real device is disconnected.", texts)
                view.action("b")
                view.transition("clients", ap=AP, clients=(demo.BROADCAST,))
                self.assertEqual(demo.request(view), (None, None))
                view.action("a")
                self.assertEqual(view.requests.get_nowait()[2], demo.BROADCAST)


if __name__ == "__main__":
    unittest.main()
