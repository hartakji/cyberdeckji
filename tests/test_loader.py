import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from deckfw import context
from deckfw.config import DEFAULTS
from deckfw.loader import discover
from deckfw.menu import Launch, Menu, MenuScreen, Submenu, default_menu
from deckfw.screens import Navigator
from deckfw.forms import SetupScreen


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deck apps ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "apps").mkdir()
        self.registry = patch.object(context, "_REGISTRY", [])
        self.registry.start()
        self.addCleanup(self.registry.stop)
        modules = set(sys.modules)
        def unload_apps():
            for name in set(sys.modules) - modules:
                if name.startswith("deck_app_"):
                    sys.modules.pop(name)
        self.addCleanup(unload_apps)

    def write(self, name, text, executable=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if executable:
            path.chmod(0o755)
        return path

    def specs(self, **overrides):
        return discover(str(self.root), dict(DEFAULTS, **overrides))

    def launch(self, spec):
        nav = Navigator()
        screen = MenuScreen(Menu("Test", [Launch(spec)]))
        nav.push(screen)
        self.addCleanup(nav.close_all)
        screen.action("a")
        app = nav.top
        app._thread.join(timeout=5)
        self.assertFalse(app.alive, "script did not finish")
        return app.ctx

    def output(self, ctx):
        return "\n".join(text for text, _ in ctx._log._raw)

    def test_plain_python_is_not_executed_until_menu_selection(self):
        marker = self.root / "custom" / "started"
        self.write("custom/my_scan.py",
                   "from pathlib import Path\n"
                   "Path('started').write_text('yes')\n"
                   "print('python ran')\n")
        specs = self.specs()
        self.assertEqual([(s.name, s.group, s.source) for s in specs],
                         [("My Scan", "", "custom")])
        self.assertFalse(marker.exists())
        ctx = self.launch(specs[0])
        self.assertEqual(marker.read_text(), "yes")
        self.assertEqual(ctx.status, "done")
        self.assertIn("python ran", self.output(ctx))

    def test_bash_without_header_or_executable_bit(self):
        self.write("custom/check.sh", "values=(one two)\nprintf '%s\\n' \"${values[1]}\"\n")
        ctx = self.launch(self.specs()[0])
        self.assertEqual(ctx.status, "done")
        self.assertEqual(self.output(ctx), "two")

    def test_shebang_overrides_extension_and_protocol_updates_widgets(self):
        self.write("custom/custom.sh",
                   f"#!{sys.executable}\n"
                   "# deck: Custom name\n# group: Network\n"
                   "print('@progress 50 Halfway')\n"
                   "print('@status working')\nprint('hello')\n")
        specs = self.specs()
        self.assertEqual((specs[0].name, specs[0].group), ("Custom name", "Network"))
        ctx = self.launch(specs[0])
        self.assertEqual(ctx.status, "done")
        self.assertEqual(ctx.note, "working")
        self.assertIn("hello", self.output(ctx))
        self.assertEqual(ctx.root.children[0].value, 0.5)

    def test_framework_decorator_forms(self):
        sources = [
            ("from deckfw import app", "@app(name='Direct')"),
            ("from deckfw import app as register", "@register(name='Alias')"),
            ("import deckfw as fw", "@fw.app(name='Module')"),
            ("from deckfw import app", "@app"),
        ]
        for i, (imports, decorator) in enumerate(sources):
            self.write(f"apps/framework_{i}.py",
                       f"{imports}\n{decorator}\ndef example(ctx):\n    ctx.log('framework')\n")
        specs = self.specs()
        self.assertEqual({s.name for s in specs}, {"Direct", "Alias", "Module", "Example"})
        for spec in specs:
            with self.subTest(name=spec.name):
                self.assertIn("framework", self.output(self.launch(spec)))

    def test_custom_scripts_and_default_menu_are_launchable(self):
        self.write("custom/tool.py", "# deck: Custom tool\nprint('custom ran')\n")
        specs = self.specs()
        menu = default_menu(specs)
        tools = next(item for item in menu.items if item.label == "Custom")
        self.assertIsInstance(tools, Submenu)
        self.assertIs(tools.menu.items[0].spec, specs[0])
        self.assertIn("custom ran", self.output(self.launch(specs[0])))

    def test_nonzero_exit_is_error_not_done(self):
        self.write("custom/failure.sh", "echo 'failure detail' >&2\nexit 7\n")
        ctx = self.launch(self.specs()[0])
        self.assertEqual(ctx.status, "error")
        self.assertIn("failure detail", self.output(ctx))
        self.assertIn("[exit code 7]", self.output(ctx))

    def test_missing_interpreter_shows_error(self):
        self.write("custom/missing.sh", "#!/no/such/interpreter\n")
        ctx = self.launch(self.specs()[0])
        self.assertEqual(ctx.status, "error")
        self.assertIn("could not start", self.output(ctx))

    def test_executable_without_extension(self):
        self.write("custom/tool", "#!/bin/sh\necho 'executable ran'\n", executable=True)
        ctx = self.launch(self.specs()[0])
        self.assertEqual(ctx.status, "done")
        self.assertIn("executable ran", self.output(ctx))

    def test_ignores_helpers_data_and_directories(self):
        self.write("apps/_helper.py", "raise RuntimeError('must not run')\n")
        self.write("apps/.hidden.sh", "exit 1\n")
        self.write("apps/data.txt", "data")
        self.write("apps/subdir/nested.py", "print('nested')\n")
        self.assertEqual(self.specs(), [])

    def test_broken_app_does_not_hide_other_entries(self):
        self.write("apps/broken.py", "def invalid syntax\n")
        self.write("apps/valid.py", "print('ok')\n")
        specs = self.specs()
        self.assertEqual({s.name for s in specs}, {"! broken.py", "Valid"})
        broken = next(s for s in specs if s.name.startswith("!"))
        self.assertIn("SyntaxError", self.output(self.launch(broken)))

    def test_same_configured_directory_is_not_scanned_twice(self):
        self.write("apps/once.py", "print('once')\n")
        self.assertEqual(len(self.specs(custom_dir="apps")), 1)

    def test_custom_directory_override(self):
        self.write("my_tools/check.py", "print('override ran')\n")
        specs = self.specs(custom_dir="my_tools")
        self.assertEqual(specs[0].source, "custom")
        self.assertIn("override ran", self.output(self.launch(specs[0])))

    def test_custom_framework_app_is_tagged_as_custom(self):
        self.write("custom/native_style.py",
                   "from deckfw import app\n"
                   "@app(name='Custom UI', group='System')\n"
                   "def run(ctx):\n    ctx.log('custom UI ran')\n")
        spec = self.specs()[0]
        self.assertEqual(spec.source, "custom")
        self.assertIn("custom UI ran", self.output(self.launch(spec)))

    def test_broken_custom_framework_app_stays_in_tools(self):
        self.write("custom/broken.py", "def invalid syntax\n")
        spec = self.specs()[0]
        self.assertEqual(spec.source, "custom")
        self.assertTrue(spec.name.startswith("!"))

    def test_running_script_can_be_cancelled(self):
        self.write("custom/wait.py", "import time\nprint('ready', flush=True)\ntime.sleep(30)\n")
        nav = Navigator()
        screen = MenuScreen(Menu("Test", [Launch(self.specs()[0])]))
        nav.push(screen)
        self.addCleanup(nav.close_all)
        screen.action("a")
        app = nav.top
        deadline = time.monotonic() + 5
        while app.ctx._log is None or "ready" not in self.output(app.ctx):
            self.assertLess(time.monotonic(), deadline, "script did not start")
            time.sleep(0.01)
        app.action("b")
        app._thread.join(timeout=5)
        self.assertFalse(app.alive)
        self.assertEqual(app.ctx.status, "cancelled")
        with app.ctx._lock:
            self.assertEqual(app.ctx._runners, [])

    def test_parameterized_python_and_bash_receive_literal_environment(self):
        message = "quotes ' \" spaces ; $(touch should_not_exist) & |"
        for extension, source in (
            ("py", "import os\nprint(os.environ['DECK_PARAM_MESSAGE'])\n"
             "print(os.environ['DECK_PARAM_COUNT'])\n"
             "print(os.environ['DECK_PARAM_ENABLED'])\nprint('@status received')\n"),
            ("sh", "printf '%s\\n' \"$DECK_PARAM_MESSAGE\" \"$DECK_PARAM_COUNT\" "
             "\"$DECK_PARAM_ENABLED\"\necho '@status received'\n"),
        ):
            self.write(f"custom/literal.{extension}", source)
            self.write(f"custom/literal.{extension}.params.json", json.dumps({
                "fields": [
                    {"type": "text", "key": "message", "label": "Message", "default": message},
                    {"type": "number", "key": "count", "label": "Count",
                     "default": 2, "minimum": 1, "maximum": 3},
                    {"type": "toggle", "key": "enabled", "label": "Enabled", "default": True},
                ],
            }))
        specs = self.specs()
        self.assertEqual(len(specs), 2)
        before = dict(os.environ)
        for spec in specs:
            with self.subTest(script=spec.name):
                nav = Navigator()
                menu = MenuScreen(Menu("Main", [Launch(spec)]))
                nav.push(menu)
                self.addCleanup(nav.close_all)
                menu.action("a")
                setup = nav.top
                self.assertIsInstance(setup, SetupScreen)
                setup.submit()
                app = nav.top
                app._thread.join(5)
                self.assertFalse(app.alive)
                self.assertEqual(app.ctx.status, "done")
                self.assertEqual(self.output(app.ctx).splitlines(), [message, "2", "true"])
                self.assertEqual(app.ctx.note, "received")
        self.assertEqual(dict(os.environ), before)
        self.assertFalse((self.root / "custom" / "should_not_exist").exists())

    def test_malformed_sidecar_never_executes_script(self):
        marker = self.root / "custom" / "started"
        self.write("custom/blocked.py",
                   "from pathlib import Path\nPath('started').write_text('bad')\n")
        self.write("custom/blocked.py.params.json", '{"fields": [{"type": "invalid"}]}')
        spec = self.specs()[0]
        self.assertTrue(spec.name.startswith("!"))
        ctx = self.launch(spec)
        self.assertIn("Unknown field type", self.output(ctx))
        self.assertFalse(marker.exists())

    def test_parameterized_native_and_custom_scripts_preserve_sources(self):
        for folder in ("apps", "custom"):
            self.write(f"{folder}/script.sh", "echo 'ok'\n")
            self.write(f"{folder}/script.sh.params.json", '{"fields": []}')
        specs = self.specs()
        self.assertEqual({s.source for s in specs}, {"native", "custom"})
        self.assertTrue(all(s.setup is not None for s in specs))

    def test_parameterized_script_preserves_cwd_and_cancellation(self):
        self.write("custom/wait.py",
                   "import os, time\n"
                   "print(os.getcwd(), flush=True)\n"
                   "print(os.environ['DECK_PARAM_MESSAGE'], flush=True)\ntime.sleep(30)\n")
        self.write("custom/wait.py.params.json", json.dumps({
            "fields": [{"type": "text", "key": "message",
                        "label": "Message", "default": "ready"}],
        }))
        spec = self.specs()[0]
        nav = Navigator()
        menu = MenuScreen(Menu("Main", [Launch(spec)]))
        nav.push(menu)
        self.addCleanup(nav.close_all)
        menu.action("a")
        setup = nav.top
        setup.submit()
        app = nav.top
        deadline = time.monotonic() + 5
        while app.ctx._log is None or "ready" not in self.output(app.ctx):
            self.assertLess(time.monotonic(), deadline, "parameterized script did not start")
            time.sleep(0.01)
        self.assertIn(str((self.root / "custom").resolve()), self.output(app.ctx))
        app.action("b")
        app._thread.join(5)
        self.assertFalse(app.alive)
        self.assertEqual(app.ctx.status, "cancelled")
        app.action("b")
        self.assertIs(nav.top, setup)


if __name__ == "__main__":
    unittest.main()
