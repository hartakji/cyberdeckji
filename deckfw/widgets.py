"""UI components.

Rules of the road
  * Widgets are plain objects. Change their attributes (bar.value = 0.5,
    graph.push(12), log.write("hi")) from any thread: that is safe.
  * Only the main thread draws. Never call draw() yourself.
  * flex = 0 means "take the natural height"; flex > 0 means "share the
    leftover space" (Graph and LogView default to flex = 1).
  * Colors are theme names ("ok", "warn", "crit", "accent", "dim", "text")
    or (r, g, b) tuples.
"""

import collections
import threading
import time

import pygame


def find_focusable(widget):
    """First widget in the tree that wants input (LogView, ListView...)."""
    if widget.focusable:
        return widget
    for child in list(getattr(widget, "children", [])):
        found = find_focusable(child)
        if found is not None:
            return found
    return None


class Widget:
    flex = 0
    focusable = False

    def __init__(self, flex=None):
        if flex is not None:
            self.flex = flex

    def height(self, theme):          # natural height in pixels
        return 0

    def draw(self, surf, rect, theme):
        pass

    def action(self, name):           # return True if the action was used
        return False


# ----------------------------------------------------------------------------
# Layout containers
# ----------------------------------------------------------------------------
class VBox(Widget):
    """Stack children top to bottom."""

    def __init__(self, children=None, gap=None, flex=None):
        super().__init__(flex)
        self.children = list(children or [])
        self.gap = gap

    def _gap(self, theme):
        return theme.pad if self.gap is None else self.gap

    def height(self, theme):
        kids = list(self.children)
        return (sum(k.height(theme) for k in kids)
                + self._gap(theme) * max(0, len(kids) - 1))

    def draw(self, surf, rect, theme):
        kids = list(self.children)      # snapshot: workers may add widgets live
        if not kids:
            return
        gap = self._gap(theme)
        natural = [0 if k.flex else k.height(theme) for k in kids]
        flex_total = sum(k.flex for k in kids)
        free = rect.h - sum(natural) - gap * (len(kids) - 1)
        clip = surf.get_clip()
        y = rect.y
        for kid, nat in zip(kids, natural):
            if kid.flex:
                h = int(free * kid.flex / flex_total) if (free > 0 and flex_total) else 0
            else:
                h = nat
            if h > 0 and y < rect.bottom:
                r = pygame.Rect(rect.x, y, rect.w, min(h, rect.bottom - y))
                surf.set_clip(r.clip(clip))
                kid.draw(surf, r, theme)
                surf.set_clip(clip)
            y += h + gap


class HBox(Widget):
    """Place children side by side. Each child's flex is its width share (default 1)."""

    def __init__(self, children=None, gap=None, flex=None):
        super().__init__(flex)
        self.children = list(children or [])
        self.gap = gap

    def _gap(self, theme):
        return theme.pad * 2 if self.gap is None else self.gap

    def height(self, theme):
        return max((k.height(theme) for k in list(self.children)), default=0)

    def draw(self, surf, rect, theme):
        kids = list(self.children)
        if not kids:
            return
        gap = self._gap(theme)
        weights = [k.flex or 1 for k in kids]
        total = float(sum(weights))
        avail = rect.w - gap * (len(kids) - 1)
        clip = surf.get_clip()
        x = rect.x
        for i, (kid, wt) in enumerate(zip(kids, weights)):
            w = rect.right - x if i == len(kids) - 1 else int(avail * wt / total)
            if w > 0:
                r = pygame.Rect(x, rect.y, w, rect.h)
                surf.set_clip(r.clip(clip))
                kid.draw(surf, r, theme)
                surf.set_clip(clip)
            x += w + gap


# ----------------------------------------------------------------------------
# Text widgets
# ----------------------------------------------------------------------------
class Label(Widget):
    def __init__(self, text="", color=None, size="normal", align="left", flex=None,
                 valign="top"):
        super().__init__(flex)
        self.text, self.color, self.size, self.align = text, color, size, align
        self.valign = valign

    def height(self, theme):
        return theme.font(self.size).get_linesize()

    def draw(self, surf, rect, theme):
        font = theme.font(self.size)
        text = theme.fit(font, str(self.text), rect.w)
        img = theme.render(font, text, theme.color(self.color, theme.text))
        x = rect.x
        if self.align == "center":
            x = rect.x + (rect.w - img.get_width()) // 2
        elif self.align == "right":
            x = rect.right - img.get_width()
        y = rect.y
        if self.valign == "center":
            y += (rect.h - img.get_height()) // 2
        surf.blit(img, (x, y))


