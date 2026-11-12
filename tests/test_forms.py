import os
import unittest
from unittest.mock import Mock, patch

import pygame

from deckfw import Choice, Form, Number, Text, Toggle
from deckfw.context import AppSpec
from deckfw.forms import SetupScreen, TextEditor
from deckfw.inputmap import InputMapper
from deckfw.config import DEFAULTS
from deckfw.menu import Launch, Menu, MenuScreen
from deckfw.screens import AppScreen, Navigator
from deckfw.theme import Theme
from deckfw.widgets import Label


class FormScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def setUp(self):
        self.calls = []
        self.spec = AppSpec("Example", lambda ctx: self.calls.append(ctx.params), setup=lambda: Form([
            Text("host", "Host", default="localhost", format="host", help="Enter a host"),
            Choice("mode", "Mode", [("Fast", "fast"), ("Slow", "slow")], default="fast"),
            Number("count", "Count", 1, minimum=1, maximum=3),
            Toggle("enabled", "Enabled"),
        ]))
        self.nav = Navigator()
        self.menu = MenuScreen(Menu("Main", [Launch(self.spec)]))
        self.nav.push(self.menu)
        self.addCleanup(self.nav.close_all)
        self.menu.action("a")
        self.setup = self.nav.top

    def test_no_execution_until_submit_and_snapshot_does_not_change(self):
        self.assertIsInstance(self.setup, SetupScreen)
        self.assertEqual(self.calls, [])
        self.setup.selected = 1
        self.setup.action("right")
        self.setup.selected = 2
        self.setup.action("right")
        self.setup.selected = 3
        self.setup.action("left")
        self.setup.selected = 4
        self.setup.action("a")
        app = self.nav.top
        self.assertIsInstance(app, AppScreen)
        app._thread.join(2)
        self.assertFalse(app.alive)
        self.assertEqual(self.calls, [
            {"host": "localhost", "mode": "slow", "count": 2, "enabled": True}
        ])
        self.setup.values["host"] = "other"
        self.assertEqual(app.ctx.params["host"], "localhost")
        self.setup.submit()
        self.assertEqual(len(self.calls), 1)
        app.action("b")
        self.assertIs(self.nav.top, self.setup)
        self.setup.submit()
        self.nav.top._thread.join(2)
        self.assertEqual(self.calls[-1]["host"], "other")
        self.nav.top.action("b")
        self.setup.action("b")
        self.menu.action("a")
        self.assertEqual(self.nav.top.values["host"], "localhost")

    def test_invalid_value_blocks_run_and_focuses_field(self):
        self.setup.values["host"] = "-invalid"
        self.setup.selected = 4
        self.setup.action("a")
        self.assertIs(self.nav.top, self.setup)
        self.assertEqual(self.setup.selected, 0)
        self.assertIn("Host:", self.setup.error)
        self.assertEqual(self.calls, [])

    def test_button_only_edit_and_cancel(self):
        self.setup.action("a")
        editor = self.nav.top
        self.assertIsInstance(editor, TextEditor)
        original = self.setup.values["host"]
        editor.selected = editor.keys.index("a")
        editor.action("a")
        editor.action("b")
        self.assertEqual(self.setup.values["host"], original)
        self.setup.action("a")
        editor = self.nav.top
        editor.selected = editor.keys.index("DEL")
        for _ in range(len(original)):
            editor.action("a")
        for char in "host":
            # Move exclusively with controller actions to the required key.
            while editor.keys[editor.selected] != char:
                editor.action("right")
            editor.action("a")
        while editor.keys[editor.selected] != "OK":
            editor.action("right")
        editor.action("a")
        self.assertIs(self.nav.top, self.setup)
        self.assertEqual(self.setup.values["host"], "host")

    def test_physical_text_edit_cursor_delete_and_validation(self):
        self.setup.action("a")
        editor = self.nav.top
        editor.value, editor.cursor = "", 0
        editor.text_event(pygame.event.Event(pygame.TEXTINPUT, text="abc"))
        editor.text_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_LEFT))
        editor.text_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DELETE))
        editor.text_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_BACKSPACE))
        editor.text_event(pygame.event.Event(pygame.TEXTINPUT, text="q"))
        self.assertEqual(editor.value, "aq")
        editor.text_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))
        self.assertEqual(self.setup.values["host"], "aq")
        self.setup.action("a")
        editor = self.nav.top
        editor.value = ""
        editor.accept()
        self.assertIs(self.nav.top, editor)
        self.assertTrue(editor.error)
        editor.text_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
        self.assertEqual(self.setup.values["host"], "aq")

    def test_direct_legacy_launch_and_bad_setup(self):
        for setup, expected in ((None, "done"), (lambda: "not a form", "error"),
                                (lambda: Form([Text("x", "X")]), "error")):
            with self.subTest(setup=setup):
                spec = AppSpec("Legacy", lambda ctx: ctx.log("executed"), setup=setup)
                with patch("sys.stderr"):
                    Launch(spec).activate(self.menu)
                app = self.nav.top
                app._thread.join(2)
                self.assertEqual(app.ctx.status, expected)
                if setup is not None:
                    self.assertIn("setup error", app.spec.name)
                    self.assertIn("Traceback", str(app.ctx._log._raw))
                self.nav.pop()

    def test_forms_and_editor_render_with_scrolling(self):
        fields = [Text("field" + str(i), "Long label " + str(i), default="x" * 100)
                  for i in range(16)]
        spec = AppSpec("Long form", lambda ctx: None, setup=lambda: Form(fields))
        screen = SetupScreen(spec)
        for size in ((320, 240), (640, 480)):
            surface = pygame.Surface(size)
            theme = Theme(*size)
            # Use the actual reduced body height left by status bar and hints.
            body = pygame.Rect(0, 30, size[0], size[1] - 60)
            for selected in (0, 10, 16):
                screen.selected = selected
                screen.error = "A useful validation message that wraps onto the next line"
                screen.draw(surface, body, theme)
            editor = TextEditor(fields[0], fields[0].default, lambda value: None)
            for selected in (0, len(editor.keys) - 1):
                editor.selected = selected
                editor.draw(surface, body, theme)

    def test_labels_center_vertically_without_changing_default_alignment(self):
        for size in ((320, 240), (640, 480)):
            theme = Theme(*size)
            rect = pygame.Rect(10, 20, 200, 40)
            for align in ("left", "center", "right"):
                with self.subTest(size=size, align=align):
                    surface = Mock()
                    Label("Example", align=align, valign="center").draw(surface, rect, theme)
                    image, (x, y) = surface.blit.call_args.args
                    self.assertLessEqual(abs(y + image.get_height() / 2 - rect.centery), 0.5)
                    expected_x = {"left": rect.x,
                                  "center": rect.x + (rect.w - image.get_width()) // 2,
                                  "right": rect.right - image.get_width()}[align]
                    self.assertEqual(x, expected_x)
            surface = Mock()
            Label("Legacy").draw(surface, rect, theme)
            self.assertEqual(surface.blit.call_args.args[1], rect.topleft)

    def test_conditional_fields_are_hidden_skipped_and_omitted_from_submit(self):
        calls = []
        form = Form([
            Toggle("whole", "Whole drive"),
            Choice("table", "Table", [("MBR", "mbr"), ("GPT", "gpt")], "mbr"),
            Toggle("confirm", "Confirm"),
        ], visible_when={"table": lambda values: values["whole"]})
        spec = AppSpec("Conditional", lambda ctx: calls.append(ctx.params), setup=lambda: form)
        screen = SetupScreen(spec)
        nav = Navigator()
        self.addCleanup(nav.close_all)
        nav.push(MenuScreen(Menu("Main", [])))
        nav.push(screen)
        screen.action("down")
        self.assertEqual(form.visible_fields(screen.values)[screen.selected].key, "confirm")
        screen.action("up")
        screen.action("right")
        self.assertEqual([field.key for field in form.visible_fields(screen.values)],
                         ["whole", "table", "confirm"])
        screen.action("down")
        screen.action("right")
        self.assertEqual(screen.values["table"], "gpt")
        screen.action("up")
        screen.action("left")
        screen.values["table"] = "invalid hidden value"
        screen.selected = 2
        screen.action("a")
        nav.top._thread.join(2)
        self.assertEqual(calls, [{"whole": False, "confirm": False}])


