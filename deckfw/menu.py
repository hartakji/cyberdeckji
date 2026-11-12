"""Menu tree.

    Menu("Main", [
        Submenu("Apps", apps_menu),
        Command("Volume up", [["amixer", "-q", "set", "Master", "5%+"]]),
        Run("Storage", ["df", "-h"]),
        Action("Say hi", lambda screen: screen.toast("hi", "ok")),
    ])

Item types
  Submenu(label, menu)        open another menu (a Menu, or a function returning one)
  Launch(spec)                open an app from apps/ or custom/
  Run(label, cmd)             run a command and show its output on a screen
  Command(label, commands)    run a command silently, show the result as a toast;
                              `commands` is one command or a list of alternatives
                              tried in order until one succeeds
  Action(label, fn)           call fn(menu_screen) (toast, quit, anything)
Command and Action accept confirm="Are you sure?" to ask Yes/No first
(the default answer is No).

To customise, create menu.py next to deck.py with:  def build(specs): ... return Menu(...)
See menu.example.py. If it fails, the default menu appears with an error entry."""

import importlib.util
import os
import shutil
import sys
import time
import traceback

import pygame

from .context import AppSpec
from .runner import Runner
from .screens import AppScreen, Screen
from .widgets import Label, ListView


# ----------------------------------------------------------------------------
# Items
# ----------------------------------------------------------------------------
class Menu:
    def __init__(self, title, items):
        self.title = title
        self.items = list(items)


class Item:
    hint = ""

    def __init__(self, label, icon=None):
        self.label = label
        self.icon = icon

    def activate(self, screen):
        pass


class Submenu(Item):
    hint = ">"

    def __init__(self, label, menu, icon="icons/folder.png"):
        super().__init__(label, icon)
        self.menu = menu

    def activate(self, screen):
        menu = self.menu() if callable(self.menu) else self.menu
        screen.nav.push(MenuScreen(menu))


class Launch(Item):
    def __init__(self, spec, icon=None):
        super().__init__(spec.name, icon if icon is not None else
                         (spec.icon or "icons/app.png"))
        self.spec = spec

    def activate(self, screen):
        if self.spec.setup is None:
            screen.nav.push(AppScreen(self.spec))
            return
        from .forms import SetupScreen
        try:
            setup = SetupScreen(self.spec)
        except Exception:
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            def show_error(ctx):
                ctx.log(tb, color="crit")
                ctx.status = "error"
            error = AppSpec(self.label + " setup error", show_error)
            screen.nav.push(AppScreen(error))
        else:
            screen.nav.push(setup)


class Run(Item):
    """Run a command and show its output on a scrollable screen."""

    def __init__(self, label, cmd, icon="icons/script.png"):
        super().__init__(label, icon)
        self.cmd = list(cmd)

    def activate(self, screen):
        cmd = self.cmd
        screen.nav.push(AppScreen(AppSpec(self.label, lambda ctx: ctx.run(cmd))))


class Action(Item):
    def __init__(self, label, fn, confirm=None, icon="icons/action.png"):
        super().__init__(label, icon)
        self.fn, self.confirm = fn, confirm

    def activate(self, screen):
        if self.confirm:
            screen.confirm(self.confirm, lambda: self.fn(screen))
        else:
            self.fn(screen)


class Command(Item):
    def __init__(self, label, commands, ok=None, confirm=None, icon="icons/script.png"):
        super().__init__(label, icon)
        if commands and isinstance(commands[0], str):      # a single command
            commands = [commands]
        self.commands = [list(c) for c in commands]
        self.ok, self.confirm = ok, confirm

    def activate(self, screen):
        go = lambda: screen.run_command(self.commands, self.ok)
        if self.confirm:
            screen.confirm(self.confirm, go)
        else:
            go()


