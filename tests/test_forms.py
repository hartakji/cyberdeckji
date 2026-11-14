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

    def test_arrow_keys_and_enter_operate_on_screen_keyboard_with_repeat(self):
        self.setup.action("a")
        editor = self.nav.top
        original = editor.value
        with patch.object(InputMapper, "_open_sticks"), \
                patch("pygame.key.start_text_input"), patch("pygame.key.stop_text_input"):
            mapper = InputMapper(DEFAULTS)

            def dispatch(events, dt=0):
                top = self.nav.top
                actions = mapper.process(events, dt, text_mode=top.text_input,
                                         on_text=getattr(top, "text_event", None))
                for action in actions:
                    self.nav.top.action(action)
                return actions

            def press(key):
                dispatch([pygame.event.Event(pygame.KEYDOWN, key=key)])
                dispatch([pygame.event.Event(pygame.KEYUP, key=key)])

            self.assertEqual(dispatch([
                pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT)]), ["right"])
            self.assertEqual(editor.selected, 1)
            self.assertEqual(dispatch([], DEFAULTS["repeat_delay"] + 0.01), ["right"])
            self.assertEqual(editor.selected, 2)
            dispatch([pygame.event.Event(pygame.KEYUP, key=pygame.K_RIGHT)])
            self.assertEqual(dispatch([], 1), [])
            press(pygame.K_DOWN)
            self.assertEqual(editor.selected, 10)
            selected = editor.keys[editor.selected]
            press(pygame.K_RETURN)
            self.assertIs(self.nav.top, editor)
            self.assertEqual(editor.value, original + selected)
            while editor.keys[editor.selected] != "OK":
                press(pygame.K_RIGHT)
            press(pygame.K_RETURN)
            self.assertIs(self.nav.top, self.setup)
            self.assertEqual(self.setup.values["host"], original + selected)

    def test_typing_and_tab_switch_between_text_and_grid_without_losing_edits(self):
        self.setup.action("a")
        editor = self.nav.top
        with patch.object(InputMapper, "_open_sticks"), \
                patch("pygame.key.start_text_input"), patch("pygame.key.stop_text_input"):
            mapper = InputMapper(DEFAULTS)

            def dispatch(event):
                for action in mapper.process([event], 0, text_mode=True,
                                             on_text=editor.text_event):
                    editor.action(action)

            dispatch(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT))
            dispatch(pygame.event.Event(pygame.TEXTINPUT, text="q "))
            self.assertFalse(editor.keyboard_navigation)
            self.assertEqual(mapper.process([], 1, text_mode=True,
                                            on_text=editor.text_event), [])
            dispatch(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_LEFT))
            self.assertEqual(editor.cursor, len(editor.value) - 1)
            dispatch(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB))
            self.assertTrue(editor.keyboard_navigation)
            dispatch(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT))
            dispatch(pygame.event.Event(pygame.KEYUP, key=pygame.K_RIGHT))
            selected = editor.keys[editor.selected]
            dispatch(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_KP_ENTER))
            self.assertEqual(editor.value, "localhostq" + selected + " ")
            dispatch(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DELETE))
            dispatch(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN,
                                       mod=pygame.KMOD_CTRL))
            self.assertIs(self.nav.top, self.setup)

    def test_controller_returns_to_grid_after_physical_typing_and_can_cancel(self):
        self.setup.action("a")
        editor = self.nav.top
        original = self.setup.values["host"]
        editor.text_event(pygame.event.Event(pygame.TEXTINPUT, text="typed"))
        with patch.object(InputMapper, "_open_sticks"), \
                patch("pygame.key.start_text_input"), patch("pygame.key.stop_text_input"):
            mapper = InputMapper(DEFAULTS)
            for event in [
                pygame.event.Event(pygame.JOYHATMOTION, value=(1, 0)),
                pygame.event.Event(pygame.JOYHATMOTION, value=(0, 0)),
                pygame.event.Event(pygame.JOYBUTTONDOWN, button=DEFAULTS["buttons"]["a"]),
            ]:
                for action in mapper.process([event], 0, text_mode=True,
                                             on_text=editor.text_event):
                    editor.action(action)
            self.assertTrue(editor.keyboard_navigation)
            self.assertEqual(editor.value, original + "typedb")
            for action in mapper.process([
                    pygame.event.Event(pygame.JOYBUTTONDOWN,
                                       button=DEFAULTS["buttons"]["b"])],
                    0, text_mode=True, on_text=editor.text_event):
                editor.action(action)
        self.assertIs(self.nav.top, self.setup)
        self.assertEqual(self.setup.values["host"], original)

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

    def test_compact_host_keyboard_fits_without_scrolling(self):
        field = Text("host", "IP / Host", default="1.1.1.1", format="host")
        editor = TextEditor(field, field.default, lambda value: None)
        for size in ((320, 240), (640, 480)):
            with self.subTest(size=size):
                surface = pygame.Surface(size)
                theme = Theme(*size)
                body = pygame.Rect(0, 30, size[0], size[1] - 60)
                rendered = []

                def capture_label(label, *args, **kwargs):
                    rendered.append(label.text)

                with patch.object(Label, "draw", autospec=True,
                                  side_effect=capture_label):
                    editor.draw(surface, body, theme)
                self.assertTrue(all(key in rendered for key in editor.keys))

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
    def test_same_frame_keyboard_grid_selection_accepts_only_with_ok(self):
        from deckfw.main import main
        calls = []
        field = Text("message", "Message", default="hello ")
        spec = AppSpec("Example", lambda ctx: calls.append(ctx.params),
                       setup=lambda: Form([field]))
        menu = Menu("Main", [Launch(spec)])

        def press(code):
            return [pygame.event.Event(pygame.KEYDOWN, key=code),
                    pygame.event.Event(pygame.KEYUP, key=code)]

        events = press(pygame.K_RETURN) * 2 + press(pygame.K_RIGHT) + press(pygame.K_RETURN)
        keys = TextEditor(field, field.default, lambda value: None).keys
        for _ in range(keys.index("OK") - 1):
            events += press(pygame.K_RIGHT)
        events += press(pygame.K_RETURN) + press(pygame.K_DOWN) + press(pygame.K_RETURN)
        with patch.dict(os.environ, {"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"}), \
                patch("deckfw.main.discover", return_value=[spec]), \
                patch("deckfw.main.build_root_menu", return_value=menu), \
                patch("pygame.event.get", side_effect=[events, [pygame.event.Event(pygame.QUIT)]]):
            main(argv=["--windowed"])
        self.assertEqual(calls, [{"message": "hello b"}])

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
