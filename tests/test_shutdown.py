import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deckfw import app
from deckfw.context import AppSpec
from deckfw.loader import discover
from deckfw.config import DEFAULTS
from deckfw.screens import AppScreen, Navigator


class ShutdownTests(unittest.TestCase):
    def test_registration_and_normal_completion(self):
        calls = []
        def cleanup(ctx):
            calls.append(ctx.params["value"])
        with patch("deckfw.context.register") as register:
            @app(shutdown=cleanup)
            def run(ctx):
                pass
        self.assertIs(register.call_args.args[0].shutdown, cleanup)
        screen = AppScreen(AppSpec("Example", run, shutdown=cleanup), {"value": 3})
        screen._thread.join(2)
        self.assertEqual(calls, [3])
        self.assertEqual(screen.ctx.status, "done")

    def test_cancelled_hook_can_run_commands_and_is_not_interrupted_by_b(self):
        started, cleaning, release = threading.Event(), threading.Event(), threading.Event()
        results = []
        def run(ctx):
            started.set()
            ctx.sleep(30)
        def cleanup(ctx):
            cleaning.set()
            release.wait(2)
            results.append(ctx.run([sys.executable, "-c", "print('restored')"]).returncode)
        screen = AppScreen(AppSpec("Example", run, shutdown=cleanup))
        nav = Navigator()
        nav.push(screen)
        self.assertTrue(started.wait(2))
        screen.action("b")
        self.assertTrue(cleaning.wait(2))
        screen.action("b")
        screen.close()
        release.set()
        screen._thread.join(3)
        self.assertFalse(screen.alive)
        self.assertEqual(results, [0])
        self.assertEqual(screen.ctx.status, "cancelled")

    def test_crash_still_runs_hook_and_cleanup_failure_is_reported(self):
        calls = []
        def fail(ctx):
            raise ValueError("work failed")
        def cleanup(ctx):
            calls.append("cleanup")
            raise RuntimeError("restore failed")
        with patch("sys.stderr"):
            screen = AppScreen(AppSpec("Example", fail, shutdown=cleanup))
            screen._thread.join(2)
        self.assertEqual(calls, ["cleanup"])
        self.assertEqual(screen.ctx.status, "error")
        self.assertIn("work failed", screen.ctx.error)
        self.assertIn("restore failed", screen.ctx.error)

    def test_script_shutdown_header_and_cwd(self):
        from deckfw import context
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = root / "custom"
            custom.mkdir()
            (custom / "demo.py").write_text(
                "# shutdown: _cleanup.py\nprint('work')\n")
            (custom / "_cleanup.py").write_text(
                "from pathlib import Path\nPath('restored').write_text('yes')\n")
            with patch.object(context, "_REGISTRY", []):
                specs = discover(directory, DEFAULTS)
            self.assertEqual(len(specs), 1)
            screen = AppScreen(specs[0])
            screen._thread.join(3)
            self.assertFalse(screen.alive)
            self.assertEqual((custom / "restored").read_text(), "yes")

    def test_gui_close_waits_for_hook(self):
        started = threading.Event()
        calls = []
        def run(ctx):
            started.set()
            ctx.sleep(30)
        screen = AppScreen(AppSpec("Example", run, shutdown=lambda ctx: calls.append("closed")))
        nav = Navigator()
        nav.push(screen)
        self.assertTrue(started.wait(2))
        nav.close_all()
        self.assertFalse(screen.alive)
        self.assertEqual(calls, ["closed"])

    def test_script_signal_cleanup_finishes_before_shutdown_helper(self):
        from deckfw import context
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "custom"
            folder.mkdir()
            (folder / "work.py").write_text(
                "# shutdown: _cleanup.py\n"
                "import signal, time\nfrom pathlib import Path\n"
                "def stop(signum, frame):\n"
                "    time.sleep(1.7)\n"
                "    Path('signal_done').write_text('yes')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "Path('ready').write_text('yes')\n"
                "time.sleep(30)\n")
            (folder / "_cleanup.py").write_text(
                "from pathlib import Path\n"
                "assert Path('signal_done').exists()\n"
                "Path('helper_done').write_text('yes')\n")
            with patch.object(context, "_REGISTRY", []):
                spec = discover(directory, DEFAULTS)[0]
            screen = AppScreen(spec)
            deadline = time.monotonic() + 3
            while not (folder / "ready").exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            screen.ctx.cancel()
            screen._thread.join(5)
            self.assertFalse(screen.alive)
            self.assertTrue((folder / "helper_done").exists())
            self.assertEqual(screen.ctx.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
