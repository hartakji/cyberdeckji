import ipaddress
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pygame

from deckfw import context
from deckfw.screens import AppScreen, Navigator
from deckfw.theme import Theme
from deckfw.loader import discover
from deckfw.config import DEFAULTS
from deckfw.menu import default_menu

with patch.object(context, "_REGISTRY", []):
    from apps import port_scanner as scanner


LAN = (ipaddress.IPv4Network("192.168.1.0/24"),)
HOST = "192.168.1.10"
XML = """<nmaprun><host><address addr="192.168.1.10" addrtype="ipv4"/>
<ports><extraports state="closed" count="1023"/>
<port protocol="tcp" portid="22"><state state="open"/>
<service name="ssh" method="table"/></port></ports></host>
<runstats><finished exit="success"/></runstats></nmaprun>"""


class ParserTests(unittest.TestCase):
    def test_interfaces_and_form(self):
        address = lambda ip: SimpleNamespace(family=socket.AF_INET, address=ip,
                                             netmask="255.255.255.0")
        with patch.object(scanner.sys, "platform", "linux"), \
                patch.object(scanner.psutil, "net_if_stats", return_value={
                    "eth0": SimpleNamespace(isup=True), "wlan0": SimpleNamespace(isup=True),
                    "lo": SimpleNamespace(isup=True), "down": SimpleNamespace(isup=False)}), \
                patch.object(scanner.psutil, "net_if_addrs", return_value={
                    "eth0": [address("192.168.1.2")], "wlan0": [address("10.0.0.2")],
                    "lo": [address("127.0.0.1")], "down": [address("10.1.0.2")]}):
            self.assertEqual(sorted(scanner.interfaces()), ["eth0", "wlan0"])
            form = scanner.setup()
            self.assertEqual(form.defaults(), {"interface": "eth0", "ports": "1-1024"})
            self.assertEqual(form.fields[0].adjust("eth0", 1), "wlan0")
            for value in scanner.RANGES:
                self.assertEqual(form.validate({"interface": "eth0", "ports": value})["ports"], value)
        with patch.object(scanner.sys, "platform", "darwin"), self.assertRaises(ValueError):
            scanner.interfaces()
        with patch.object(scanner.sys, "platform", "linux"), \
                patch.object(scanner.psutil, "net_if_stats", return_value={}), \
                patch.object(scanner.psutil, "net_if_addrs", return_value={}), \
                self.assertRaisesRegex(ValueError, "No active"):
            scanner.interfaces()

    def test_arp_records(self):
        line = HOST + "\t02:00:00:00:00:01\tVendor & Sons"
        hosts = scanner.parse_hosts(["Interface: eth0", line, line, "Ending arp-scan"], LAN)
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0].vendor, "Vendor & Sons")
        self.assertEqual(scanner.parse_hosts(["Starting arp-scan", "Ending arp-scan"], LAN), ())
        for invalid in ("192.168.1.999 bad", HOST + " bad",
                        "10.0.0.2 02:00:00:00:00:01",
                        "192.168.1.255 02:00:00:00:00:01"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                scanner.parse_hosts([invalid], LAN)

    def test_xml_and_invalid_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.xml"
            path.write_text(XML)
            rows, counts = scanner.parse_ports(path, HOST, "1-1024")
            self.assertEqual(rows, (("22", "TCP", "ssh"),))
            self.assertEqual(counts, {"closed": 1023, "open": 1})
            for xml in (XML.replace('exit="success"', 'exit="error"'),
                        XML.replace(HOST, "192.168.1.11"), "<nmaprun>",
                        XML.replace('protocol="tcp"', 'protocol="udp"'),
                        XML.replace('portid="22"', 'portid="0"'),
                        XML.replace('count="1023"', 'count="1"')):
                path.write_text(xml)
                with self.subTest(xml=xml), self.assertRaises(ValueError):
                    scanner.parse_ports(path, HOST, "1-1024")
            path.write_text(XML.replace('state="open"', 'state="filtered"'))
            rows, counts = scanner.parse_ports(path, HOST, "1-1024")
            self.assertEqual(rows, ())
            self.assertEqual(counts["filtered"], 1)


class WorkflowTests(unittest.TestCase):
    def test_select_scan_back_and_ranges(self):
        for port_range in scanner.RANGES:
            output_paths = []
            calls = []
            def execute(ctx, args):
                calls.append(args)
                if args[0] == "arp-scan":
                    return [HOST + " 02:00:00:00:00:01 Vendor"]
                path = Path(args[args.index("-oX") + 1])
                output_paths.append(path)
                count = int(port_range.split("-")[1]) - 1
                path.write_text(XML.replace('count="1023"', f'count="{count}"'))
                return []
            with patch.object(scanner, "interfaces", return_value={"eth0": LAN}), \
                    patch.object(scanner.shutil, "which", side_effect=lambda name: name), \
                    patch.object(scanner, "command", side_effect=execute):
                screen = AppScreen(context.AppSpec("Ports", scanner.port_scanner),
                                   {"interface": "eth0", "ports": port_range})
                nav = Navigator()
                nav.push(SimpleNamespace(close=lambda: None))
                nav.push(screen)
                try:
                    table = self.wait_view(screen, "devices")
                    table.action("a")
                    self.wait_view(screen, "results")
                    self.assertIn("B devices", screen.hints)
                    self.assertEqual(table.view.rows, (("22", "TCP", "ssh"),))
                    screen.action("b")
                    self.wait_view(screen, "devices")
                    self.assertFalse(screen.ctx.cancelled)
                    self.assertEqual(calls[0], ["arp-scan", "--interface", "eth0", "--localnet"])
                    self.assertIn("-sT", calls[1])
                    self.assertEqual(calls[1][-1], HOST)
                    self.assertEqual(calls[1][calls[1].index("-p") + 1], port_range)
                    self.assertTrue(all(not path.exists() for path in output_paths))
                finally:
                    nav.close_all()
                self.assertFalse(screen.alive)

    def wait_view(self, screen, phase):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if screen.ctx.root.children:
                table = screen.ctx.root.children[0]
                if table.view.phase == phase:
                    return table
            time.sleep(0.01)
        self.fail(f"Did not reach {phase}: {screen.ctx.error}")

    def test_failures_and_cancel_cleanup(self):
        with patch.object(scanner, "interfaces", return_value={"eth0": LAN}), \
                patch.object(scanner.shutil, "which", return_value=None):
            screen = AppScreen(context.AppSpec("Ports", scanner.port_scanner),
                               {"interface": "eth0", "ports": "1-1024"})
            screen._thread.join(2)
            self.assertEqual(screen.ctx.status, "error")
            self.assertIn("Install", screen.ctx.error)
        paths, started = [], threading.Event()
        def execute(ctx, args):
            if args[0] == "arp-scan":
                return [HOST + " 02:00:00:00:00:01 Vendor"]
            paths.append(Path(args[args.index("-oX") + 1]))
            paths[0].write_text("partial")
            started.set()
            ctx.sleep(30)
        with patch.object(scanner, "interfaces", return_value={"eth0": LAN}), \
                patch.object(scanner.shutil, "which", side_effect=lambda name: name), \
                patch.object(scanner, "command", side_effect=execute):
            screen = AppScreen(context.AppSpec("Ports", scanner.port_scanner),
                               {"interface": "eth0", "ports": "1-1024"})
            self.wait_view(screen, "devices").action("a")
            self.assertTrue(started.wait(2))
            screen.action("b")
            screen._thread.join(2)
            self.assertFalse(screen.alive)
            self.assertEqual(screen.ctx.status, "cancelled")
            self.assertFalse(paths[0].exists())

    def test_command_failure(self):
        ctx = context.Context(context.AppSpec("Ports", lambda ctx: None))
        with patch.object(ctx, "run", return_value=context.RunResult(1, ["Permission denied"])), \
                self.assertRaisesRegex(ValueError, "Permission denied"):
            scanner.command(ctx, ["arp-scan"])

    def test_native_menu_registration(self):
        with patch.object(context, "_REGISTRY", []):
            specs = discover(str(Path(__file__).resolve().parents[1]), DEFAULTS)
        spec = next(spec for spec in specs if spec.name == "Port scanner")
        self.assertEqual(spec.source, "native")
        network = next(item for item in default_menu(specs).items[0].menu.items
                       if item.label == "Network")
        self.assertTrue(any(item.spec is spec for item in network.menu.items))

    def test_nmap_error_returns_to_devices_and_empty_discovery(self):
        def execute(ctx, args):
            if args[0] == "arp-scan":
                return [HOST + " 02:00:00:00:00:01 Vendor"]
            raise ValueError("Nmap permission denied")
        with patch.object(scanner, "interfaces", return_value={"eth0": LAN}), \
                patch.object(scanner.shutil, "which", side_effect=lambda name: name), \
                patch.object(scanner, "command", side_effect=execute):
            screen = AppScreen(context.AppSpec("Ports", scanner.port_scanner),
                               {"interface": "eth0", "ports": "1-1024"})
            try:
                table = self.wait_view(screen, "devices")
                table.action("a")
                self.wait_view(screen, "results")
                self.assertIn("permission denied", table.view.error)
                self.assertEqual(screen.ctx.status, "error")
                screen.action("b")
                self.wait_view(screen, "devices")
            finally:
                screen.close()
                screen._thread.join(2)
        with patch.object(scanner, "interfaces", return_value={"eth0": LAN}), \
                patch.object(scanner.shutil, "which", side_effect=lambda name: name), \
                patch.object(scanner, "command", return_value=[]):
            screen = AppScreen(context.AppSpec("Ports", scanner.port_scanner),
                               {"interface": "eth0", "ports": "1-1024"})
            try:
                table = self.wait_view(screen, "devices")
                self.assertEqual(table.view.rows, ())
                self.assertFalse(table.action("a"))
            finally:
                screen.close()
                screen._thread.join(2)

    def test_tables_at_handheld_sizes(self):
        pygame.font.init()
        for size in ((320, 240), (640, 480)):
            table = scanner.ScanTable()
            table.view = scanner.View("devices", "eth0: 20 devices", rows=tuple(
                (f"192.168.1.{i}", "02:00:00:00:00:01", "Long vendor " * 20)
                for i in range(1, 21)))
            for _ in range(15):
                table.action("down")
            theme = Theme(*size)
            surface = pygame.Surface(size)
            body = pygame.Rect(0, 30, size[0], size[1] - 90)
            table.draw(surface, body, theme)
            table.action("right")
            self.assertEqual(table.offset, min(80, table._max_offset))
            table.draw(surface, body, theme)
            table.action("a")
            self.assertEqual(table.requests.get_nowait(), ("scan", "192.168.1.16"))
            table.view = scanner.View("results", "22 open, 1023 closed",
                                      ("Port", "Protocol", "Usual service"),
                                      (("22", "TCP", "ssh"),))
            table.draw(surface, body, theme)
            self.assertTrue(table.action("b"))

    def test_real_stub_processes_and_cancellation(self):
        for cancel in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                arp = root / "arp-scan"
                nmap = root / "nmap"
                arp.write_text("#!/usr/bin/env python3\n"
                               f"print('{HOST} 02:00:00:00:00:01 Vendor')\n")
                nmap.write_text(
                    "#!/usr/bin/env python3\nimport sys, time\nfrom pathlib import Path\n"
                    "output = Path(sys.argv[sys.argv.index('-oX') + 1])\n"
                    f"output.write_text({XML!r})\n"
                    f"Path({str(root / 'output-path')!r}).write_text(str(output))\n"
                    + ("time.sleep(30)\n" if cancel else ""))
                arp.chmod(0o755)
                nmap.chmod(0o755)
                with patch.object(scanner, "interfaces", return_value={"eth0": LAN}), \
                        patch.object(scanner.shutil, "which", side_effect=lambda name: str(root / name)):
                    screen = AppScreen(context.AppSpec("Ports", scanner.port_scanner),
                                       {"interface": "eth0", "ports": "1-1024"})
                    try:
                        self.wait_view(screen, "devices").action("a")
                        if cancel:
                            deadline = time.monotonic() + 3
                            while not (root / "output-path").exists():
                                self.assertLess(time.monotonic(), deadline)
                                time.sleep(0.01)
                        else:
                            self.assertEqual(self.wait_view(screen, "results").view.rows,
                                             (("22", "TCP", "ssh"),))
                    finally:
                        screen.close()
                        screen._thread.join(3)
                    self.assertFalse(screen.alive)
                    self.assertFalse(Path((root / "output-path").read_text()).exists())


if __name__ == "__main__":
    unittest.main()