class TextRoutingTests(unittest.TestCase):
    def test_keyboard_shortcuts_are_scoped_but_controller_quit_survives(self):
        with patch.object(InputMapper, "_open_sticks"), \
                patch("pygame.key.start_text_input") as start, \
                patch("pygame.key.stop_text_input") as stop:
            mapper = InputMapper(DEFAULTS)
            events = [
                pygame.event.Event(pygame.KEYDOWN, key=pygame.K_q),
                pygame.event.Event(pygame.KEYDOWN, key=pygame.K_SPACE),
                pygame.event.Event(pygame.KEYDOWN, key=pygame.K_BACKSPACE),
                pygame.event.Event(pygame.TEXTINPUT, text="q "),
            ]
            received = []
            self.assertEqual(mapper.process(events, 0, text_mode=True,
                                            on_text=received.append), [])
            self.assertEqual(received, events)
            start.assert_called_once()
            actions = mapper.process([
                pygame.event.Event(pygame.JOYBUTTONDOWN, button=6),
                pygame.event.Event(pygame.JOYBUTTONDOWN, button=7),
                pygame.event.Event(pygame.QUIT),
            ], 0, text_mode=True)
            self.assertEqual(actions.count("quit"), 2)
            self.assertEqual(mapper.process(events[:2], 0), ["quit", "a"])
            stop.assert_called_once()


class MainLoopTests(unittest.TestCase):
    def test_same_frame_editor_transitions_do_not_trigger_quit_or_launch_early(self):
        from deckfw.main import main
        calls = []
        spec = AppSpec("Example", lambda ctx: calls.append(ctx.params),
                       setup=lambda: Form([Text("message", "Message", default="hello ")]))
        menu = Menu("Main", [Launch(spec)])
        key = lambda code: pygame.event.Event(pygame.KEYDOWN, key=code)
        events = [
            key(pygame.K_RETURN),  # menu -> setup
            key(pygame.K_RETURN),  # setup -> editor
            key(pygame.K_q),
            key(pygame.K_SPACE),
            pygame.event.Event(pygame.TEXTINPUT, text="q "),
            key(pygame.K_RETURN),  # editor -> setup
            key(pygame.K_DOWN),
            key(pygame.K_RETURN),  # setup -> app
        ]
        with patch.dict(os.environ, {"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"}), \
                patch("deckfw.main.discover", return_value=[spec]), \
                patch("deckfw.main.build_root_menu", return_value=menu), \
                patch("pygame.event.get", side_effect=[events, [pygame.event.Event(pygame.QUIT)]]):
            main(argv=["--windowed"])
        self.assertEqual(calls, [{"message": "hello q "}])


if __name__ == "__main__":
    unittest.main()
