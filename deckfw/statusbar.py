"""The always-visible top bar: clock, battery, CPU, memory, disk."""

import os
import time

import pygame

try:
    import psutil
except ImportError:
    raise SystemExit("Missing dependency. Install it with:  pip install psutil")

from .theme import Theme


def read_battery():
    """(percent or None, charging). Falls back to sysfs, which handheld Linux
    builds usually expose even when psutil sees nothing."""
    try:
        b = psutil.sensors_battery()
        if b is not None:
            return int(b.percent), bool(b.power_plugged)
    except Exception:
        pass
    base = "/sys/class/power_supply"
    try:
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            with open(os.path.join(path, "type")) as f:
                if f.read().strip() != "Battery":
                    continue
            with open(os.path.join(path, "capacity")) as f:
                cap = int(f.read().strip())
            try:
                with open(os.path.join(path, "status")) as f:
                    charging = f.read().strip() in ("Charging", "Full")
            except OSError:
                charging = False
            return cap, charging
    except Exception:
        pass
    return None, False


class StatusBar:
    def __init__(self, theme, cfg):
        self.height = max(48, theme.h // 9)
        self.pad = max(6, self.height // 7)
        self.f_label = Theme._font(self.height * 0.24)
        self.f_value = Theme._font(self.height * 0.40, bold=True)
        self.disk_path = cfg["disk_path"]
        self.interval = cfg["stats_interval"]
        self.cpu = self.mem = self.disk = 0.0
        self.battery, self.charging = None, False
        self._last = 0.0
        psutil.cpu_percent(interval=None)          # prime the counter
        self._refresh()

    def _refresh(self):
        self.cpu = psutil.cpu_percent(interval=None)
        self.mem = psutil.virtual_memory().percent
        try:
            self.disk = psutil.disk_usage(self.disk_path).percent
        except Exception:
            self.disk = 0.0
        self.battery, self.charging = read_battery()
        self._last = time.monotonic()

    def update(self):
        if time.monotonic() - self._last >= self.interval:
            self._refresh()

    @staticmethod
    def _usage_color(theme, pct):
        return theme.crit if pct >= 85 else theme.warn if pct >= 60 else theme.ok

    @staticmethod
    def _battery_color(theme, pct, charging):
        if charging:
            return theme.ok
        return theme.crit if pct <= 15 else theme.warn if pct <= 35 else theme.ok

    def _segment(self, surf, theme, x, y, w, label, value, color, fraction):
        pad = self.pad
        lab = theme.render(self.f_label, label, theme.dim)
        val = theme.render(self.f_value, value, color if fraction is not None else theme.text)
        surf.blit(lab, (x + pad, y + pad))
        surf.blit(val, (x + pad, y + pad + lab.get_height()))
        if fraction is not None:
            track = pygame.Rect(x + pad, y + self.height - pad - 4, w - 2 * pad, 4)
            pygame.draw.rect(surf, theme.track, track)
            fill_w = int(track.w * max(0.0, min(1.0, fraction)))
            if fill_w > 0:
                pygame.draw.rect(surf, color, (track.x, track.y, fill_w, track.h))

    def draw(self, surf, rect, theme):
        pygame.draw.rect(surf, theme.panel, (rect.x, rect.y, rect.w, self.height))
        pygame.draw.line(surf, theme.line, (rect.x, rect.y + self.height),
                         (rect.right, rect.y + self.height))

        weights = [1.35, 1, 1, 1, 1]               # the clock gets a little more room
        total = sum(weights)
        widths = [int(rect.w * wt / total) for wt in weights]
        widths[-1] += rect.w - sum(widths)

        now = time.localtime()
        if self.battery is None:
            bat = ("BAT", "n/a", theme.dim, None)
        else:
            bat = ("BAT", f"{self.battery}%" + ("+" if self.charging else ""),
                   self._battery_color(theme, self.battery, self.charging), self.battery / 100.0)
        cells = [
            (time.strftime("%a %d %b", now), time.strftime("%H:%M:%S", now), theme.text, None),
            bat,
            ("CPU", f"{self.cpu:.0f}%", self._usage_color(theme, self.cpu), self.cpu / 100.0),
            ("MEM", f"{self.mem:.0f}%", self._usage_color(theme, self.mem), self.mem / 100.0),
            ("DSK", f"{self.disk:.0f}%", self._usage_color(theme, self.disk), self.disk / 100.0),
        ]
        x = rect.x
        for i, ((label, value, color, frac), w) in enumerate(zip(cells, widths)):
            self._segment(surf, theme, x, rect.y, w, label, value, color, frac)
            x += w
            if i < len(cells) - 1:
                pygame.draw.line(surf, theme.line, (x, rect.y + self.pad),
                                 (x, rect.y + self.height - self.pad))
