import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import pygame

from deckfw import context
from deckfw.config import DEFAULTS
from deckfw.menu import Launch, Menu, MenuScreen, default_menu
from deckfw.loader import discover
from deckfw.screens import Navigator
from deckfw.theme import Theme

with patch.object(context, "_REGISTRY", []):
    from apps import wifi_scanner as scanner


def escaped(value):
    return value.replace("\\", "\\\\").replace(":", "\\:")


def record(ssid="Example", bssid="02:00:00:00:00:01", channel=1, frequency=2412,
           signal=70, security="WPA2"):
    return ":".join(escaped(str(value)) for value in
                    (ssid, security, bssid, channel, str(frequency) + " MHz", signal))


class ParserTests(unittest.TestCase):
    def test_bands_channels_hidden_unicode_and_escaped_ssids(self):
        lines = [
            record(r"Office:Wi\Fi"),
            record("Guest", "02:00:00:00:00:02", 36, 5180, 90),
            record("", "02:00:00:00:00:03", 14, 2484, 20),
            record("Caf\u00e9", "02:00:00:00:00:04", 149, 5745, 65),
            record("Six GHz", "02:00:00:00:00:05", 1, 5955, 100),
        ]
        networks = scanner.parse_networks(lines)
        self.assertEqual(len(networks), 4)
        self.assertEqual([ap.band for ap in networks], ["5", "2.4", "5", "2.4"])
        self.assertEqual(networks[1].ssid, r"Office:Wi\Fi")
        self.assertEqual(networks[1].security, "WPA2")
        self.assertEqual(networks[-1].ssid, "")
        self.assertEqual(networks[-1].security, "WPA2")
        open_network = scanner.parse_networks([record(security="--")])[0]
        self.assertEqual(open_network.security, "--")
        self.assertEqual({ap.channel for ap in scanner.parse_networks(lines, "2.4")}, {1, 14})
        self.assertEqual({ap.channel for ap in scanner.parse_networks(lines, "5")}, {36, 149})
        self.assertEqual(scanner.parse_networks([]), ())
        self.assertEqual(scanner.parse_networks([lines[-1]]), ())

    def test_deduplicate_bssid_but_not_ssid_and_sort_ties(self):
        networks = scanner.parse_networks([
            record(signal=30), record(signal=80),
            record(bssid="02:00:00:00:00:02", signal=80),
            record(bssid="02:00:00:00:ab:cd", signal=60),
            record(bssid="02:00:00:00:AB:CD", signal=10),
        ])
        self.assertEqual(len(networks), 3)
        self.assertEqual([ap.signal for ap in networks], [80, 80, 60])
        self.assertEqual(networks[-1].bssid, "02:00:00:00:AB:CD")

    def test_invalid_records_are_not_empty_success(self):
        for line in [
            "warning from command", "a:b:c:d:e:f",
            record(bssid="not-a-bssid"), record(channel=0), record(channel="oops"),
            record(signal=101), record(signal="-1"), record(signal="nan"),
            record(frequency=0), record(frequency="5.18 GHz"),
            "bad\\q", "trailing\\",
        ]:
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, "nmcli row 1"):
                scanner.parse_networks([line])
        with self.assertRaisesRegex(ValueError, "Unknown band"):
            scanner.parse_networks([], "6")
        bare = record().replace("2412 MHz", "2412")
        self.assertEqual(scanner.parse_networks([bare])[0].frequency, 2412)

    def test_detected_interface_choices_and_refresh_bounds(self):
        with patch.object(scanner, "wireless_interfaces", return_value=("wlan0", "wlp2s0")):
            form = scanner.setup()
        self.assertEqual(form.defaults(), {"interface": "", "band": "both", "interval": 15})
        interface = form.fields[0]
        self.assertIsInstance(interface, scanner.Choice)
        self.assertEqual(interface.adjust("", 1), "wlan0")
        self.assertEqual(interface.adjust("wlan0", 1), "wlp2s0")
        self.assertEqual(interface.adjust("wlp2s0", 1), "")
        self.assertEqual(form.validate({"interface": "wlp2s0", "band": "both",
                                        "interval": 5})["interval"], 5)
        with self.assertRaises(ValueError):
            form.validate({"interface": "", "band": "both", "interval": 4})
        with self.assertRaises(ValueError):
            form.validate({"interface": "unknown", "band": "both", "interval": 15})

    def test_wireless_detection_filters_and_sorts_sysfs_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wlp2s0" / "phy80211").mkdir(parents=True)
            (root / "wlan0" / "wireless").mkdir(parents=True)
            (root / "eth0").mkdir()
            (root / "lo").mkdir()
            with patch.object(scanner.sys, "platform", "linux"), \
                    patch.object(scanner, "Path", return_value=root):
                self.assertEqual(scanner.wireless_interfaces(), ("wlan0", "wlp2s0"))

    def test_detection_errors_are_visible(self):
        with patch.object(scanner.sys, "platform", "darwin"), \
                self.assertRaisesRegex(ValueError, "requires Linux"):
            scanner.setup()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(scanner.sys, "platform", "linux"), \
                patch.object(scanner, "Path", return_value=Path(directory)), \
                self.assertRaisesRegex(ValueError, "No wireless interfaces"):
            scanner.setup()
        with patch.object(scanner.sys, "platform", "linux"), \
                patch.object(scanner, "Path") as path:
            path.return_value.iterdir.side_effect = PermissionError("denied")
            with self.assertRaisesRegex(ValueError, "Cannot detect.*denied"):
                scanner.setup()


