from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import pygame

from deckfw import context
from deckfw.config import DEFAULTS
from deckfw.loader import discover
from deckfw.menu import default_menu
from deckfw.runner import Runner
from deckfw.screens import AppScreen, Navigator, Screen
from deckfw.theme import Theme
from apps._wifi_scan import AccessPoint, Snapshot, scan as real_scan

with patch.object(context, "_REGISTRY", []):
    from apps import wifi_deauth as demo


AP = AccessPoint("--Example: Wi\\Fi; $(not-a-command)", "WPA2",
                 "02:00:00:00:00:AB", 36, 5180, 90)
CLIENT = demo.Client("02:DE:AD:00:00:01", "Demo phone", 82)


def system_stubs(directory):
    """Test executables only; production supplies these commands separately."""
    for name, event in (("monairng", "monitor_started"), ("monsniff", "clients"),
                        ("kickerscript", "kick"), ("monairngreverse", "monitor_stopped")):
        script = Path(directory) / name
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys, time\n"
            "target = dict(arg[2:].split('=', 1) for arg in sys.argv[1:])\n"
            "target['channel'] = int(target['channel'])\n"
            f"event = {event!r}\n"
            "data = {'demo': True, 'event': event, 'target': target}\n"
            "if event == 'clients':\n"
            "    data['clients'] = [\n"
            "        {'mac': '02:DE:AD:00:00:01', 'name': 'Demo phone', 'signal': 82},\n"
            "        {'mac': '02:DE:AD:00:00:02', 'name': 'Demo laptop', 'signal': 65}]\n"
            "if event == 'kick':\n"
            "    data.update(elapsed=1, ticks=1)\n"
            "print(json.dumps(data), flush=True)\n"
            "if event == 'kick': time.sleep(30)\n"
        )
        script.chmod(0o755)
    return {name: str(Path(directory) / name) for name in demo.COMMANDS}


