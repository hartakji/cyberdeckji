"""Colors, fonts and text helpers shared by every widget.

Sizes are derived from the screen height, so the UI adapts to different
handheld resolutions."""

import logging
import os

import pygame

_COLOR_NAMES = ("text", "dim", "accent", "ok", "warn", "crit")


class Theme:
    bg = (16, 21, 28)
    panel = (24, 32, 42)
    track = (44, 56, 70)
    line = (52, 66, 82)
    text = (215, 226, 234)
    dim = (122, 140, 156)
    accent = (79, 209, 197)
    ok = (79, 209, 197)
    warn = (242, 184, 75)
    crit = (229, 86, 75)
    series = [(79, 209, 197), (242, 184, 75), (138, 160, 255), (229, 86, 75), (190, 150, 255)]

    def __init__(self, w, h, root=None):
        self.w, self.h = w, h
        self.pad = max(6, h // 60)
        self.f_small = self._font(h / 32)
        self.f_normal = self._font(h / 24)
        self.f_large = self._font(h / 13, bold=True)
        self.f_mono = self._font(h / 27)
        self._fonts = {
            "small": self.f_small,
            "normal": self.f_normal,
            "large": self.f_large,
            "mono": self.f_mono,
        }
        self._cache = {}
        self.root = root if root is not None else os.path.dirname(os.path.dirname(__file__))
        self._icon_images = {}
        self._icon_sizes = {}

    def icon(self, path, size):
        """Load once, preserve aspect ratio, and cache each requested size."""
        path = os.path.abspath(os.path.join(self.root, os.fspath(path)))
        key = (path, size)
        if key not in self._icon_sizes:
            if path not in self._icon_images:
                try:
                    self._icon_images[path] = pygame.image.load(path)
                except (OSError, pygame.error) as exc:
                    logging.getLogger(__name__).warning("Could not load menu icon %s: %s", path, exc)
                    self._icon_images[path] = None
            image = self._icon_images[path]
            if image is None:
                self._icon_sizes[key] = None
            else:
                w, h = image.get_size()
                scale = size / max(w, h)
                self._icon_sizes[key] = pygame.transform.smoothscale(
                    image, (max(1, round(w * scale)), max(1, round(h * scale))))
        return self._icon_sizes[key]

    @staticmethod
    def _font(size, bold=False):
        return pygame.font.SysFont("dejavusansmono,liberationmono,monospace",
                                   max(8, int(size)), bold=bold)

    def font(self, name):
        return self._fonts.get(name, self.f_normal)

    def color(self, value, default=None):
        """Accepts None, a theme color name ("warn"), or an (r, g, b) tuple."""
        if value is None:
            return default if default is not None else self.text
        if isinstance(value, str):
            return getattr(self, value) if value in _COLOR_NAMES else self.text
        return tuple(value)

    def render(self, font, text, color):
        """Render text to a surface, cached (static text is free after frame 1)."""
        key = (id(font), text, color)
        img = self._cache.get(key)
        if img is None:
            if len(self._cache) > 500:
                self._cache.clear()
            if text:
                img = font.render(text, True, color)
            else:                       # some pygame builds refuse empty strings
                img = pygame.Surface((1, font.get_linesize()), pygame.SRCALPHA)
            self._cache[key] = img
        return img

    @staticmethod
    def fit(font, text, width):
        """Shorten text with '...' so it fits in `width` pixels."""
        if width <= 0:
            return ""
        if font.size(text)[0] <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.size(text[:mid] + "...")[0] <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "..."