class ScanTests(unittest.TestCase):
    def setUp(self):
        with patch.object(scanner, "wireless_interfaces", return_value=("wlan0",)):
            params = scanner.setup().defaults()
        self.ctx = context.Context(context.AppSpec("Scan", scanner.wifi_scanner),
                                   params)

    def run_results(self, radio=None, devices=None, networks=None):
        return [
            context.RunResult(0, radio if radio is not None else ["enabled"]),
            context.RunResult(0, devices if devices is not None else ["wlan0:wifi:disconnected"]),
            context.RunResult(0, networks if networks is not None else [record()]),
        ]

    def test_command_flags_locale_and_interface(self):
        with patch.object(self.ctx, "run", side_effect=self.run_results()) as run:
            result = scanner.scan(self.ctx, "/usr/bin/nmcli", "wlan0", "both")
        self.assertEqual(len(result), 1)
        command = run.call_args.args[0]
        self.assertEqual(command[-7:],
                         ["device", "wifi", "list", "--rescan", "yes", "ifname", "wlan0"])
        self.assertIn("SSID,SECURITY,BSSID,CHAN,FREQ,SIGNAL", command)
        self.assertEqual(command[1:9], ["--terse", "--escape", "yes", "--colors",
                                       "no", "--wait", "20", "--fields"])
        self.assertEqual(run.call_args.kwargs, {"log": False, "env": scanner.ENV})
        with patch.object(self.ctx, "run", side_effect=self.run_results()) as run:
            scanner.scan(self.ctx, "nmcli", "", "2.4")
        self.assertNotIn("ifname", run.call_args.args[0])

    def test_preflight_failures(self):
        for results, message in [
            (self.run_results(radio=["disabled"]), "radio is disabled"),
            (self.run_results(devices=[]), "No available"),
            (self.run_results(devices=["eth0:ethernet:connected"]), "No available"),
            (self.run_results(devices=["wlan0:wifi:unmanaged"]), "No available"),
            (self.run_results(devices=["wlan0:wifi:unavailable"]), "No available"),
            (self.run_results(devices=["malformed"]), "Invalid nmcli"),
        ]:
            with self.subTest(message=message), \
                    patch.object(self.ctx, "run", side_effect=results), \
                    self.assertRaisesRegex(ValueError, message):
                scanner.scan(self.ctx, "nmcli", "", "both")
        with patch.object(self.ctx, "run", side_effect=self.run_results()), \
                self.assertRaisesRegex(ValueError, "No NetworkManager.*wlan1"):
            scanner.scan(self.ctx, "nmcli", "wlan1", "both")
        with patch.object(self.ctx, "run",
                          side_effect=self.run_results(devices=["wlan0:wifi:unmanaged"])), \
                self.assertRaisesRegex(ValueError, "wlan0 is unmanaged"):
            scanner.scan(self.ctx, "nmcli", "wlan0", "both")

    def test_nmcli_nonzero_errors_include_diagnostic_and_exit_code(self):
        for code, message in [(3, "Timeout expired"), (8, "NetworkManager is not running"),
                              (1, "Not authorized to scan"), (-1, "could not start")]:
            with self.subTest(code=code), patch.object(
                    self.ctx, "run", return_value=context.RunResult(code, [message])), \
                    self.assertRaisesRegex(ValueError, message):
                scanner.scan(self.ctx, "nmcli", "", "both")

    def test_platform_and_missing_tool_are_visible_errors(self):
        with patch.object(scanner.sys, "platform", "darwin"):
            scanner.wifi_scanner(self.ctx)
        self.assertEqual(self.ctx.status, "error")
        self.assertIn("requires Linux", self.ctx.root.children[0].snapshot.error)
        with patch.object(scanner.sys, "platform", "linux"), \
                patch.object(scanner.shutil, "which", return_value=None):
            scanner.wifi_scanner(self.ctx)
        self.assertIn("Install NetworkManager", self.ctx.root.children[-1].snapshot.error)

    def test_periodic_snapshot_and_failed_refresh_preserve_stale_results(self):
        observations = []
        ap = scanner.parse_networks([record()])

        def capture_sleep(interval):
            observations.append(self.ctx.root.children[0].snapshot)
            self.assertEqual(interval, 15)

        with patch.object(scanner.sys, "platform", "linux"), \
                patch.object(scanner.shutil, "which", return_value="nmcli"), \
                patch.object(scanner, "scan", side_effect=[ap, (), ap, ValueError("Permission denied")]), \
                patch.object(self.ctx, "sleep", side_effect=capture_sleep):
            scanner.wifi_scanner(self.ctx)
        self.assertEqual([len(s.networks) for s in observations], [1, 0, 1])
        self.assertEqual(self.ctx.status, "error")
        stale = self.ctx.root.children[0].snapshot
        self.assertEqual(stale.networks, ap)
        self.assertIn("STALE", stale.message)
        self.assertEqual(stale.error, "Permission denied")


class NetworkListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def test_render_scroll_and_anchor_across_refresh(self):
        view = scanner.NetworkList()
        points = scanner.parse_networks([
            record("Long SSID " * 10, f"02:00:00:00:00:{i:02x}", signal=100 - i)
            for i in range(20)
        ])
        for size in ((320, 240), (640, 480)):
            surface, theme = pygame.Surface(size), Theme(*size)
            body = pygame.Rect(0, 30, size[0], size[1] - 60)
            view.snapshot = scanner.Snapshot(points, "20 APs", updated="12:00:00")
            view.draw(surface, body, theme)
            self.assertEqual(len(view._networks), 20)
            for _ in range(15):
                view.action("down")
            anchor = view._networks[view._top].bssid
            view.snapshot = scanner.Snapshot(tuple(reversed(points)), "Updated", updated="12:00:15")
            view.draw(surface, body, theme)
            self.assertIn(anchor, [ap.bssid for ap in
                                   view._networks[view._top:view._top + view._rows]])
            view.action("right")
            self.assertEqual(view._horizontal, min(80, view._max_horizontal))
            view.draw(surface, body, theme)
            view.action("left")
            self.assertFalse(view.action("a"))
            view.snapshot = scanner.Snapshot(points, "STALE results",
                                             error="Error details " * 30, updated="12:00:00")
            view.draw(surface, body, theme)
            self.assertEqual(len(view._networks), 20)
            view.snapshot = scanner.Snapshot(message="0 APs", updated="12:00:00")
            view.draw(surface, body, theme)
            self.assertEqual(view._networks, ())
            self.assertEqual(view._top, 0)

    def test_table_headers_full_bssid_and_interfaces_are_rendered(self):
        points = scanner.parse_networks([record(security="WPA1 WPA2 WPA3")])
        for size in ((320, 240), (640, 480)):
            theme = Theme(*size)
            view = scanner.NetworkList()
            view.snapshot = scanner.Snapshot(points, "1 AP", updated="12:00:00",
                                             interfaces=("wlan0", "wlp2s0"))
            with patch.object(theme, "render", wraps=theme.render) as render:
                view.draw(pygame.Surface(size), pygame.Rect(0, 30, size[0], size[1] - 60), theme)
            texts = [call.args[1] for call in render.call_args_list]
            self.assertIn("Interface: wlan0, wlp2s0", texts)
            header_start = texts.index("BSSID / SSID")
            self.assertEqual(texts[header_start:header_start + 4],
                             ["BSSID / SSID", "Enc", "Ch + Band", "Signal"])
            for value in ("Enc", "Ch + Band", "Signal", "1 / 2.4G",
                          "02:00:00:00:00:01", "Example"):
                self.assertIn(value, texts)
            self.assertIn("WPA1 WPA2 WPA3", texts)
            self.assertNotIn("MHz", texts)
            self.assertEqual(view._max_horizontal, 0)

    def test_actual_automatic_interfaces_are_published(self):
        ctx = context.Context(context.AppSpec("Scan", scanner.wifi_scanner))
        results = [
            context.RunResult(0, ["enabled"]),
            context.RunResult(0, ["wlan0:wifi:connected", "wlp2s0:wifi:disconnected",
                                  "wlan1:wifi:unmanaged"]),
            context.RunResult(0, [record()]),
        ]
        callback = []
        with patch.object(ctx, "run", side_effect=results):
            scanner.scan(ctx, "nmcli", "", "both", on_interfaces=callback.append)
        self.assertEqual(callback, [("wlan0", "wlp2s0")])


