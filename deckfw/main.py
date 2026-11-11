"""Main loop: input -> actions -> current screen, with the status bar on top."""

import os
import sys
import traceback

import pygame

from . import config
from .inputmap import InputMapper
from .loader import discover
from .menu import MenuScreen, build_root_menu
from .screens import Navigator
from .statusbar import StatusBar
from .theme import Theme


def _guarded(nav, fn, *args):
    """Run a screen call. If an app screen blows up, close it and go back to the
    launcher instead of freezing a device that has no terminal to read errors on."""
    try:
        fn(*args)
    except Exception:
        traceback.print_exc()
        if len(nav.stack) > 1:
            nav.pop()
        else:
            raise


def main(root=None, argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = root or os.getcwd()
    cfg = config.load(root)
    windowed = "--windowed" in argv or not cfg["fullscreen"]

    pygame.init()
    pygame.mouse.set_visible(False)
    if windowed:
        screen = pygame.display.set_mode(tuple(cfg["size"]))
    else:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("cyberdeck")
    w, h = screen.get_size()

    theme = Theme(w, h, root=root)
    bar = StatusBar(theme, cfg)
    inp = InputMapper(cfg)
    nav = Navigator()
    nav.push(MenuScreen(build_root_menu(root, discover(root, cfg))))

    footer_h = theme.f_small.get_linesize() + theme.pad * 2
    clock = pygame.time.Clock()
    dt = 0.0
    running = True

    while running:
        # Dispatch one event at a time: an editor can open/close during a frame.
        for event in pygame.event.get():
            top = nav.top
            for act in inp.process([event], 0, text_mode=top.text_input,
                                   on_text=getattr(top, "text_event", None)):
                if act == "quit":
                    running = False
                else:
                    _guarded(nav, nav.top.action, act)
        for act in inp.process([], dt, text_mode=nav.top.text_input,
                               on_text=getattr(nav.top, "text_event", None)):
            if act == "quit":
                running = False
            else:
                _guarded(nav, nav.top.action, act)
        if nav.quit_requested:
            running = False

        _guarded(nav, nav.top.update, dt)
        bar.update()

        screen.fill(theme.bg)
        bar.draw(screen, pygame.Rect(0, 0, w, bar.height), theme)

        body = pygame.Rect(0, bar.height + 1, w, h - bar.height - 1 - footer_h)
        screen.set_clip(body)
        _guarded(nav, nav.top.draw, screen, body, theme)
        screen.set_clip(None)

        fy = h - footer_h
        pygame.draw.line(screen, theme.line, (0, fy), (w, fy))
        hint = theme.fit(theme.f_small, nav.top.hints, w - theme.pad * 4)
        screen.blit(theme.render(theme.f_small, hint, theme.dim),
                    (theme.pad * 2, fy + theme.pad))

        pygame.display.flip()
        dt = clock.tick(cfg["fps"]) / 1000.0

    nav.close_all()                    # cancels apps, kills their commands
    pygame.quit()