# ----------------------------------------------------------------------------
# Screens
# ----------------------------------------------------------------------------
class MenuScreen(Screen):
    def __init__(self, menu):
        self.menu = menu
        self.title = menu.title
        self.list = ListView([(i.label, i, i.hint, i.icon) for i in menu.items],
                             on_select=lambda item: item.activate(self))
        self._runner = None
        self._queue = []             # alternatives still to try
        self._ok = None
        self._last = ""
        self._toast = ("", None)
        self._toast_until = 0.0

    # -- screen interface --------------------------------------------------------
    @property
    def hints(self):
        root = self.nav is not None and len(self.nav.stack) == 1
        return "Up/Down move   A select   " + ("Start+Select quit" if root else "B back")

    def action(self, name):
        if name == "b":
            self.nav.pop()                   # the root menu can't be popped
        else:
            self.list.action(name)

    def close(self):
        if self._runner is not None:
            self._runner.stop()

    # -- helpers menu items can use -------------------------------------------------
    def toast(self, message, color="dim", seconds=3.0):
        self._toast = (message, color)
        self._toast_until = time.monotonic() + seconds if seconds else float("inf")

    def confirm(self, prompt, on_yes):
        self.nav.push(ConfirmScreen(prompt, on_yes))

    def run_command(self, commands, ok=None):
        """Run the first alternative that exists and succeeds, without blocking."""
        if self._runner is not None:
            return                                           # one at a time
        self._queue = [c for c in commands if shutil.which(c[0])]
        if not self._queue:
            need = ", ".join(sorted({c[0] for c in commands}))
            self.toast(f"Not available (needs: {need})", "crit")
            return
        self._ok = ok
        self._start_next()

    def _start_next(self):
        self._last = ""
        self._runner = Runner(self._queue.pop(0))
        self.toast("working...", "dim", seconds=None)

    def update(self, dt):
        r = self._runner
        if r is None:
            return
        r.poll()
        for line in r.new_lines():
            if line.strip():
                self._last = line.strip()
        if r.running:
            return
        if r.returncode == 0:
            self._runner = None
            self.toast(self._ok or self._last or "Done", "ok")
        elif self._queue:                                    # try the next alternative
            self._start_next()
        else:
            self._runner = None
            detail = f": {self._last}" if self._last else ""
            self.toast(f"Failed ({r.returncode}){detail}", "crit", seconds=5.0)

    # -- drawing ------------------------------------------------------------------------
    def draw(self, surf, rect, theme):
        pad, small = theme.pad, theme.f_small
        head_h = small.get_linesize() + pad * 2

        if self.nav is not None and self in self.nav.stack:
            upto = self.nav.stack[:self.nav.stack.index(self) + 1]
            crumbs = [s.title for s in upto if isinstance(s, MenuScreen)]
        else:
            crumbs = [self.title]
        crumb = theme.fit(small, "  >  ".join(crumbs), rect.w - pad * 4)
        surf.blit(theme.render(small, crumb, theme.text), (rect.x + pad * 2, rect.y + pad))
        pygame.draw.line(surf, theme.line, (rect.x, rect.y + head_h - 1),
                         (rect.right, rect.y + head_h - 1))

        toast_h = small.get_linesize() + pad * 2             # always reserved: no layout jump
        body = pygame.Rect(rect.x + pad * 2, rect.y + head_h + pad,
                           rect.w - pad * 4, rect.h - head_h - pad * 2 - toast_h)
        if self.list.items:
            self.list.draw(surf, body, theme)
        else:
            Label("(empty)", color="dim").draw(surf, body, theme)

        message, color = self._toast
        if message and time.monotonic() < self._toast_until:
            ty = rect.bottom - toast_h
            pygame.draw.line(surf, theme.line, (rect.x, ty), (rect.right, ty))
            text = theme.fit(small, message, rect.w - pad * 4)
            surf.blit(theme.render(small, text, theme.color(color, theme.text)),
                      (rect.x + pad * 2, ty + pad))


