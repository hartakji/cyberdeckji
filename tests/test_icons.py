from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pygame

from deckfw import app
from deckfw.context import AppSpec
from deckfw.loader import _metadata
from deckfw.menu import (Action, Command, Launch, Menu, MenuScreen, Run,
                         Submenu, build_root_menu)
from deckfw.theme import Theme
from deckfw.widgets import ListView


class IconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def test_icon_overrides_and_legacy_constructors(self):
        spec = AppSpec("Example", lambda ctx: None, icon="icons/python.png")
        self.assertEqual(Launch(spec).icon, "icons/python.png")
        self.assertEqual(Launch(spec, icon="icons/network.png").icon, "icons/network.png")
        menu = Menu("Nested", [])
        self.assertEqual(Submenu("Nested", menu).icon, "icons/folder.png")
        self.assertIsNone(Submenu("Nested", menu, icon=None).icon)
        self.assertEqual(Run("Run", ["true"]).icon, "icons/script.png")
        self.assertEqual(Command("Command", ["true"]).icon, "icons/script.png")
        self.assertEqual(Action("Action", lambda screen: None).icon, "icons/action.png")

    def test_app_decorator_and_script_header_icons(self):
        with patch("deckfw.context.register") as register:
            @app(name="Example", icon="icons/python.png")
            def example(ctx):
                pass
        self.assertEqual(register.call_args[0][0].icon, "icons/python.png")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "script.sh"
            path.write_text("# deck: Example\n# group: Network\n# icon: icons/network.png\n")
            self.assertEqual(_metadata(path), ("Example", "Network", "icons/network.png"))

    def test_images_and_scaled_sizes_are_cached(self):
        theme = Theme(640, 480)
        with patch("pygame.image.load", wraps=pygame.image.load) as load:
            first = theme.icon("icons/app.png", 20)
            self.assertEqual(first.get_size(), (20, 20))
            self.assertIs(first, theme.icon("icons/app.png", 20))
            self.assertEqual(theme.icon("icons/app.png", 32).get_size(), (32, 32))
            self.assertEqual(load.call_count, 1)

    def test_aspect_ratio_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            pygame.image.save(pygame.Surface((80, 40)), str(Path(directory) / "wide.png"))
            theme = Theme(640, 480, root=directory)
            self.assertEqual(theme.icon("wide.png", 20).get_size(), (20, 10))

    def test_missing_image_is_logged_once_and_does_not_break_menu(self):
        theme = Theme(640, 480)
        with self.assertLogs("deckfw.theme", level="WARNING") as logs:
            self.assertIsNone(theme.icon("icons/not-found.png", 20))
            self.assertIsNone(theme.icon("icons/not-found.png", 32))
        self.assertEqual(len(logs.output), 1)

    def test_real_tree_and_legacy_lists_render_at_handheld_sizes(self):
        root = str(Path(__file__).resolve().parents[1])
        specs = [
            AppSpec("Network quick check", lambda ctx: None, source="custom"),
            AppSpec("Custom demo", lambda ctx: None, source="custom", icon="icons/python.png"),
        ]
        menu = build_root_menu(root, specs)
        for width, height in [(320, 240), (640, 480)]:
            with self.subTest(size=(width, height)):
                surface = pygame.Surface((width, height))
                theme = Theme(width, height, root=root)

                def draw_tree(tree):
                    screen = MenuScreen(tree)
                    for row in screen.list.items:
                        if row[3]:
                            self.assertIsNotNone(theme.icon(row[3], 20))
                    screen.draw(surface, surface.get_rect(), theme)
                    for item in tree.items:
                        if isinstance(item, Submenu):
                            draw_tree(item.menu)

                draw_tree(menu)
                ListView([("Legacy", 1), ("Hint", 2, ">")]).draw(
                    surface, surface.get_rect(), theme)


if __name__ == "__main__":
    unittest.main()