class ContractTests(unittest.TestCase):
    def test_form_detects_one_adapter_and_validates_scanner_limits(self):
        with patch.object(demo, "wireless_interfaces", return_value=("wlan0", "wlan1")):
            form = demo.setup()
        self.assertEqual(form.defaults(), {"interface": "wlan0", "band": "both", "interval": 15})
        self.assertEqual(form.fields[0].adjust("wlan0", 1), "wlan1")
        self.assertEqual(form.validate({"interface": "wlan1", "band": "5", "interval": 5}),
                         {"interface": "wlan1", "band": "5", "interval": 5})
        for key, value in (("interface", ""), ("band", "6"), ("interval", 4), ("interval", 121)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                form.validate({**form.defaults(), key: value})

    def test_system_scripts_round_trip_all_parameters_and_hidden_essid(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = system_stubs(directory)
            ctx = context.Context(context.AppSpec("Demo", lambda ctx: None))
            ctx.wifi_deauth_session = demo.Session("wlan0", commands)
            for ap in (AP, replace(AP, ssid="")):
                values = demo.parameters("wlan0", ap)
                for name, expected in (("monairng", "monitor_started"),
                                       ("monsniff", "clients"), ("monairngreverse", "monitor_stopped")):
                    data = demo.helper(ctx, name, values, expected)
                    self.assertEqual(data["target"], values)
                    if name == "monsniff":
                        clients = demo.clients_from(data)
                        self.assertEqual(len(clients), 2)
                        self.assertTrue(all(client.name.startswith("Demo ") for client in clients))
            with self.assertRaisesRegex(ValueError, "Unknown demo helper"):
                demo.helper_command("/usr/bin/real-command", values, commands)
            with patch.object(context.Context, "run",
                              return_value=context.RunResult(7, ["failed to simulate"])), \
                    self.assertRaisesRegex(ValueError, "monairng failed \\(7\\).*failed to simulate"):
                demo.helper(ctx, "monairng", values, "monitor_started")

    def test_system_resolution_requires_all_commands_and_keeps_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = system_stubs(directory)
            with patch.dict(os.environ, {"PATH": directory}):
                self.assertEqual(demo.resolve_commands(), commands)
                (Path(directory) / "monairngreverse").unlink()
                with self.assertRaisesRegex(ValueError, "Missing system demo command: monairngreverse"):
                    demo.resolve_commands()

    def test_malformed_demo_output_is_not_accepted(self):
        values = demo.parameters("wlan0", AP)
        valid = {"demo": True, "event": "clients", "target": values}
        for data in ({}, [], {**valid, "demo": False}, {**valid, "event": "kick"},
                     {**valid, "target": {**values, "channel": 1}}):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "invalid demo"):
                demo.event(json.dumps(data), "clients", values)
        with self.assertRaisesRegex(ValueError, "invalid demo JSON"):
            demo.event("not json", "clients", values)
        client = {"mac": CLIENT.mac, "name": CLIENT.name, "signal": CLIENT.signal}
        for clients in (None, {}, [None], [client, client], [{**client, "mac": "bad"}],
                        [{**client, "signal": True}], [{**client, "signal": 101}],
                        [{**client, "name": ""}]):
            with self.subTest(clients=clients), self.assertRaisesRegex(ValueError, "synthetic client"):
                demo.clients_from({"clients": clients})
        self.assertEqual(demo.clients_from({"clients": []}), ())
        ctx = context.Context(context.AppSpec("Demo", None))
        ctx.wifi_deauth_session = demo.Session("wlan0", {name: "/" + name for name in demo.COMMANDS})
        with patch.object(context.Context, "run", return_value=context.RunResult(0, [])), \
                self.assertRaisesRegex(ValueError, "expected one demo event"):
            demo.helper(ctx, "monsniff", values, "clients")

    def test_discovery_has_one_demo_and_one_scanner_without_helper_shortcuts(self):
        root = str(Path(__file__).resolve().parents[1])
        with patch.object(context, "_REGISTRY", []):
            specs = discover(root, DEFAULTS)
        names = [spec.name for spec in specs]
        self.assertEqual(names.count("Wifi Deauth"), 1)
        self.assertEqual(names.count("Wi-Fi scanner"), 1)
        self.assertFalse(any(name in names for name in
                             ("Monairng", "Monsniff", "Kickerscript", "Monairngreverse")))
        spec = next(spec for spec in specs if spec.name == "Wifi Deauth")
        self.assertEqual(spec.source, "native")
        network = next(item for item in default_menu(specs).items[0].menu.items
                       if item.label == "Network")
        self.assertTrue(any(item.spec is spec for item in network.menu.items))
        self.assertIsNotNone(spec.shutdown)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.commands = system_stubs(temporary.name)
        self.runners = []
        self.calls = []
        self.nav = Navigator()
        self.nav.push(Screen())
        self.addCleanup(self.nav.close_all)
        for patcher in (
            patch.object(demo, "wireless_interfaces", return_value=("wlan0",)),
            patch.object(demo.shutil, "which", side_effect=lambda name:
                         "/stub/nmcli" if name == "nmcli" else self.commands.get(name)),
            patch.object(demo, "scan", return_value=(AP,)),
            patch("deckfw.context.Runner", side_effect=self.runner),
            patch.object(demo, "helper", side_effect=self.helper),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def runner(self, *args, **kwargs):
        runner = Runner(*args, **kwargs)
        self.runners.append(runner)
        return runner

    # Retain the real implementation before setUp patches its module binding.
    real_helper = staticmethod(demo.helper)

    def helper(self, ctx, name, values, expected):
        self.calls.append((name, dict(values)))
        if name == "monairngreverse":
            self.assertTrue(all(runner._proc.poll() is not None for runner in self.runners))
        return self.real_helper(ctx, name, values, expected)

    def wait(self, predicate, message="Demo did not become ready"):
        deadline = time.monotonic() + 5
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
        self.wait(lambda: view.state.phase == "clients" and bool(view.state.clients))

    def kicking(self, screen, view):
        screen.action("a")
        self.wait(lambda: any(runner.cmd[0] == self.commands["kickerscript"] and runner.total > 0
                              for runner in self.runners))
        self.assertEqual(view.state.phase, "kick")
        return next(runner for runner in reversed(self.runners)
                    if runner.cmd[0] == self.commands["kickerscript"])

    def test_navigation_keeps_parameters_and_reaps_kick_before_cleanup(self):
        screen, view = self.start()
        demo.scan.assert_called_once_with(screen.ctx, "/stub/nmcli", "wlan0", "both")
        self.clients(screen, view)
        self.assertEqual(view.state.ap, AP)
        self.assertEqual(self.calls[0], ("monairng", demo.parameters("wlan0", AP)))
        screen.action("down")
        client = view.state.clients[1]
        runner = self.kicking(screen, view)
        self.assertEqual(runner.cmd, demo.helper_command(
            "kickerscript", demo.parameters("wlan0", AP, client), self.commands))
        screen.action("b")
        self.wait(lambda: view.state.phase == "clients")
        self.assertIsNotNone(runner._proc.poll())
        self.assertFalse(screen.ctx.cancelled)
        self.assertNotIn("monairngreverse", [name for name, _ in self.calls])
        screen.action("b")
        self.wait(lambda: view.state.phase == "networks")
        self.assertFalse(screen.ctx.cancelled)
        self.clients(screen, view)
        self.assertEqual([name for name, _ in self.calls].count("monairng"), 2)
        screen.action("b")
        self.wait(lambda: view.state.phase == "networks")
        screen.action("b")
        screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "cancelled")
        self.assertIsNone(screen.ctx.wifi_deauth_session.monitor_ap)
        self.assertEqual([name for name, _ in self.calls].count("monairngreverse"), 1)
        self.assertTrue(all(runner._proc.poll() is not None for runner in self.runners))
        self.assertTrue(all(runner.cmd[0] in self.commands.values() for runner in self.runners))
        screen.action("b")
        self.assertEqual(len(self.nav.stack), 1)

    def test_launcher_close_stops_kick_then_cleans_up_once(self):
        screen, view = self.start()
        self.clients(screen, view)
        runner = self.kicking(screen, view)
        self.nav.close_all()
        self.assertFalse(screen.alive)
        self.assertIsNotNone(runner._proc.poll())
        self.assertEqual([name for name, _ in self.calls].count("monairngreverse"), 1)

    def test_external_cancel_does_not_trap_back_in_a_finished_view(self):
        screen, view = self.start()
        self.clients(screen, view)
        runner = self.kicking(screen, view)
        screen.close()
        screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertIsNotNone(runner._proc.poll())
        self.assertFalse(view.handles_back)
        screen.action("b")
        self.assertEqual(len(self.nav.stack), 1)

    def test_initial_discovery_uses_existing_nmcli_scanner(self):
        executable = Path(self.commands["monairng"]).with_name("nmcli")
        fields = (AP.ssid, AP.security, AP.bssid, str(AP.channel), "5180 MHz", str(AP.signal))
        record = ":".join(value.replace("\\", "\\\\").replace(":", "\\:") for value in fields)
        executable.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "if sys.argv[-1] == 'radio': print('enabled')\n"
            "elif sys.argv[-2:] == ['device', 'status']: print('wlan0:wifi:disconnected')\n"
            f"else: print({record!r})\n")
        executable.chmod(0o755)
        with patch.object(demo, "scan", side_effect=real_scan), \
                patch.object(demo.shutil, "which", side_effect=lambda name:
                             str(executable) if name == "nmcli" else self.commands.get(name)):
            screen, view = self.start()
            self.assertEqual(view.networks.snapshot.networks, (AP,))
            network_command = self.runners[-1].cmd
            self.assertEqual(network_command[-7:],
                             ["device", "wifi", "list", "--rescan", "yes", "ifname", "wlan0"])
            self.assertIn("SSID,SECURITY,BSSID,CHAN,FREQ,SIGNAL", network_command)
            self.assertEqual(self.calls, [])
            screen.close()
            screen._thread.join(3)
        self.assertFalse(screen.alive)

    def test_cancel_before_activation_has_no_cleanup_command(self):
        screen, _ = self.start()
        screen.action("b")
        screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(self.calls, [])

    def test_missing_system_command_prevents_discovery_and_activation(self):
        self.commands.pop("monairngreverse")
        screen, view = self.start()
        screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("Missing system demo command: monairngreverse", view.state.title)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runners, [])
        demo.scan.assert_not_called()

    def test_one_shot_timeout_stops_process_before_reverse_cleanup(self):
        Path(self.commands["monairng"]).write_text(
            f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")

        def timed(ctx, name, values, expected):
            with patch.object(demo, "SCRIPT_TIMEOUT", 0.1 if name == "monairng" else 5):
                return self.helper(ctx, name, values, expected)

        with patch.object(demo, "helper", side_effect=timed):
            screen, view = self.start()
            screen.action("a")
            screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("monairng timed out", screen.ctx.error)
        self.assertTrue(all(runner._proc.poll() is not None for runner in self.runners))
        self.assertEqual([name for name, _ in self.calls].count("monairngreverse"), 1)
        self.assertIsNone(screen.ctx.wifi_deauth_session.monitor_ap)

    def test_cancellation_during_activation_stops_before_reverse_cleanup(self):
        Path(self.commands["monairng"]).write_text(
            f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        screen, view = self.start()
        screen.action("a")
        self.wait(lambda: any(runner.cmd[0] == self.commands["monairng"] for runner in self.runners))
        screen.close()
        screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "cancelled")
        self.assertTrue(all(runner._proc.poll() is not None for runner in self.runners))
        self.assertEqual([name for name, _ in self.calls].count("monairngreverse"), 1)

    def test_activation_or_client_failure_still_cleans_up(self):
        for failing in ("monairng", "monsniff"):
            with self.subTest(failing=failing):
                self.calls.clear()

                def fail(ctx, name, values, expected):
                    if name == failing:
                        self.calls.append((name, dict(values)))
                        raise ValueError(f"{name} test failure")
                    return self.helper(ctx, name, values, expected)

                with patch.object(demo, "helper", side_effect=fail):
                    screen, view = self.start()
                    screen.action("a")
                    screen._thread.join(3)
                self.assertFalse(screen.alive)
                self.assertEqual(screen.ctx.status, "error")
                self.assertIn(f"{failing} test failure", screen.ctx.error)
                self.assertEqual([name for name, _ in self.calls].count("monairngreverse"), 1)
                self.nav.pop()

    def test_bad_kick_telemetry_stops_process_and_surfaces_error(self):
        original = demo.helper_command

        def commands(name, values, resolved):
            if name == "kickerscript":
                return [sys.executable, "-u", "-c",
                        "import time; print('invalid telemetry', flush=True); time.sleep(30)"]
            return original(name, values, resolved)

        with patch.object(demo, "helper_command", side_effect=commands):
            screen, view = self.start()
            self.clients(screen, view)
            screen.action("a")
            screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("invalid demo JSON", screen.ctx.error)
        self.assertTrue(all(runner._proc.poll() is not None for runner in self.runners))
        self.assertEqual([name for name, _ in self.calls].count("monairngreverse"), 1)

    def test_cleanup_failure_is_visible(self):
        screen, view = self.start()
        self.clients(screen, view)

        def fail(ctx, name, values, expected):
            if name == "monairngreverse":
                raise ValueError("fake restoration failed")
            return self.helper(ctx, name, values, expected)

        with patch.object(demo, "helper", side_effect=fail), patch("sys.stderr"):
            screen.close()
            screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("fake restoration failed", screen.ctx.error)

    def test_failed_real_refresh_preserves_but_disables_stale_aps(self):
        with patch.object(demo, "scan", side_effect=[(AP,), ValueError("scan denied")]), \
                patch.object(demo.time, "monotonic", side_effect=lambda: time.perf_counter() * 1000):
            screen, view = self.start()
            screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(screen.ctx.status, "error")
        self.assertEqual(view.networks.snapshot.networks, (AP,))
        self.assertIn("STALE", view.networks.snapshot.message)
        screen.action("a")
        self.assertTrue(view.requests.empty())


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def test_tables_and_details_at_real_reduced_body_sizes(self):
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
                points = tuple(replace(AP, bssid=f"02:00:00:00:00:{i:02X}") for i in range(20))
                view.networks.snapshot = Snapshot(points, "20 real APs", interfaces=("wlan0",))
                for _ in range(19):
                    view.action("down")
                view.draw(surface, body, theme)
                self.assertEqual(view.networks.selected_bssid, points[-1].bssid)
                self.assertGreater(view.networks._top, 0)
                view.action("a")
                self.assertEqual(view.requests.get_nowait()[2], points[-1])

                clients = tuple(replace(CLIENT, mac=f"02:DE:AD:00:00:{i:02X}") for i in range(20))
                view.transition("clients", ap=AP, clients=clients)
                for _ in range(19):
                    view.action("down")
                with patch.object(theme, "render", wraps=theme.render) as render:
                    view.draw(surface, body, theme)
                texts = [call.args[1] for call in render.call_args_list]
                self.assertTrue(any(text.startswith("Invented clients") for text in texts))
                self.assertIn(clients[-1].mac, texts)
                view.action("right")
                self.assertEqual(view.horizontal, min(80, view.max_horizontal))
                view.draw(surface, body, theme)
                view.action("a")
                self.assertEqual(view.requests.get_nowait()[2], clients[-1])
                view.state = replace(view.state, clients=tuple(reversed(clients)))
                view.action("a")
                self.assertEqual(view.requests.get_nowait()[2], clients[-1])
                view.transition("kick", ap=replace(AP, ssid="Long SSID " * 20), client=CLIENT)
                for _ in range(100):
                    view.action("down")
                with patch.object(theme, "render", wraps=theme.render) as render:
                    view.draw(surface, body, theme)
                if size[0] == 320:
                    self.assertGreater(view.top, 0)
                texts = [call.args[1] for call in render.call_args_list]
                self.assertIn("No real device is disconnected.", texts)
                view.action("b")
                view.transition("clients", ap=AP)
                self.assertEqual(demo.request(view), (None, None))
                view.draw(surface, body, theme)
                self.assertFalse(view.action("a"))


if __name__ == "__main__":
    unittest.main()