class ConfirmScreen(Screen):
    """Yes/No prompt. The cursor starts on No so a stray A press is harmless."""
    hints = "Up/Down move   A choose   B cancel"

    def __init__(self, prompt, on_yes):
        self.prompt, self.on_yes = prompt, on_yes
        self.list = ListView([("No, cancel", False), ("Yes, do it", True)],
                             on_select=self._choose)

    def _choose(self, yes):
        self.nav.pop()                       # back to the menu first...
        if yes:
            self.on_yes()                    # ...so the result toast shows there

    def action(self, name):
        if name == "b":
            self.nav.pop()
        else:
            self.list.action(name)

    def draw(self, surf, rect, theme):
        pad = theme.pad
        row_h = int(theme.f_normal.get_linesize() * 1.5)
        top = rect.y + rect.h // 5
        Label(self.prompt, size="normal", align="center").draw(
            surf, pygame.Rect(rect.x + pad * 2, top, rect.w - pad * 4,
                              theme.f_normal.get_linesize()), theme)
        self.list.draw(surf, pygame.Rect(rect.x + rect.w // 6, top + row_h + pad * 2,
                                         rect.w * 2 // 3, row_h * 2), theme)


# ----------------------------------------------------------------------------
# The default tree
# ----------------------------------------------------------------------------
def _volume(step_wpctl, step_pactl, step_amixer):
    return [["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", step_wpctl],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", step_pactl],
            ["amixer", "-q", "set", "Master", step_amixer]]


GROUP_ICONS = {"Network": "icons/globe.png", "Removable Storage": "icons/storage.png"}


def default_menu(specs):
    native_specs = [s for s in specs if s.source == "native"]
    custom_specs = [s for s in specs if s.source == "custom"]
    system_specs = [s for s in native_specs if s.group == "System"]
    app_specs = [s for s in native_specs if s.group != "System"]
    groups = {}
    for spec in app_specs:
        groups.setdefault(spec.group or "Other", []).append(spec)
    if len(groups) <= 1:
        apps_items = [Launch(s) for s in app_specs]
    else:
        apps_items = [Submenu(name, Menu(name, [Launch(s) for s in members]),
                              icon=GROUP_ICONS.get(name, "icons/folder.png"))
                      for name, members in sorted(groups.items(), key=lambda kv: kv[0].lower())]

    controls = Menu("Controls", [
        Command("Volume up", _volume("5%+", "+5%", "5%+"), ok="Volume up"),
        Command("Volume down", _volume("5%-", "-5%", "5%-"), ok="Volume down"),
        Command("Mute / unmute", [["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"],
                                  ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
                                  ["amixer", "-q", "set", "Master", "toggle"]], ok="Mute toggled"),
        Command("Brightness up", [["brightnessctl", "set", "10%+"]], ok="Brightness up"),
        Command("Brightness down", [["brightnessctl", "--min-value=1", "set", "10%-"],
                                    ["brightnessctl", "set", "10%-"]], ok="Brightness down"),
        Command("Wi-Fi on", [["nmcli", "radio", "wifi", "on"],
                             ["rfkill", "unblock", "wifi"]], ok="Wi-Fi on"),
        Command("Wi-Fi off", [["nmcli", "radio", "wifi", "off"],
                              ["rfkill", "block", "wifi"]], ok="Wi-Fi off"),
    ])

    system = Menu("System", [Launch(s) for s in system_specs] + [
        Submenu("Controls", controls, icon="icons/controls.png"),
        Run("Storage", ["df", "-h"]),
        Run("Memory", ["free", "-h"]),
        Run("Network addresses", ["ip", "-br", "addr"]),
        Run("Uptime", ["uptime"]),
    ])

    exit_menu = Menu("Exit", [
        Action("Quit to shell", lambda screen: screen.nav.request_quit()),
        Command("Reboot", [["systemctl", "reboot"], ["reboot"]],
                ok="Rebooting...", confirm="Reboot the device?"),
        Command("Power off", [["systemctl", "poweroff"], ["poweroff"]],
                ok="Powering off...", confirm="Power off the device?"),
    ])

    return Menu("Main", [
        Submenu("Apps", Menu("Apps", apps_items), icon="icons/apps.png"),
        Submenu("Custom", custom_menu(custom_specs), icon="icons/custom.png"),
        Submenu("System", system, icon="icons/system.png"),
        Submenu("Exit", exit_menu, icon="icons/exit.png"),     # always last
    ])


def custom_menu(specs, items=()):
    """Keep the declared tree, then append unplaced custom scripts at its root.

    Use concrete Menu instances for submenus so placements can be inspected.
    """
    root = Menu("Custom", items)
    placed = set()

    def collect(menu):
        for item in menu.items:
            if isinstance(item, Launch):
                placed.add(item.spec)
            elif isinstance(item, Submenu):
                if callable(item.menu):
                    raise ValueError("Custom submenus must use a Menu, not a function")
                collect(item.menu)

    collect(root)
    root.items.extend(Launch(spec) for spec in specs
                      if spec.source == "custom" and spec not in placed)
    return root


def build_root_menu(root, specs):
    """menu.py next to deck.py can replace the tree: def build(specs) -> Menu."""
    path = os.path.join(root, "menu.py")
    if not os.path.isfile(path):
        return default_menu(specs)
    try:
        spec = importlib.util.spec_from_file_location("deck_user_menu", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.build(specs)
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        menu = default_menu(specs)
        broken = AppSpec("menu.py error", lambda ctx: ctx.log(tb, color="crit"))
        menu.items.insert(0, Launch(broken))
        menu.items[0].label = "! menu.py failed (see error)"
        return menu
