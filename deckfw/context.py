"""What an app sees.

    from deckfw import app

    @app(name="Port check", group="Network")
    def port_check(ctx):
        ctx.progress(0.5, "Halfway")
        ctx.log("hello")
        ctx.sleep(1)

Your function runs in its own worker thread, so it may block, sleep or run
commands freely: the screen keeps drawing. When the user presses B the
context is cancelled and the next ctx.sleep()/ctx.run()/ctx.check() raises
Cancelled, which unwinds your function (use try/finally for cleanup)."""

import collections
import threading
import time

from .runner import Runner
from .widgets import VBox, HBox, Stat, ProgressBar, Graph, LogView

RunResult = collections.namedtuple("RunResult", "returncode lines")


class Cancelled(Exception):
    """Raised inside an app when the user cancels it."""


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------
class AppSpec:
    def __init__(self, name, run, group="", description="", order=100, source="native",
                 icon=None, setup=None, shutdown=None):
        self.name, self.run, self.group = name, run, group
        self.description, self.order = description, order
        self.source = source
        self.icon = icon
        self.setup = setup
        self.shutdown = shutdown


_REGISTRY = []


def register(spec):
    _REGISTRY.append(spec)
    return spec


def registered():
    return list(_REGISTRY)


def app(name=None, *, group="", description="", order=100, icon=None, setup=None,
        shutdown=None):
    """Decorator that registers a function as an app. Works as @app or @app(...).

    setup is an optional zero-argument function returning a Form; the app reads
    validated selections from ctx.params after the user activates Run.
    shutdown(ctx) optionally restores state after execution ends or is cancelled.
    group="Network/WiFi" puts it in nested menus (any depth). Without a group,
    the app takes the name of the apps/ sub-folder it lives in.
    order: lower numbers come first within a menu (default 100, then A-Z)."""
    def decorator(fn):
        label = name if isinstance(name, str) else fn.__name__.replace("_", " ").title()
        register(AppSpec(label, fn, group, description or (fn.__doc__ or "").strip(),
                         order, icon=icon, setup=setup, shutdown=shutdown))
        return fn

    if callable(name):                 # bare @app
        fn, name = name, None
        return decorator(fn)
    return decorator


# ----------------------------------------------------------------------------
# Context
# ----------------------------------------------------------------------------
class Context:
    def __init__(self, spec, params=None):
        self.spec = spec
        self.params = dict(params or {})
        self.title = spec.name
        self.note = ""                 # free text shown in the header
        self.status = "running"        # running | done | cancelled | error
        self.error = None
        self.root = VBox([])
        self._cancel = threading.Event()
        self._runners = []
        self._lock = threading.Lock()
        self._log = None
        self._bar = None
        self._shutting_down = False

    # -- building the screen ---------------------------------------------------
    def add(self, widget, flex=None, index=None):
        """Put a widget on the screen. index=0 puts it at the top."""
        if flex is not None:
            widget.flex = flex
        if index is None:
            self.root.children.append(widget)
        else:
            self.root.children.insert(index, widget)
        return widget

    def row(self, *widgets, flex=None):
        """Place widgets side by side: ctx.row(Stat(...), Stat(...))."""
        return self.add(HBox(list(widgets)), flex=flex)

    def clear(self):
        del self.root.children[:]
        self._log = self._bar = None

    # -- quick helpers (create their widget on first use) ----------------------
    def _ensure_log(self):
        if self._log is None:
            self._log = self.add(LogView())
        return self._log

    def log(self, text, color=None):
        self._ensure_log().write(text, color)

    def progress(self, value, label=None, text=None):
        """Set the progress bar (0..1, or None for indeterminate)."""
        if self._bar is None:
            self._bar = self.add(ProgressBar(), index=0)
        self._bar.value = value
        if label is not None:
            self._bar.label = label
        if text is not None:
            self._bar.text = text

    # -- cancellation ----------------------------------------------------------
    @property
    def cancelled(self):
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()
        if self._shutting_down:
            return
        with self._lock:
            runners = list(self._runners)
        for runner in runners:
            runner.stop()

    def check(self):
        if self._cancel.is_set() and not self._shutting_down:
            raise Cancelled()

    def sleep(self, seconds):
        """Sleep, but wake immediately (raising Cancelled) if the user cancels."""
        if self._shutting_down:
            time.sleep(seconds)
            return
        if self._cancel.wait(seconds):
            raise Cancelled()

    def loop(self, interval):
        """for _ in ctx.loop(1.0): ...  runs once per interval until cancelled."""
        nxt = time.monotonic()
        while not self._cancel.is_set():
            yield
            nxt += interval
            delay = nxt - time.monotonic()
            if delay > 0:
                if self._cancel.wait(delay):
                    return
            else:
                nxt = time.monotonic()  # fell behind: don't try to catch up

    # -- running commands --------------------------------------------------------
    def run(self, cmd, log=True, on_line=None, protocol=False, cwd=None, env=None):
        """Run a command (a list, never a shell string) and wait for it.

        log=True      show output in the screen's log (default)
        log=False     don't show output
        log=LogView   show output in that specific LogView
        on_line       callback(line) for each finished line, to parse output
        protocol=True lines starting with '@' drive widgets (see Protocol)
        env           per-process environment overrides (does not change os.environ)

        Returns RunResult(returncode, lines). Raises Cancelled if the user
        cancels; the process (and its children) are killed first."""
        self.check()
        if protocol:
            emit = Protocol(self, log if isinstance(log, LogView) else None).feed
        elif isinstance(log, LogView):
            emit = log.write
        elif log:
            emit = self.log
        else:
            emit = lambda line: None

        runner = Runner(cmd, cwd=cwd, env=env)
        with self._lock:
            self._runners.append(runner)
        lines = collections.deque(maxlen=5000)
        try:
            while True:
                runner.poll()
                for line in runner.new_lines():
                    lines.append(line)
                    emit(line)
                    if on_line:
                        on_line(line)
                if not runner.running:
                    break
                if self._shutting_down:
                    time.sleep(0.03)
                elif self._cancel.wait(0.03):
                    runner.stop()
                    runner.wait_stopped()
                    raise Cancelled()
        finally:
            with self._lock:
                if runner in self._runners:
                    self._runners.remove(runner)
        return RunResult(runner.returncode, list(lines))


