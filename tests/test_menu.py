from pathlib import Path
import unittest

from deckfw.context import AppSpec
from deckfw.menu import (Launch, Menu, MenuScreen, Submenu, build_root_menu,
                         custom_menu, default_menu)
from deckfw.screens import Navigator


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.monitor = AppSpec("System monitor", lambda ctx: None, group="System")
        self.tool = AppSpec("My tool", lambda ctx: None, group="Network")
        self.custom = AppSpec("Network quick check", lambda ctx: None,
                              group="Network/WiFi", source="custom")
        self.specs = [self.monitor, self.tool, self.custom]

    def assert_system_layout(self, menu):
        self.assertEqual([item.label for item in menu.items[-2:]], ["System", "Exit"])
        self.assertNotIn("Controls", [item.label for item in menu.items])
        self.assertNotIn("System monitor", [item.label for item in menu.items])
        system = menu.items[-2].menu
        monitor = next(item for item in system.items if isinstance(item, Launch))
        self.assertIs(monitor.spec, self.monitor)
        controls = next(item for item in system.items if item.label == "Controls")
        self.assertIsInstance(controls, Submenu)
        self.assertIn("Volume up", [item.label for item in controls.menu.items])
        apps = next(item for item in menu.items if item.label == "Apps").menu
        self.assertEqual(len(apps.items), 1)
        self.assertIs(apps.items[0].spec, self.tool)

    def test_default_menu(self):
        self.assert_system_layout(default_menu(self.specs))

    def test_custom_menu(self):
        root = str(Path(__file__).resolve().parents[1])
        menu = build_root_menu(root, self.specs)
        self.assertEqual([item.label for item in menu.items],
                         ["Apps", "Custom", "System", "Exit"])
        self.assert_system_layout(menu)
        tools = menu.items[1].menu
        network = tools.items[0]
        self.assertIsInstance(network, Submenu)
        self.assertEqual(network.label, "Network")
        self.assertIs(network.menu.items[0].spec, self.custom)
        self.assertEqual(len(tools.items), 1)
        nav = Navigator()
        nav.push(MenuScreen(menu))
        menu.items[1].activate(nav.top)
        network.activate(nav.top)
        self.assertIs(nav.top.menu, network.menu)
        nav.top.action("b")
        self.assertIs(nav.top.menu, tools)
        nav.top.action("b")
        self.assertIs(nav.top.menu, menu)

    def test_custom_system_group_does_not_move_to_system(self):
        custom = AppSpec("Custom system tool", lambda ctx: None,
                         group="System", source="custom")
        menu = default_menu([self.monitor, custom])
        tools = menu.items[1].menu
        self.assertIs(tools.items[0].spec, custom)
        system_launches = [item.spec for item in menu.items[-2].menu.items
                           if isinstance(item, Launch)]
        self.assertEqual(system_launches, [self.monitor])

    def test_empty_tools_has_no_hardcoded_commands(self):
        menu = default_menu([self.tool])
        self.assertEqual(menu.items[1].label, "Custom")
        self.assertEqual(menu.items[1].menu.items, [])

    def test_unplaced_scripts_ignore_group_and_appear_at_custom_root(self):
        menu = default_menu(self.specs)
        self.assertIs(menu.items[1].menu.items[0].spec, self.custom)

    def test_deep_placement_appends_only_unplaced_custom_scripts(self):
        extra = AppSpec("Extra", lambda ctx: None, group="Ignored", source="custom")
        menu = custom_menu(self.specs + [extra], [
            Submenu("Network", Menu("Network", [
                Submenu("WiFi", Menu("WiFi", [Launch(self.custom)])),
            ])),
        ])
        self.assertEqual(len(menu.items), 2)
        self.assertIs(menu.items[1].spec, extra)
        self.assertIs(menu.items[0].menu.items[0].menu.items[0].spec, self.custom)

    def test_custom_menu_without_explicitly_named_script(self):
        root = str(Path(__file__).resolve().parents[1])
        extra = AppSpec("Extra", lambda ctx: None, source="custom")
        menu = build_root_menu(root, [self.monitor, self.tool, extra])
        self.assertEqual([item.label for item in menu.items],
                         ["Apps", "Custom", "System", "Exit"])
        self.assertEqual([item.spec for item in menu.items[1].menu.items], [extra])

    def test_callable_custom_submenus_report_invalid_declaration(self):
        with self.assertRaisesRegex(ValueError, "must use a Menu"):
            custom_menu(self.specs, [Submenu("Dynamic", lambda: Menu("Dynamic", []))])

    def test_system_menu_without_system_apps(self):
        menu = default_menu([self.tool])
        self.assertEqual(menu.items[-2].menu.items[0].label, "Controls")


if __name__ == "__main__":
    unittest.main()
