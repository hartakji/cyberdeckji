"""Button-friendly setup forms and a transactional on-screen text editor."""

import pygame

from .parameters import Form, Number, Text
from .screens import AppScreen, Screen
from .widgets import Label


class SetupScreen(Screen):
    hints = "Up/Down field  Left/Right change  A edit/run  B back"

    def __init__(self, spec):
        self.spec = spec
        self.form = spec.setup()
        if not isinstance(self.form, Form):
            raise ValueError("setup() must return a Form")
        self.values = self.form.defaults()
        self.selected = 0
        self.error = ""

    def submit(self):
        if self.nav.top is not self:
            return
        for index, field in enumerate(self.form.fields):
            try:
                field.validate(self.values[field.key])
            except ValueError as exc:
                self.selected = index
                self.error = f"{field.label}: {exc}"
                return
        params = self.form.validate(self.values)
        self.error = ""
        self.nav.push(AppScreen(self.spec, params=params))

    def action(self, name):
        count = len(self.form.fields)
        if name == "b":
            self.nav.pop()
        elif name in ("up", "down"):
            self.selected = (self.selected + (1 if name == "down" else -1)) % (count + 1)
            self.error = ""
        elif self.selected == count:
            if name == "a":
                self.submit()
        else:
            field = self.form.fields[self.selected]
            if name in ("left", "right"):
                self.values[field.key] = field.adjust(
                    self.values[field.key], 1 if name == "right" else -1)
                self.error = ""
            elif name == "a" and isinstance(field, Text):
                self.nav.push(TextEditor(field, self.values[field.key], self._commit))

    def _commit(self, value):
        self.values[self.form.fields[self.selected].key] = value
        self.error = ""

    def draw(self, surf, rect, theme):
        pad = theme.pad
        line = theme.f_small.get_linesize()
        head = line + 2 * pad
        row_h = max(28, theme.f_normal.get_linesize() + 2 * pad)
        footer = 3 * line + pad
        Label(self.spec.name, size="small", valign="center").draw(
            surf, pygame.Rect(rect.x + pad, rect.y, rect.w - 2 * pad, head), theme)
        visible = max(1, (rect.h - head - footer) // row_h)
        total = len(self.form.fields) + 1
        top = max(0, self.selected - visible + 1)
        for index in range(top, min(total, top + visible)):
            row = pygame.Rect(rect.x + pad, rect.y + head + (index - top) * row_h,
                              rect.w - 2 * pad, row_h - 3)
            if index == self.selected:
                pygame.draw.rect(surf, theme.panel, row)
                pygame.draw.rect(surf, theme.accent, (row.x, row.y, 3, row.h))
            if index == len(self.form.fields):
                Label(self.form.submit_label, align="center", color="accent",
                      valign="center").draw(
                    surf, row, theme)
                continue
            field = self.form.fields[index]
            label_w = row.w * 2 // 5
            value_rect = pygame.Rect(row.x + label_w, row.y, row.w - label_w, row.h)
            if isinstance(field, Number) and field.maximum > field.minimum:
                fraction = ((self.values[field.key] - field.minimum)
                            / (field.maximum - field.minimum))
                pygame.draw.rect(surf, theme.track, value_rect)
                pygame.draw.rect(surf, theme.line,
                                 (value_rect.x, value_rect.y,
                                  int(value_rect.w * fraction), value_rect.h))
            Label(field.label, valign="center").draw(
                surf, pygame.Rect(row.x + pad, row.y, label_w - pad, row.h), theme)
            text = field.display(self.values[field.key])
            if not isinstance(field, Text):
                text = "< " + text + " >"
            Label(text, align="center", valign="center").draw(surf, value_rect, theme)
        message = self.error
        if not message and self.selected < len(self.form.fields):
            message = self.form.fields[self.selected].help
        # Wrap rather than truncate validation messages on small screens.
        width = max(1, rect.w - 2 * pad)
        words, lines, current = message.split(), [], ""
        for word in words:
            trial = (current + " " + word).strip()
            if current and theme.f_small.size(trial)[0] > width:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
        for index, text in enumerate(lines[:3]):
            Label(text, size="small", color="crit" if self.error else "dim").draw(
                surf, pygame.Rect(rect.x + pad, rect.bottom - footer + index * line,
                                  width, line), theme)


class TextEditor(Screen):
    text_input = True
    hints = "D-pad keys  A type  B cancel  Enter accept"

    def __init__(self, field, value, commit):
        self.field, self.value, self.commit = field, value, commit
        self.cursor = len(value)
        self.selected = 0
        self.error = ""
        chars = ("0123456789abcdef.:" if field.format == "ip" else
                 "abcdefghijklmnopqrstuvwxyz0123456789.-_:/@,;!?=+()[]{}'\"\\#$%&*~`^<>|")
        self.keys = list(chars) + ["SHIFT", "SPACE", "DEL", "CUR<", "CUR>", "OK", "CANCEL"]
        self.uppercase = False

    def insert(self, text):
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            self.error = "Control characters are not supported"
            return
        if len(self.value) + len(text) > self.field.max_length:
            self.error = f"Maximum {self.field.max_length} characters"
            return
        self.value = self.value[:self.cursor] + text + self.value[self.cursor:]
        self.cursor += len(text)
        self.error = ""

    def accept(self):
        try:
            self.field.validate(self.value)
        except ValueError as exc:
            self.error = str(exc)
            return
        self.commit(self.value)
        self.nav.pop()

    def action(self, name):
        if name == "b":
            self.nav.pop()
        elif name in ("left", "right"):
            self.selected = (self.selected + (1 if name == "right" else -1)) % len(self.keys)
        elif name in ("up", "down"):
            self.selected = (self.selected + (8 if name == "down" else -8)) % len(self.keys)
        elif name == "a":
            key = self.keys[self.selected]
            if key == "OK":
                self.accept()
            elif key == "CANCEL":
                self.nav.pop()
            elif key == "SHIFT":
                self.uppercase = not self.uppercase
            elif key == "DEL":
                self.backspace()
            elif key in ("CUR<", "CUR>"):
                self.cursor = max(0, min(len(self.value),
                                         self.cursor + (1 if key == "CUR>" else -1)))
            else:
                self.insert(" " if key == "SPACE" else
                            (key.upper() if self.uppercase else key))

    def backspace(self):
        if self.cursor:
            self.value = self.value[:self.cursor - 1] + self.value[self.cursor:]
            self.cursor -= 1
            self.error = ""

    def close(self):
        if pygame.get_init():
            pygame.key.stop_text_input()

    def text_event(self, event):
        if event.type == pygame.TEXTINPUT:
            self.insert(event.text)
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_RETURN:
                self.accept()
            elif event.key == pygame.K_ESCAPE:
                self.nav.pop()
            elif event.key == pygame.K_BACKSPACE:
                self.backspace()
            elif event.key == pygame.K_DELETE:
                self.value = self.value[:self.cursor] + self.value[self.cursor + 1:]
                self.error = ""
            elif event.key == pygame.K_LEFT:
                self.cursor = max(0, self.cursor - 1)
            elif event.key == pygame.K_RIGHT:
                self.cursor = min(len(self.value), self.cursor + 1)
            elif event.key == pygame.K_HOME:
                self.cursor = 0
            elif event.key == pygame.K_END:
                self.cursor = len(self.value)

    def draw(self, surf, rect, theme):
        pad, line = theme.pad, theme.f_small.get_linesize()
        Label(self.field.label, size="small", valign="center").draw(
            surf, pygame.Rect(rect.x + pad, rect.y, rect.w - 2 * pad, line + pad), theme)
        # Keep the insertion cursor visible even for long text.
        before = self.value[:self.cursor]
        available = max(1, rect.w - 4 * pad)
        while before and theme.f_small.size(before + "|")[0] > available // 2:
            before = before[1:]
        shown = before + "|" + self.value[self.cursor:]
        Label(shown, size="small", valign="center").draw(
            surf, pygame.Rect(rect.x + pad, rect.y + line + pad,
                              rect.w - 2 * pad, line + pad), theme)
        start = rect.y + 2 * (line + pad)
        cell_h = max(20, (rect.bottom - start - 2 * line) // 7)
        rows = max(1, (rect.bottom - start - 2 * line) // cell_h)
        top = max(0, self.selected // 8 - rows + 1)
        cell_w = max(1, (rect.w - 2 * pad) // 8)
        for index, key in enumerate(self.keys):
            row, col = divmod(index, 8)
            if not top <= row < top + rows:
                continue
            cell = pygame.Rect(rect.x + pad + col * cell_w, start + (row - top) * cell_h,
                               cell_w - 2, cell_h - 2)
            if index == self.selected:
                pygame.draw.rect(surf, theme.track, cell)
            label = key.upper() if self.uppercase and len(key) == 1 else key
            Label(label, size="small", align="center", valign="center").draw(
                surf, cell, theme)
        if not self.error:
            total_rows = (len(self.keys) + 7) // 8
            message = f"Keys {top + 1}-{min(total_rows, top + rows)}/{total_rows}; Down for more"
        else:
            message = self.error
        Label(message, size="small", color="crit" if self.error else "dim").draw(
            surf, pygame.Rect(rect.x + pad, rect.bottom - 2 * line,
                              rect.w - 2 * pad, 2 * line), theme)
