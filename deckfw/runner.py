"""
Non-blocking command runner.

    r = Runner(["ping", "-c", "4", "127.0.0.1"])
    while True:                  # your UI loop
        r.poll()                 # moves new output from the thread into r.lines
        ...draw r.lines...
        if not r.running: ...

Design:
  * The process is read in a background thread, so the UI loop never blocks.
  * The thread only puts text on a queue; poll() (called from the UI thread)
    does all the parsing, so there is no shared-state locking to worry about.
  * The command runs in its own process group so stop() also kills children.
  * Commands are lists, never shell strings: no shell=True, no injection.
"""

import codecs
import collections
import os
import queue
import re
import signal
import subprocess
import threading

# ANSI colour / cursor sequences and other terminal escapes
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
# control characters we never want to render (keeps \t \n \r, handled later)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Runner:
    def __init__(self, cmd, max_lines=2000, cwd=None, env=None):
        self.cmd = list(cmd)
        self.lines = collections.deque(maxlen=max_lines)  # finished lines
        self._raw = ""           # unfinished line, kept raw so \r survives chunking
        self.total = 0           # lines ever pushed (deque may drop old ones)
        self._cursor = 0         # how many lines new_lines() has already handed out
        self.returncode = None   # None while running
        self.error = None        # set if the command could not start
        self.stopped = False     # True if stop() was requested

        self._q = queue.Queue()
        self._proc = None

        full_env = dict(os.environ)
        full_env.update(PYTHONUNBUFFERED="1", TERM="dumb", NO_COLOR="1")
        if env:
            full_env.update(env)

        try:
            self._proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,      # merge stderr into the same stream
                stdin=subprocess.DEVNULL,      # tools must never wait for input
                bufsize=0,
                cwd=cwd,
                env=full_env,
                start_new_session=True,        # own process group
            )
        except OSError as exc:
            self.error = str(exc)
            self.returncode = -1
            self._push(f"[could not start: {exc}]")
            return

        threading.Thread(target=self._reader, daemon=True).start()

    # -- state ---------------------------------------------------------------
    @property
    def partial(self):
        """Current unfinished line (e.g. a live progress bar), ready to draw."""
        return self._after_cr(self._raw)

    @property
    def running(self):
        return self._proc is not None and self.returncode is None

    # -- background thread ---------------------------------------------------
    def _reader(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = self._proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)      # returns as soon as ANY data arrives
            except OSError:
                break
            if not chunk:
                break
            self._q.put(decoder.decode(chunk))
        tail = decoder.decode(b"", final=True)
        if tail:
            self._q.put(tail)
        self._proc.wait()
        self._q.put(None)                      # end-of-output marker

    # -- called from the UI thread -------------------------------------------
    def poll(self, max_chunks=200):
        """Drain pending output. Returns True if anything changed."""
        changed = False
        for _ in range(max_chunks):            # bound the work done per frame
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            changed = True
            if item is None:
                if self._raw:
                    self._push(self._after_cr(self._raw))
                    self._raw = ""
                self.returncode = self._proc.returncode
                continue
            self._feed(item)
        return changed

    def new_lines(self):
        """Finished lines produced since the last call (each line returned once)."""
        delta = self.total - self._cursor
        if delta <= 0:
            return []
        src = list(self.lines)
        out = src[-delta:] if delta <= len(src) else src
        self._cursor = self.total
        return out

    def _feed(self, text):
        text = _CTRL.sub("", _ANSI.sub("", text))
        buf = (self._raw + text).replace("\r\n", "\n")
        parts = buf.split("\n")
        self._raw = parts.pop()
        for part in parts:
            self._push(self._after_cr(part))

    @staticmethod
    def _after_cr(line):
        # Progress bars rewrite the line with \r: keep only the latest version.
        segments = [s for s in line.split("\r") if s]
        return (segments[-1] if segments else "").expandtabs(4)

    def _push(self, line):
        self.lines.append(line)
        self.total += 1

    # -- control ---------------------------------------------------------------
    def stop(self, grace=10.0):
        """Ask nicely (SIGTERM to the whole group), then force-kill."""
        if not self.running:
            return
        self.stopped = True
        self._signal(signal.SIGTERM)
        timer = threading.Timer(grace, self._kill_if_alive)
        timer.daemon = True
        timer.start()

    def wait_stopped(self):
        """Wait for cancellation to finish before restoration commands start."""
        if self._proc is not None:
            self._proc.wait()
        self.poll()

    def _kill_if_alive(self):
        if self._proc is not None and self._proc.poll() is None:
            self._signal(signal.SIGKILL)

    def _signal(self, sig):
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
