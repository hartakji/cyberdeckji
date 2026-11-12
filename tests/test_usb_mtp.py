import threading
import unittest
from unittest.mock import patch

from apps import _usb_otg, usb_mtp
from deckfw import context
from deckfw.config import DEFAULTS
from deckfw.loader import discover
from deckfw.menu import default_menu
from deckfw.screens import AppScreen


class UsbMtpTests(unittest.TestCase):
    def test_service_commands_disable_paging(self):
        ctx = context.Context(context.AppSpec("MTP", usb_mtp.usb_mtp))
        for action in ("show", "start", "stop"):
            with self.subTest(action=action), patch.object(
                    ctx, "run", return_value=context.RunResult(0, [])) as run:
                _usb_otg.command(ctx, action, "usb-mtp")
                run.assert_called_once_with(
                    ["timeout", "--kill-after=2s", "10s", "systemctl",
                     "--no-pager", action, "usb-mtp"])

    def test_discovered_under_usb_otg(self):
        with patch.object(context, "_REGISTRY", []):
            specs = discover(".", DEFAULTS)
        apps = default_menu(specs).items[0].menu
        group = next(item for item in apps.items if item.label == "USB-OTG")
        self.assertIn("USB-MTP", [item.label for item in group.menu.items])

    def test_cancel_stops_started_service(self):
        started = threading.Event()
        calls = []

        def run(ctx, *args):
            calls.append(args)
            if args[0] == "show":
                return context.RunResult(0, ["LoadState=loaded", "ActiveState=inactive"])
            if args[0] == "start":
                started.set()
            return context.RunResult(0, [])

        spec = context.AppSpec("MTP", usb_mtp.usb_mtp, shutdown=_usb_otg.shutdown)
        with patch.object(_usb_otg, "command", side_effect=run):
            screen = AppScreen(spec)
            self.assertTrue(started.wait(2))
            screen.ctx.cancel()
            screen._thread.join(2)
        self.assertFalse(screen.alive)
        self.assertEqual([call[0] for call in calls], ["show", "start", "stop"])

    def test_existing_service_is_not_stopped(self):
        spec = context.AppSpec("MTP", usb_mtp.usb_mtp, shutdown=_usb_otg.shutdown)
        with patch.object(_usb_otg, "command",
                          return_value=context.RunResult(
                              0, ["LoadState=loaded", "ActiveState=active"])) as command:
            screen = AppScreen(spec)
            screen._thread.join(2)
        self.assertEqual(screen.ctx.status, "error")
        self.assertEqual(command.call_count, 1)

    def test_start_failure_still_attempts_cleanup(self):
        calls = []

        def run(ctx, *args):
            calls.append(args[0])
            if args[0] == "show":
                return context.RunResult(0, ["LoadState=loaded", "ActiveState=inactive"])
            if args[0] == "start":
                raise RuntimeError("start failed")
            return context.RunResult(0, [])

        spec = context.AppSpec("MTP", usb_mtp.usb_mtp, shutdown=_usb_otg.shutdown)
        with patch.object(_usb_otg, "command", side_effect=run):
            screen = AppScreen(spec)
            screen._thread.join(2)
        self.assertEqual(screen.ctx.status, "error")
        self.assertEqual(calls, ["show", "start", "stop"])


if __name__ == "__main__":
    unittest.main()