class ScannerIntegrationTests(unittest.TestCase):
    def test_native_discovery_and_network_menu(self):
        root = str(Path(__file__).resolve().parents[1])
        with patch.object(context, "_REGISTRY", []):
            specs = discover(root, DEFAULTS)
        spec = next(s for s in specs if s.name == "Wi-Fi scanner")
        self.assertEqual(spec.source, "native")
        network = next(item for item in default_menu(specs).items[0].menu.items
                       if item.label == "Network")
        self.assertTrue(any(item.spec is spec for item in network.menu.items))

    def exercise_cancellation(self, block):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "nmcli"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, time\n"
                "args = sys.argv[1:]\n"
                "if args[-1] == 'radio': print('enabled')\n"
                "elif args[-2:] == ['device', 'status']: print('wlan0:wifi:disconnected')\n"
                "else:\n"
                f"    print({json.dumps(record())}, flush=True)\n"
                + ("    time.sleep(30)\n" if block else "")
            )
            executable.chmod(0o755)
            spec = context.AppSpec("Scanner", scanner.wifi_scanner, setup=scanner.setup)
            nav = Navigator()
            nav.push(MenuScreen(Menu("Main", [Launch(spec)])))
            self.addCleanup(nav.close_all)
            with patch.object(scanner, "wireless_interfaces", return_value=("wlan0",)):
                nav.top.action("a")
            setup = nav.top
            with patch.object(scanner.sys, "platform", "linux"), \
                    patch.object(scanner.shutil, "which", return_value=str(executable)):
                setup.submit()
                app = nav.top
                deadline = time.monotonic() + 5
                while True:
                    self.assertLess(time.monotonic(), deadline, "Scanner did not become ready")
                    if block:
                        with app.ctx._lock:
                            runners = list(app.ctx._runners)
                        if runners and "SSID,SECURITY,BSSID,CHAN,FREQ,SIGNAL" in runners[0].cmd:
                            if runners[0].total:
                                break
                    elif app.ctx.root.children and app.ctx.root.children[0].snapshot.updated:
                        break
                    time.sleep(0.01)
                app.action("b")
                app._thread.join(3)
            self.assertFalse(app.alive)
            self.assertEqual(app.ctx.status, "cancelled")
            self.assertFalse(any(path.name == "unexpected" for path in Path(directory).iterdir()))
            if block:
                runners[0]._proc.wait(timeout=3)
                self.assertIsNotNone(runners[0]._proc.returncode)
            app.action("b")
            self.assertIs(nav.top, setup)

    def test_cancellation_during_nmcli_scan(self):
        self.exercise_cancellation(block=True)

    def test_cancellation_between_scans(self):
        self.exercise_cancellation(block=False)


if __name__ == "__main__":
    unittest.main()