class Stat(Widget):
    """A small label over a big value: Stat("Hosts up", "12")."""

    def __init__(self, label="", value="", color=None, flex=None):
        super().__init__(flex)
        self.label, self.value, self.color = label, value, color

    def height(self, theme):
        return theme.f_small.get_linesize() + theme.f_large.get_linesize()

    def draw(self, surf, rect, theme):
        small, large = theme.f_small, theme.f_large
        lab = theme.fit(small, str(self.label), rect.w)
        val = theme.fit(large, str(self.value), rect.w)
        surf.blit(theme.render(small, lab, theme.dim), (rect.x, rect.y))
        surf.blit(theme.render(large, val, theme.color(self.color, theme.text)),
                  (rect.x, rect.y + small.get_linesize()))


# ----------------------------------------------------------------------------
# Progress bar
# ----------------------------------------------------------------------------
class ProgressBar(Widget):
    """value: 0.0 to 1.0, or None for an indeterminate (sweeping) bar."""

    def __init__(self, value=0.0, label="", text=None, color=None, flex=None):
        super().__init__(flex)
        self.value, self.label, self.text, self.color = value, label, text, color

    @staticmethod
    def _bar_h(theme):
        return max(8, theme.pad * 3 // 2)

    def height(self, theme):
        return theme.f_small.get_linesize() + 2 + self._bar_h(theme) + theme.pad // 2

    def draw(self, surf, rect, theme):
        small = theme.f_small
        v = None if self.value is None else max(0.0, min(1.0, float(self.value)))
        color = theme.color(self.color, theme.accent)

        if self.label:
            label = theme.fit(small, str(self.label), rect.w * 3 // 4)
            surf.blit(theme.render(small, label, theme.dim), (rect.x, rect.y))
        txt = self.text if self.text is not None else ("" if v is None else f"{v * 100:.0f}%")
        if txt:
            img = theme.render(small, str(txt), theme.text)
            surf.blit(img, (rect.right - img.get_width(), rect.y))

        bar = pygame.Rect(rect.x, rect.y + small.get_linesize() + 2, rect.w, self._bar_h(theme))
        pygame.draw.rect(surf, theme.track, bar)
        if v is None:
            seg = max(8, bar.w // 4)
            pos = int(((time.monotonic() * 0.8) % 1.0) * (bar.w + seg)) - seg
            sweep = pygame.Rect(bar.x + pos, bar.y, seg, bar.h).clip(bar)
            if sweep.w > 0:
                pygame.draw.rect(surf, color, sweep)
        else:
            w = int(bar.w * v)
            if w > 0:
                pygame.draw.rect(surf, color, (bar.x, bar.y, w, bar.h))


# ----------------------------------------------------------------------------
# Graph
# ----------------------------------------------------------------------------
class Graph(Widget):
    """Scrolling line graph with one or more series.

        g = Graph(title="CPU", unit="%", y_min=0, y_max=100)
        g.push(42)                 # series 0
        g.push(61, series=1)       # series 1 (give names=[...] for a legend)

    y_min / y_max default to auto-scaling."""

    def __init__(self, title="", unit="", max_points=120, y_min=None, y_max=None,
                 names=None, fmt="{:.0f}", flex=1):
        super().__init__(flex)
        self.title, self.unit = title, unit
        self.max_points = max(2, int(max_points))
        self.y_min, self.y_max = y_min, y_max
        self.names = list(names or [])
        self.fmt = fmt
        self.series = []
        self._lock = threading.Lock()

    def push(self, value, series=0):
        with self._lock:
            while len(self.series) <= series:
                self.series.append(collections.deque(maxlen=self.max_points))
            self.series[series].append(float(value))

    def clear(self):
        with self._lock:
            self.series = []

    def height(self, theme):
        return theme.f_small.get_linesize() * 4

    def draw(self, surf, rect, theme):
        pad, small = theme.pad, theme.f_small
        pygame.draw.rect(surf, theme.panel, rect)
        pygame.draw.rect(surf, theme.line, rect, 1)

        with self._lock:
            data = [list(s) for s in self.series]

        # header: latest values on the right, title on the left
        head_y = rect.y + pad // 2
        x = rect.right - pad
        for i in range(len(data) - 1, -1, -1):
            if not data[i]:
                continue
            name = (self.names[i] + " ") if i < len(self.names) else ""
            label = name + self.fmt.format(data[i][-1]) + self.unit
            img = theme.render(small, label, theme.series[i % len(theme.series)])
            x -= img.get_width()
            surf.blit(img, (x, head_y))
            x -= pad
        if self.title:
            title = theme.fit(small, self.title, x - (rect.x + pad))
            surf.blit(theme.render(small, title, theme.dim), (rect.x + pad, head_y))

        top = head_y + small.get_linesize() + pad // 2
        plot = pygame.Rect(rect.x + pad, top, rect.w - 2 * pad, rect.bottom - pad - top)
        if plot.w < 20 or plot.h < 12:
            return

        # value range
        flat = [v for s in data for v in s]
        lo, hi = self.y_min, self.y_max
        if lo is None:
            lo = 0.0 if (not flat or min(flat) >= 0) else min(flat)
        if hi is None:
            hi = max(flat) * 1.1 if flat else 1.0
        if hi <= lo:
            hi = lo + 1.0

        # grid + axis labels
        for k in range(3):
            gy = plot.y + int((plot.h - 1) * k / 2)
            pygame.draw.line(surf, theme.track, (plot.x, gy), (plot.right - 1, gy))
        surf.blit(theme.render(small, self.fmt.format(hi), theme.dim), (plot.x + 3, plot.y + 1))
        lo_img = theme.render(small, self.fmt.format(lo), theme.dim)
        surf.blit(lo_img, (plot.x + 3, plot.bottom - lo_img.get_height() - 1))

        # series: newest point sits on the right edge
        step = plot.w / float(self.max_points - 1)
        span = hi - lo
        for i, values in enumerate(data):
            n = len(values)
            if n == 0:
                continue
            pts = []
            for j, v in enumerate(values):
                px = plot.right - 1 - int((n - 1 - j) * step)
                py = plot.bottom - 1 - int((v - lo) / span * (plot.h - 2))
                pts.append((px, max(plot.top, min(plot.bottom - 1, py))))
            color = theme.series[i % len(theme.series)]
            if n >= 2:
                pygame.draw.lines(surf, color, False, pts, 2)
            else:
                pygame.draw.circle(surf, color, pts[0], 3)


# ----------------------------------------------------------------------------
# Scrollable log
# ----------------------------------------------------------------------------
class LogView(Widget):
    """Scrollable, auto-following text log.

    write() is thread-safe. Up/Down scroll, Left/Right page. Scrolling up
    freezes the view so you can read while new lines keep arriving; scroll
    back to the bottom to follow again."""

    focusable = True

    def __init__(self, max_lines=2000, flex=1):
        super().__init__(flex)
        self._raw = collections.deque(maxlen=max_lines)    # (text, color)
        self._total = 0
        self._lock = threading.Lock()
        self._wrapped = []
        self._seen = 0
        self._cols = 0
        self._rows = 10
        self.offset = 0                                    # lines scrolled up from bottom

    def write(self, text, color=None):
        with self._lock:
            for line in str(text).split("\n"):
                self._raw.append((line.expandtabs(4), color))
                self._total += 1

    def clear(self):
        with self._lock:
            self._raw.clear()
            self._total = 0
        self._wrapped, self._seen, self.offset = [], 0, 0

    @staticmethod
    def _wrap(text, color, cols):
        if not text:
            return [("", color)]
        return [(text[i:i + cols], color) for i in range(0, len(text), cols)]

    def _sync(self, cols):
        with self._lock:
            total = self._total
            src = list(self._raw)
        if cols != self._cols:                             # width changed: rewrap all
            self._cols = cols
            self._wrapped = [p for t, c in src for p in self._wrap(t, c, cols)]
            self._seen = total
        else:
            delta = total - self._seen
            if delta <= 0:
                return
            new = src[-delta:] if delta <= len(src) else src
            self._seen = total
            added = 0
            for t, c in new:
                for piece in self._wrap(t, c, cols):
                    self._wrapped.append(piece)
                    added += 1
            if self.offset > 0:
                self.offset += added                       # keep the view still
        if len(self._wrapped) > 6000:
            del self._wrapped[:len(self._wrapped) - 6000]

    def _clamp(self):
        self.offset = max(0, min(self.offset, max(0, len(self._wrapped) - self._rows)))

    def action(self, name):
        page = max(1, self._rows - 1)
        if name == "up":
            self.offset += 1
        elif name == "down":
            self.offset -= 1
        elif name == "left":
            self.offset += page
        elif name == "right":
            self.offset -= page
        else:
            return False
        self._clamp()
        return True

    def draw(self, surf, rect, theme):
        font, pad = theme.f_mono, theme.pad
        lh = font.get_linesize()
        cw = max(1, font.size("M")[0])
        cols = max(8, (rect.w - 2 * pad) // cw)
        self._rows = max(1, rect.h // lh)
        self._sync(cols)
        self._clamp()

        end = len(self._wrapped) - self.offset
        start = max(0, end - self._rows)
        y = rect.y
        for text, color in self._wrapped[start:end]:
            surf.blit(theme.render(font, text, theme.color(color, theme.text)), (rect.x + pad, y))
            y += lh

        n = len(self._wrapped)
        if n > self._rows:                                 # scrollbar
            frac = 1.0 - self.offset / float(max(1, n - self._rows))
            thumb = max(12, int(rect.h * self._rows / n))
            ty = rect.y + int((rect.h - thumb) * frac)
            pygame.draw.rect(surf, theme.track, (rect.right - 4, rect.y, 3, rect.h))
            pygame.draw.rect(surf, theme.dim, (rect.right - 4, ty, 3, thumb))


# ----------------------------------------------------------------------------
# Selectable list
# ----------------------------------------------------------------------------
class ListView(Widget):
    """items: (label, value), (label, value, hint), or (label, value, hint, icon).
    A calls on_select(value)."""

    focusable = True

    def __init__(self, items=None, on_select=None, flex=1):
        super().__init__(flex)
        self.items = list(items or [])
        self.on_select = on_select
        self.selected = 0
        self._top = 0

    def action(self, name):
        n = len(self.items)
        if not n:
            return False
        if name == "up":
            self.selected = (self.selected - 1) % n
        elif name == "down":
            self.selected = (self.selected + 1) % n
        elif name == "a":
            if self.on_select:
                self.on_select(self.items[self.selected][1])
        else:
            return False
        return True

    def draw(self, surf, rect, theme):
        font, small, pad = theme.f_normal, theme.f_small, theme.pad
        row_h = int(font.get_linesize() * 1.5)
        rows = max(1, rect.h // row_h)
        items = list(self.items)
        icon_size = max(1, min(font.get_linesize(), row_h - 8))
        icon_space = icon_size + pad if any(len(item) > 3 for item in items) else 0
        self.selected = min(self.selected, max(0, len(items) - 1))
        if self.selected < self._top:
            self._top = self.selected
        elif self.selected >= self._top + rows:
            self._top = self.selected - rows + 1

        for idx in range(self._top, min(len(items), self._top + rows)):
            item = items[idx]
            label, hint = item[0], (item[2] if len(item) > 2 else "")
            row = pygame.Rect(rect.x, rect.y + (idx - self._top) * row_h, rect.w, row_h - 4)
            if idx == self.selected:
                pygame.draw.rect(surf, theme.panel, row)
                pygame.draw.rect(surf, theme.accent, (row.x, row.y, 4, row.h))
                color = theme.text
            else:
                color = theme.dim

            hint_w = 0
            if hint:
                himg = theme.render(small, str(hint), theme.dim)
                hint_w = himg.get_width() + pad * 2
                surf.blit(himg, (row.right - himg.get_width() - pad,
                                 row.centery - himg.get_height() // 2))
            if len(item) > 3 and item[3]:
                icon = theme.icon(item[3], icon_size)
                if icon is not None:
                    surf.blit(icon, (row.x + pad * 2, row.centery - icon.get_height() // 2))
            text = theme.fit(font, str(label), row.w - pad * 3 - hint_w - icon_space)
            img = theme.render(font, text, color)
            surf.blit(img, (row.x + pad * 2 + icon_space,
                            row.centery - img.get_height() // 2))