# ----------------------------------------------------------------------------
# Script protocol: lets shell scripts (or any language) drive widgets
# ----------------------------------------------------------------------------
class Protocol:
    """Lines a script prints that start with '@' control the screen:

        @progress 40 Scanning ports     progress bar, 0-100 ('?' = indeterminate)
        @graph latency 12.5             value for the graph named "latency"
        @stat hosts=12                  big number tile
        @status halfway there           note shown in the header

    Every other line goes to the log."""

    def __init__(self, ctx, log=None):
        self.ctx = ctx
        self.log = log or ctx._ensure_log()
        self.stats = {}
        self.stat_row = None
        self.bar = None
        self.graphs = {}
        self._top = 0                  # widgets pinned above the graphs
        self._handlers = {
            "progress": self._progress,
            "graph": self._graph,
            "stat": self._stat,
            "status": self._status,
        }

    def feed(self, line):
        if not line.startswith("@"):
            self.log.write(line)
            return
        word, _, rest = line[1:].partition(" ")
        handler = self._handlers.get(word)
        if handler is None:
            self.log.write(line)
            return
        try:
            handler(rest.strip())
        except ValueError:
            self.log.write("[bad directive] " + line, color="warn")

    def _progress(self, rest):
        value, _, label = rest.partition(" ")
        if value in ("?", "-"):
            frac = None
        else:
            frac = max(0.0, min(100.0, float(value.rstrip("%")))) / 100.0
        if self.bar is None:
            self.bar = ProgressBar()
            self.ctx.add(self.bar, index=self._top)
            self._top += 1
        self.bar.value = frac
        if label.strip():
            self.bar.label = label.strip()

    def _graph(self, rest):
        name, _, number = rest.rpartition(" ")
        value = float(number)
        name = name.strip() or "graph"
        graph = self.graphs.get(name)
        if graph is None:
            graph = Graph(title=name, fmt="{:.1f}")
            self.graphs[name] = graph
            children = self.ctx.root.children
            index = children.index(self.log) if self.log in children else len(children)
            self.ctx.add(graph, index=index)
        graph.push(value)

    def _stat(self, rest):
        label, sep, value = rest.partition("=")
        if not sep:
            raise ValueError("expected label=value")
        label, value = label.strip(), value.strip()
        stat = self.stats.get(label)
        if stat is None:
            stat = Stat(label, value)
            self.stats[label] = stat
            if self.stat_row is None:
                self.stat_row = HBox([])
                self.ctx.add(self.stat_row, index=0)
                self._top += 1
            self.stat_row.children.append(stat)
        stat.value = value

    def _status(self, rest):
        self.ctx.note = rest
