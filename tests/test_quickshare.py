import ipaddress
import json
import os
from pathlib import Path
import pwd
import tempfile
import threading
import unittest
from unittest.mock import patch

import pygame

from deckfw import context
from deckfw.context import AppSpec, Cancelled, Context, RunResult
from deckfw.menu import default_menu, Launch
from deckfw.theme import Theme
from apps import _quickshare_network as net

with patch.object(context, "_REGISTRY", []):
    from apps import quickshare as app


PREVIOUS = "11111111-1111-4111-8111-111111111111"


class FakeContext:
    def __init__(self):
        self.active = PREVIOUS
        self.profiles = {PREVIOUS}
        self.table = None
        self.calls = []
        self.logs = []
        self.fail = ""
        self.rules = ""
        self.password = ""
        self.credential_file = None
        self.created_uuid = ""
        self.args = []

    def log(self, text):
        self.logs.append(text)

    def run(self, cmd, **kwargs):
        self.calls.append(cmd)
        tool, args = Path(cmd[3]).name, cmd[4:]
        lines, stage = [], ""
        if tool == "nmcli":
            args = args[4:]
            if args[:2] == ["--get-values", "WIFI"]:
                lines = ["enabled"]
            elif args[:1] == ["--get-values"]:
                field = args[1]
                lines = {
                    "GENERAL.NM-MANAGED": ["yes"], "WIFI-PROPERTIES.AP": ["yes"],
                    "GENERAL.CON-UUID": [self.active or "--"],
                    "IP4.ADDRESS": ["192.168.10.1/24"],
                }[field]
            elif args[:2] == ["connection", "add"]:
                self.args = args
                self.created_uuid = args[args.index("connection.uuid") + 1]
                self.profiles.add(self.created_uuid)
                stage = "add"
            elif args[:2] == ["connection", "up"]:
                self.active = args[3]
                if "passwd-file" in args:
                    self.credential_file = Path(args[args.index("passwd-file") + 1])
                    self.password = self.credential_file.read_text()
                    if os.stat(self.credential_file).st_mode & 0o077:
                        raise AssertionError("Credential file is not private")
                stage = "up"
            elif args[:2] == ["connection", "down"]:
                stage = "down"
                if self.fail != stage:
                    self.active = ""
            elif args[:2] == ["connection", "delete"]:
                self.profiles.discard(args[3])
                stage = "delete"
            elif args[:2] == ["-g", "UUID"]:
                lines = sorted(self.profiles)
            elif args[:2] == ["-g", "802-11-wireless-security.key-mgmt"]:
                lines = [self.args[self.args.index("802-11-wireless-security.key-mgmt") + 1]]
            else:
                raise AssertionError(args)
        elif tool == "ip":
            lines = [json.dumps([])]
        elif tool == "iw":
            lines = ["\ttype AP"] if args[-1] == "info" else [
                "Station 02:00:00:00:00:01 (on wlan0)", "\tinactive time: 3 ms"]
        elif tool == "nft":
            if args[0] == "-f":
                self.rules = Path(args[1]).read_text()
                self.table = self.rules.split()[2]
                stage = "guard"
            elif args == ["-j", "list", "tables"]:
                tables = [{"table": {"name": self.table, "family": "inet"}}] if self.table else []
                lines = [json.dumps({"nftables": tables})]
            elif args[:2] == ["delete", "table"]:
                stage = "unguard"
                if self.fail != stage:
                    self.table = None
            elif args[:2] == ["list", "table"]:
                if self.table is None:
                    return RunResult(1, ["table missing"])
            else:
                raise AssertionError(args)
        else:
            raise AssertionError(tool)
        if stage and self.fail == stage:
            return RunResult(1, ["synthetic " + stage + " failure"])
        return RunResult(0, lines)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.ctx = FakeContext()
        self.hotspot = net.Hotspot(self.ctx, "wlan0", "192.168.10.0/24")
        self.hotspot.tools = {name: name for name in ("timeout", "nmcli", "nft", "ip", "iw")}
        self.hotspot.previous = PREVIOUS

    def test_security_and_cleanup_order_credentials_not_in_arguments(self):
        for security in ("open", "wpa2", "wpa3"):
            with self.subTest(security=security):
                self.setUp()
                self.hotspot.start("QuickShare", security, "NewPassword12")
                self.assertIn("save", self.ctx.args)
                self.assertEqual(self.ctx.args[self.ctx.args.index("save") + 1], "no")
                self.assertIn("ipv6.method", self.ctx.args)
                self.assertIn('iifname "wlan0" drop', self.ctx.rules)
                self.assertIn('oifname "wlan0" drop', self.ctx.rules)
                self.assertIn("tcp dport 8080 accept", self.ctx.rules)
                self.assertIn('iifname != "wlan0" ip daddr 192.168.10.1 '
                              'tcp dport 8080 drop', self.ctx.rules)
                self.assertNotIn("dport 53 accept", self.ctx.rules)
                self.assertNotIn("NewPassword12", str(self.ctx.calls))
                if security != "open":
                    self.assertIn("psk:NewPassword12", self.ctx.password)
                    self.assertFalse(self.ctx.credential_file.exists())
                    self.assertIn("sae" if security == "wpa3" else "wpa-psk", self.ctx.args)
                else:
                    self.assertNotIn("802-11-wireless-security.key-mgmt", self.ctx.args)
                self.assertEqual(self.hotspot.clients(),
                                 (("02:00:00:00:00:01", "IP unknown"),))
                self.hotspot.close()
                self.assertEqual(self.ctx.active, PREVIOUS)
                self.assertEqual(self.ctx.profiles, {PREVIOUS})
                self.assertIsNone(self.ctx.table)
                calls = str(self.ctx.calls)
                self.assertLess(calls.index("'connection', 'down'"),
                                calls.index("'delete', 'table'"))
                self.assertLess(calls.index("'delete', 'table'"),
                                calls.rindex("'connection', 'up'"))
                count = len(self.ctx.calls)
                self.hotspot.close()
                self.assertEqual(len(self.ctx.calls), count)

    def test_failed_or_cancelled_creation_is_cleaned(self):
        for stage in ("guard", "add", "up"):
            for cancelled in (False, True):
                with self.subTest(stage=stage, cancelled=cancelled):
                    self.setUp()
                    original = self.ctx.run
                    if cancelled:
                        def run(*args, **kwargs):
                            result = original(*args, **kwargs)
                            if result.returncode:
                                raise Cancelled()
                            return result
                        self.ctx.run = run
                    self.ctx.fail = stage
                    with self.assertRaises((RuntimeError, Cancelled)):
                        self.hotspot.start("QuickShare", "wpa2", "NewPassword12")
                    self.ctx.fail = ""
                    self.hotspot.close()
                    self.assertIsNone(self.ctx.table)
                    self.assertEqual(self.ctx.profiles, {PREVIOUS})
                    self.assertEqual(self.ctx.active, PREVIOUS)

    def test_failed_deactivation_retains_guard_and_reports_identifiers(self):
        self.hotspot.start("QuickShare", "open", "")
        self.ctx.fail = "down"
        with self.assertRaisesRegex(RuntimeError, self.hotspot.table):
            self.hotspot.close()
        self.assertIsNotNone(self.ctx.table)
        self.assertEqual(self.ctx.active, self.hotspot.uuid)

    def test_external_connection_is_not_overwritten(self):
        self.hotspot.start("QuickShare", "open", "")
        self.ctx.active = "22222222-2222-4222-8222-222222222222"
        self.hotspot.close()
        self.assertEqual(self.ctx.active, "22222222-2222-4222-8222-222222222222")
        self.assertTrue(any("changed elsewhere" in line for line in self.ctx.logs))

    def test_preflight_consent_privileges_and_interface_lock(self):
        original_open = os.open
        with tempfile.TemporaryDirectory() as directory:
            lock = str(Path(directory) / "interface.lock")
            def open_lock(path, flags, mode=0o600):
                return original_open(lock, flags, mode)
            with patch.object(net.sys, "platform", "linux"), \
                    patch.object(net.os, "geteuid", return_value=0), \
                    patch.object(net.os, "open", side_effect=open_lock), \
                    patch.object(net.os, "fstat", return_value=type("Info", (), {
                        "st_uid": 0, "st_mode": 0o100600})()), \
                    patch.object(net.shutil, "which", side_effect=lambda tool, **kw: tool):
                with self.assertRaisesRegex(ValueError, "disconnect consent"):
                    self.hotspot.preflight(False)
                self.hotspot.close()
                self.hotspot = net.Hotspot(self.ctx, "wlan0", "192.168.10.0/24")
                self.hotspot.preflight(True)
                record = json.loads(Path(lock).read_text())
                self.assertEqual(record["profile"], self.hotspot.uuid)
                self.assertEqual(record["previous"], PREVIOUS)
                other = net.Hotspot(self.ctx, "wlan0", "192.168.10.0/24")
                try:
                    with self.assertRaisesRegex(ValueError, "already owns"):
                        other.preflight(True)
                finally:
                    other.close()
                    self.hotspot.close()
                self.assertEqual(Path(lock).read_text(), "")
                Path(lock).write_text(json.dumps({
                    "profile": PREVIOUS, "table": "quickshare_old"}))
                stale = net.Hotspot(self.ctx, "wlan0", "192.168.10.0/24")
                try:
                    with self.assertRaisesRegex(ValueError, "Earlier QuickShare"):
                        stale.preflight(True)
                finally:
                    stale.close()
                self.assertEqual(json.loads(Path(lock).read_text())["profile"], PREVIOUS)
        with patch.object(net.sys, "platform", "linux"), \
                patch.object(net.os, "geteuid", return_value=1000), \
                self.assertRaises(PermissionError):
            net.Hotspot(self.ctx, "wlan0", net.SUBNETS[0]).preflight(True)

    def test_overlapping_addresses_and_vpn_routes_rejected(self):
        for subnet in net.SUBNETS:
            network = ipaddress.IPv4Network(subnet)
            address = str(network.network_address + 2)
            with self.subTest(subnet=subnet), self.assertRaisesRegex(ValueError, "overlaps"):
                net.check_overlap(subnet, [{"ifname": "eth0", "addr_info": [
                    {"family": "inet", "local": address, "prefixlen": 24}]}], [], "wlan0")
        with self.assertRaisesRegex(ValueError, "route"):
            net.check_overlap(net.SUBNETS[0], [], [{"dst": "192.168.0.0/16", "dev": "tun0"}],
                              "wlan0")
        net.check_overlap(net.SUBNETS[0], [], [{"dst": "default", "dev": "eth0"},
                                               {"dst": net.SUBNETS[0], "dev": "wlan0"}], "wlan0")

    def test_station_list_is_not_neighbor_cache(self):
        clients = net.stations(["Station 02:00:00:00:00:01 (on wlan0)",
                               "Station 02:00:00:00:00:02 (on wlan0)"], [
            {"lladdr": "02:00:00:00:00:01", "dst": "192.168.10.5"},
            {"lladdr": "02:00:00:00:00:03", "dst": "192.168.10.6"}])
        self.assertEqual(clients, (("02:00:00:00:00:01", "192.168.10.5"),
                                   ("02:00:00:00:00:02", "IP unknown")))
        with self.assertRaises(ValueError):
            net.stations(["Station invalid"], [])
        for interface in ("wlan0;reboot", "../foo", ""):
            with self.assertRaises(ValueError):
                net.Hotspot(self.ctx, interface, net.SUBNETS[0])


class FormAndViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.user = pwd.struct_passwd(("alice", "x", 1001, 1001, "",
                                      self.directory.name, "/bin/bash"))

    def form(self):
        with patch.object(app.network, "interfaces", return_value=("wlan0",)), \
                patch.object(app, "owners", return_value=(self.user,)), \
                patch.object(app, "invoking_user", return_value=self.user):
            return app.setup()

    def test_owner_defaults_and_open_consent(self):
        form = self.form()
        values = form.defaults()
        self.assertEqual(values["owner"], 1001)
        self.assertEqual(values["subnet"], "192.168.10.0/24")
        with patch.object(app, "owner", return_value=self.user):
            submitted = form.validate(values)
            self.assertNotIn("open_consent", submitted)
            values["security"] = "open"
            with self.assertRaisesRegex(ValueError, "nearby"):
                form.validate(values)
            values["open_consent"] = True
            self.assertEqual(form.validate(values)["security"], "open")
            values["ssid"] = "\u00e9" * 17
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                form.validate(values)
        with patch.object(app.network, "interfaces", return_value=()), \
                patch.object(app, "owners", return_value=()), \
                patch.object(app, "invoking_user", return_value=None):
            empty = app.setup()
        with self.assertRaisesRegex(ValueError, "adapter"):
            empty.validate(empty.defaults())

    def test_dashboard_tabs_scroll_at_both_sizes(self):
        pygame.font.init()
        board = app.Dashboard()
        board.details = tuple("Details " + "x" * 90 for _ in range(25))
        board.clients = tuple("02:00:00:00:00:{:02x} IP unknown".format(i) for i in range(40))
        for index in range(50):
            board.write("Upload complete {} {}".format(index, "long-name-" * 8))
        for size in ((320, 240), (640, 480)):
            theme = Theme(*size)
            surface = pygame.Surface(size)
            body = pygame.Rect(10, 55, size[0] - 20, size[1] - 90)
            for tab in range(3):
                self.assertEqual(board.tab, tab)
                board.draw(surface, body, theme)
                self.assertTrue(board.action("up"))
                board.draw(surface, body, theme)
                self.assertTrue(board.action("right"))
        self.assertNotIn("\n", board.logs._raw[-1][0])

    def test_network_menu_integration_without_custom_menu_changes(self):
        specs = [
            AppSpec("QuickShare", app.quickshare, group="Network", setup=app.setup),
            AppSpec("Wi-Fi scanner", lambda ctx: None, group="Network"),
            AppSpec("Other", lambda ctx: None, group="USB-OTG"),
        ]
        root = default_menu(specs)
        self.assertEqual([item.label for item in root.items], ["Apps", "Custom", "System", "Exit"])
        apps = root.items[0].menu
        group = next(item for item in apps.items if item.label == "Network")
        self.assertTrue(any(isinstance(item, Launch) and item.spec.name == "QuickShare"
                            for item in group.menu.items))

    def test_worker_cancellation_joins_service_then_cleans_network(self):
        params = self.form().defaults()
        ctx = Context(AppSpec("QuickShare", app.quickshare), params)
        ready = threading.Event()
        closed = []

        class FakeHotspot:
            address = "192.168.10.1"
            uuid, table = "test-profile", "test-table"

            def __init__(self, *args, **kwargs):
                pass

            def preflight(self, consent):
                pass

            def start(self, ssid, security, password):
                self.password = password

            def verify(self):
                ready.set()

            def clients(self):
                return ()

            def close(self):
                closed.append("network")

        def run_child(cmd, **kwargs):
            self.assertIn("-I", cmd)
            self.assertEqual(cmd[cmd.index("--uid") + 1], "1001")
            kwargs["on_line"](json.dumps({"event": "ready", "root": str(
                Path(self.user.pw_dir) / "QuickShare"), "free": 1024 ** 3,
                "uid": 1001, "gid": 1001}))
            while True:
                ctx.sleep(0.01)

        with patch.object(app.network, "Hotspot", FakeHotspot), \
                patch.object(ctx, "run", side_effect=run_child):
            session = app.Session(ctx, self.user)
            errors = []
            def work():
                try:
                    session.run()
                except Cancelled:
                    pass
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            self.assertTrue(ready.wait(3))
            ctx.cancel()
            worker.join(4)
            self.assertFalse(worker.is_alive())
            self.assertFalse(session.thread.is_alive())
            session.close()
        self.assertEqual(errors, [])
        self.assertEqual(closed, ["network"])
        self.assertEqual(len(session.hotspot.password), 12)
