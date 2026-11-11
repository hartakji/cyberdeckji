import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deckfw import context
from deckfw.config import DEFAULTS
from deckfw.loader import discover
from deckfw.menu import Launch, Menu, MenuScreen
from deckfw.screens import Navigator


class ParameterExampleTests(unittest.TestCase):
    def setUp(self):
        self.registry = patch.object(context, "_REGISTRY", [])
        self.registry.start()
        self.addCleanup(self.registry.stop)
        root = str(Path(__file__).resolve().parents[1])
        self.specs = discover(root, DEFAULTS)

    def test_custom_demo_uses_all_selected_values(self):
        spec = next(s for s in self.specs if s.name == "Custom Python demo")
        ctx = context.Context(spec, {
            "message": "A selected message", "mode": "relaxed", "steps": 3, "verbose": False,
        })
        with patch.object(ctx, "sleep") as sleep:
            spec.run(ctx)
        self.assertEqual(sleep.call_count, 3)
        sleep.assert_called_with(0.6)
        self.assertEqual(ctx._bar.value, 1)
        logs = [text for text, _ in ctx._log._raw]
        self.assertEqual(logs, ["A selected message", "Custom demo finished!"])

    def test_ping_monitor_uses_validated_host_and_count_as_arguments(self):
        spec = next(s for s in self.specs if s.name == "Ping monitor")
        form = spec.setup()
        params = form.validate({"host": "example.org", "count": 3})
        ctx = context.Context(spec, params)
        with patch.object(ctx, "run", return_value=context.RunResult(0, [])) as run:
            spec.run(ctx)
        self.assertEqual(run.call_args.args[0],
                         ["ping", "-c", "3", "-i", "0.5", "example.org"])
        # Host validation excludes option prefixes, so this is not shell/option interpolation.
        with self.assertRaises(ValueError):
            form.validate({"host": "-f", "count": 3})

    def test_real_shell_example_setup_and_execution_without_network(self):
        spec = next(s for s in self.specs if s.name == "Network quick check")
        with tempfile.TemporaryDirectory() as directory:
            fakebin = Path(directory)
            for name, source in (
                ("ping", "#!/bin/sh\nprintf 'reply time=1.23 ms\\n'\n"),
                ("sleep", "#!/bin/sh\nexit 0\n"),
            ):
                path = fakebin / name
                path.write_text(source)
                path.chmod(0o755)
            nav = Navigator()
            menu = MenuScreen(Menu("Main", [Launch(spec)]))
            nav.push(menu)
            self.addCleanup(nav.close_all)
            menu.action("a")
            setup = nav.top
            setup.values.update(host="example.org", rounds=2)
            with patch.dict(os.environ, {"PATH": directory + os.pathsep + os.environ["PATH"]}):
                setup.submit()
                app = nav.top
                app._thread.join(5)
            self.assertFalse(app.alive)
            self.assertEqual(app.ctx.status, "done")
            self.assertEqual(app.ctx.params, {"host": "example.org", "rounds": 2})
            progress = next(child for child in app.ctx.root.children
                            if hasattr(child, "value") and hasattr(child, "label"))
            self.assertEqual(progress.value, 1)
            self.assertIn("finished", str(app.ctx._log._raw))


if __name__ == "__main__":
    unittest.main()
