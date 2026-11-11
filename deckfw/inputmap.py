"""Turns raw pygame events (hat, stick, buttons, keyboard) into named actions.

Actions: up down left right  a b x y  select start  quit
Directions repeat while held. Nothing else in the framework looks at raw
joystick events, so remapping a device only means editing config "buttons"."""

import pygame

DIRS = ("up", "down", "left", "right")

KEYDIR = {
    pygame.K_UP: "up",
    pygame.K_DOWN: "down",
    pygame.K_LEFT: "left",
    pygame.K_RIGHT: "right",
}


class InputMapper:
    DEADZONE = 0.6

    def __init__(self, cfg):
        self.names = {int(btn): name for name, btn in cfg["buttons"].items()}
        self.delay = cfg["repeat_delay"]
        self.rate = cfg["repeat_rate"]
        self._src = {"key": set(), "hat": set(), "axis": set(), "btn": set()}
        self._timer = {d: 0.0 for d in DIRS}
        self._axis = [0.0, 0.0]
        self._down = set()
        self._sticks = []
        self._text_mode = False
        self._open_sticks()

    def _open_sticks(self):
        pygame.joystick.init()
        self._sticks = [pygame.joystick.Joystick(i)
                        for i in range(pygame.joystick.get_count())]

    # -- direction bookkeeping -------------------------------------------------
    def _held(self):
        held = set()
        for source in self._src.values():
            held |= source
        return held

    def _set(self, source, new, out):
        before = self._held()
        self._src[source] = new
        for d in DIRS:
            if d in new and d not in before:
                out.append(d)                       # fire immediately on press
                self._timer[d] = self.delay

    # -- main entry --------------------------------------------------------------
    def process(self, events, dt, text_mode=False, on_text=None):
        if text_mode != self._text_mode:
            self._src["key"].clear()
            self._text_mode = text_mode
            if text_mode:
                pygame.key.start_text_input()
            else:
                pygame.key.stop_text_input()
        out = []
        for e in events:
            t = e.type
            if t == pygame.QUIT:
                out.append("quit")

            elif text_mode and t in (pygame.KEYDOWN, pygame.KEYUP, pygame.TEXTINPUT):
                if on_text is not None and t != pygame.KEYUP:
                    on_text(e)
            elif t == pygame.KEYDOWN:
                d = KEYDIR.get(e.key)
                if d:
                    self._set("key", self._src["key"] | {d}, out)
                elif e.key in (pygame.K_RETURN, pygame.K_SPACE):
                    out.append("a")
                elif e.key in (pygame.K_BACKSPACE, pygame.K_ESCAPE):
                    out.append("b")
                elif e.key == pygame.K_q:
                    out.append("quit")

            elif t == pygame.KEYUP:
                d = KEYDIR.get(e.key)
                if d:
                    self._set("key", self._src["key"] - {d}, out)

            elif t == pygame.JOYHATMOTION:
                x, y = e.value
                new = set()
                if x < 0:
                    new.add("left")
                elif x > 0:
                    new.add("right")
                if y > 0:
                    new.add("up")
                elif y < 0:
                    new.add("down")
                self._set("hat", new, out)

            elif t == pygame.JOYAXISMOTION and e.axis in (0, 1):
                self._axis[e.axis] = e.value
                ax, ay = self._axis
                new = set()
                if ax < -self.DEADZONE:
                    new.add("left")
                elif ax > self.DEADZONE:
                    new.add("right")
                if ay < -self.DEADZONE:
                    new.add("up")
                elif ay > self.DEADZONE:
                    new.add("down")
                self._set("axis", new, out)

            elif t == pygame.JOYBUTTONDOWN:
                name = self.names.get(e.button)
                if name in DIRS:
                    self._set("btn", self._src["btn"] | {name}, out)
                elif name:
                    out.append(name)
                    self._down.add(name)
                    if {"select", "start"} <= self._down:
                        out.append("quit")

            elif t == pygame.JOYBUTTONUP:
                name = self.names.get(e.button)
                if name in DIRS:
                    self._set("btn", self._src["btn"] - {name}, out)
                elif name:
                    self._down.discard(name)

            elif t in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                self._open_sticks()

        # hold-to-repeat
        held = self._held()
        for d in DIRS:
            if d in held:
                self._timer[d] -= dt
                if self._timer[d] <= 0:
                    out.append(d)
                    self._timer[d] += self.rate
        return out
