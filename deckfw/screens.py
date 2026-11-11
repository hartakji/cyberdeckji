"""Screens and navigation: a launcher that lists apps, and an AppScreen that
runs one app in a worker thread and draws whatever widgets it created."""

import threading
import traceback
import sys

import pygame

from .context import Context, Cancelled
from .widgets import LogView, find_focusable


class Screen:
    nav = None
    hints = ""
    text_input = False

    def update(self, dt):
        pass

    def draw(self, surf, rect, theme):
        pass

    def action(self, name):
        pass

    def close(self):
        pass


class Navigator:
    def __init__(self):
        self.stack = []
        self.quit_requested = False

    def request_quit(self):
        """Ask the main loop to exit (used by the Exit menu)."""
        self.quit_requested = True

    @property
    def top(self):
        return self.stack[-1] if self.stack else None

    def push(self, screen):
        screen.nav = self
        self.stack.append(screen)

    def pop(self):
        if len(self.stack) > 1:           # the launcher stays
            self.stack.pop().close()

    def close_all(self):
        for screen in reversed(self.stack):
            screen.close()
        for screen in reversed(self.stack):
            if isinstance(screen, AppScreen):
                screen._thread.join(timeout=15)
                if screen.alive:
                    print(f"Shutdown still running for {screen.spec.name}; cleanup may be incomplete",
                          file=sys.stderr)
        self.stack = []


class AppScreen(Screen):
    def __init__(self, spec, params=None):
        self.spec = spec
        self.ctx = Context(spec, params=params)
        self._thread = threading.Thread(target=self._work, daemon=True, name="app-" + spec.name)
        self._thread.start()

    # -- worker thread ---------------------------------------------------------
    def _work(self):
        ctx = self.ctx
        try:
            self.spec.run(ctx)
            if ctx.cancelled:
                ctx.status = "cancelled"
            elif ctx.status == "running":
                ctx.status = "done"
        except Cancelled:
            ctx.status = "cancelled"
        except Exception:
            ctx.error = traceback.format_exc()
            log = LogView()
            log.write("The app crashed:", color="crit")
            log.write(ctx.error)
            ctx.root.children[:] = [log]           # replace the screen with the traceback
            ctx.status = "error"
        finally:
            if self.spec.shutdown is not None:
                ctx._shutting_down = True
                ctx.note = "cleaning up..."
                try:
                    self.spec.shutdown(ctx)
                except BaseException:
                    error = traceback.format_exc()
                    ctx.error = (ctx.error + "\n" if ctx.error else "") + error
                    ctx.log("Shutdown failed:", color="crit")
                    ctx.log(error, color="crit")
                    print(error, file=sys.stderr)
                    ctx.status = "error"
                finally:
                    ctx._shutting_down = False
                    ctx.note = "cleanup failed" if ctx.status == "error" else ""

    @property
    def alive(self):
        return self._thread.is_alive()

    # -- screen interface ------------------------------------------------------
    @property
    def hints(self):
        focus = find_focusable(self.ctx.root)
        if getattr(focus, "handles_back", False) and not self.ctx._shutting_down:
            return focus.hints
        scroll = "Up/Down scroll   Left/Right page   " if isinstance(focus, LogView) else ""
        if self.alive:
            if self.ctx._shutting_down:
                return scroll + "Cleaning up - please wait"
            return scroll + ("B force close" if self.ctx.cancelled else "B cancel")
        return scroll + "B back"

    def action(self, name):
        if name == "b":
            if self.ctx._shutting_down:
                return
            focus = find_focusable(self.ctx.root)
            if getattr(focus, "handles_back", False) and focus.action("b"):
                return
            if self.alive and not self.ctx.cancelled:
                self.ctx.cancel()                  # first press: ask the app to stop
            else:
                self.nav.pop()                     # finished, or second press: leave
            return
        focus = find_focusable(self.ctx.root)
        if focus is not None:
            focus.action(name)

    def close(self):
        self.ctx.cancel()                          # kills any commands it started

    def _state(self, theme):
        c = self.ctx
        if self.alive:
            if c._shutting_down:
                return ("cleaning up...", theme.warn)
            return ("stopping...", theme.warn) if c.cancelled else ("running", theme.accent)
        return {"done": ("done", theme.ok),
                "cancelled": ("cancelled", theme.warn),
                "error": ("error", theme.crit)}.get(c.status, ("done", theme.ok))

    def draw(self, surf, rect, theme):
        pad, small = theme.pad, theme.f_small
        head_h = small.get_linesize() + pad * 2
        state, color = self._state(theme)

        right = theme.render(small, state, color)
        surf.blit(right, (rect.right - right.get_width() - pad * 2, rect.y + pad))
        title = self.ctx.title + (("  " + self.ctx.note) if self.ctx.note else "")
        title = theme.fit(small, title, rect.w - right.get_width() - pad * 5)
        surf.blit(theme.render(small, title, theme.text), (rect.x + pad * 2, rect.y + pad))
        pygame.draw.line(surf, theme.line, (rect.x, rect.y + head_h - 1),
                         (rect.right, rect.y + head_h - 1))

        body = pygame.Rect(rect.x + pad, rect.y + head_h + pad,
                           rect.w - pad * 2, rect.h - head_h - pad * 2)
        self.ctx.root.draw(surf, body, theme)
