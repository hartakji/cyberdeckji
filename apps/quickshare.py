"""A temporary local-only Wi-Fi share managed by NetworkManager."""

import json
import os
from pathlib import Path
import pwd
import queue
import secrets
import string
import sys
import threading
import time

from deckfw import app, Cancelled, Choice, Form, LogView, Text, Toggle, Widget
from apps import _quickshare_network as network
from apps._user_paths import invoking_user


def owners():
    invoking = invoking_user()
    accounts = [user for user in pwd.getpwall()
                if user.pw_uid != 0 and (user.pw_uid >= 1000 or user == invoking)
                and Path(user.pw_dir).is_absolute() and Path(user.pw_dir).is_dir()
                and Path(user.pw_shell).name not in ("nologin", "false")]
    return tuple(sorted(accounts, key=lambda user: user.pw_name))


def owner(uid):
    user = pwd.getpwuid(uid)
    if user.pw_uid == 0 or user not in owners():
        raise ValueError("Select an available non-root local owner")
    return user


class ShareForm(Form):
    def validate(self, values):
        result = super().validate(values)
        if not result["interface"]:
            raise ValueError("No wireless adapter; connect one and reopen the form")
        owner(result["owner"])
        if len(result["ssid"].encode("utf-8")) > 32:
            raise ValueError("SSID must be at most 32 UTF-8 bytes")
        if result["security"] == "open" and not result["open_consent"]:
            raise ValueError("Confirm that anyone nearby may access the open share")
        return result


def setup():
    devices = network.interfaces()
    users = owners()
    invoking = invoking_user()
    default_owner = invoking.pw_uid if invoking is not None and invoking in users else 0
    return ShareForm([
        Choice("interface", "Wi-Fi interface",
               [(name, name) for name in devices] or [("No wireless adapter", "")],
               devices[0] if devices else ""),
        Choice("owner", "Folder owner",
               [("Select a non-root user", 0)] +
               [("{}: {}/QuickShare".format(user.pw_name, user.pw_dir), user.pw_uid)
                for user in users], default_owner,
               help="Files and the HTTP service use this user's identity."),
        Text("ssid", "Network name", default="QuickShare", max_length=32),
        Choice("security", "Encryption", [
            ("WPA2-Personal", "wpa2"), ("WPA3-Personal", "wpa3"),
            ("Open (no encryption)", "open"),
        ], "wpa2", help="Secured modes generate a new 12-character password."),
        Choice("subnet", "IP range", [(value, value) for value in network.SUBNETS],
               "192.168.10.0/24", help="Local files only; no internet access."),
        Toggle("disconnect", "Allow Wi-Fi disconnect", False,
               help="Required if this adapter is already connected. Restored on exit."),
        Toggle("open_consent", "Allow open access", False,
               help="Anyone nearby can read files or fill this share. HTTP has no TLS."),
    ], submit_label="Enable QuickShare", visible_when={
        "open_consent": lambda values: values["security"] == "open",
    })


class Dashboard(Widget):
    focusable = True

    def __init__(self):
        super().__init__(flex=1)
        self.details = ()
        self.clients = ()
        self.logs = LogView(max_lines=500)
        self.views = [LogView(), LogView(), self.logs]
        self.tab = 0
        self._seen = [None, None]

    def write(self, text):
        clean = "".join(char if ord(char) >= 32 and ord(char) != 127 else "?"
                        for char in str(text))
        self.logs.write(time.strftime("%H:%M:%S ") + clean)

    def action(self, name):
        if name in ("left", "right"):
            self.tab = (self.tab + (1 if name == "right" else -1)) % 3
            return True
        return self.views[self.tab].action(name)

    def draw(self, surf, rect, theme):
        titles = ("Details", "Clients", "Activity")
        heading = "{}  (Left/Right: tabs)".format(titles[self.tab])
        image = theme.render(theme.f_small, theme.fit(theme.f_small, heading, rect.w),
                             theme.accent)
        surf.blit(image, rect.topleft)
        height = theme.f_small.get_linesize() + theme.pad
        for index, values in enumerate((self.details, self.clients)):
            if values != self._seen[index]:
                self.views[index].clear()
                for line in values or ("No connected stations",):
                    self.views[index].write(line)
                self.views[index].offset = 10 ** 9
                self._seen[index] = values
        body = rect.copy()
        body.y += height
        body.h = max(0, body.h - height)
        self.views[self.tab].draw(surf, body, theme)


class Session:
    def __init__(self, ctx, user):
        self.ctx, self.user = ctx, user
        self.board = Dashboard()
        self.events = queue.Queue(maxsize=256)
        self.dropped = 0
        self.failure = ""
        self.ready = None
        self.result = None
        self.thread = None
        self.hotspot = network.Hotspot(ctx, ctx.params["interface"], ctx.params["subnet"],
                                       log=self.board.write)

    def receive(self, line):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                raise ValueError("Invalid service event")
        except (ValueError, TypeError):
            self.failure = "File service: " + line[:500]
            return
        if event["event"] == "ready":
            self.ready = event
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def child(self):
        try:
            self.result = self.ctx.run([
                sys.executable, "-I", "-u", str(Path(__file__).with_name("_quickshare_http.py")),
                "--uid", str(self.user.pw_uid), "--host", self.hotspot.address,
                "--parent", str(os.getpid()),
            ], log=False, on_line=self.receive)
        except Cancelled:
            return
        except Exception as exc:
            self.failure = "Cannot run file service: " + str(exc)

    def drain(self):
        if self.dropped:
            self.board.write("Activity busy: {} events omitted".format(self.dropped))
            self.dropped = 0
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            if event["event"] != "ready":
                self.board.write(json.dumps(event, ensure_ascii=True))

    def run(self):
        params = self.ctx.params
        self.ctx.add(self.board)
        self.board.details = ("Preparing QuickShare...", "Left/Right switches tabs; B stops.")
        password = ("" if params["security"] == "open" else
                    "".join(secrets.choice(string.ascii_letters + string.digits)
                            for _ in range(12)))
        self.hotspot.preflight(params["disconnect"])
        self.hotspot.start(params["ssid"], params["security"], password)
        self.thread = threading.Thread(target=self.child, daemon=True, name="quickshare-http")
        self.thread.start()
        started = time.monotonic()
        try:
            while self.ready is None:
                self.check_child()
                if time.monotonic() - started > 15:
                    raise RuntimeError("File service did not become ready")
                self.ctx.sleep(0.1)
            expected = str(Path(self.user.pw_dir) / "QuickShare")
            if (self.ready.get("root") != expected
                    or self.ready.get("uid") != self.user.pw_uid
                    or self.ready.get("gid") != self.user.pw_gid):
                raise ValueError("File service has an unexpected folder or owner")
            self.board.details = (
                "SSID: " + params["ssid"],
                "Security: " + params["security"].upper(),
                "Password: " + (password or "None - OPEN NETWORK"),
                "URL: http://{}:8080/".format(self.hotspot.address),
                "Owner: " + self.user.pw_name,
                "Folder: " + expected,
                "Interface: " + params["interface"],
                "Subnet: " + params["subnet"],
                "Free at start: {:.1f} GiB".format(self.ready["free"] / 1024 ** 3),
                "Everyone connected can read/add files. No web login or TLS.",
                "256 MiB disk reserve; no fixed file-size cap.",
                "No internet. Tell phones to stay on this Wi-Fi.",
                "Left/Right: tabs; Up/Down: scroll; B: stop and restore.",
            )
            self.board.write("QuickShare ready; local files only")
            previous = set()
            while True:
                self.check_child()
                self.drain()
                self.hotspot.verify()
                clients = self.hotspot.clients()
                current = {mac for mac, _ in clients}
                for mac in sorted(current - previous):
                    self.board.write("Station joined: " + mac)
                for mac in sorted(previous - current):
                    self.board.write("Station left: " + mac)
                previous = current
                self.board.clients = tuple("{}  {}".format(mac, ip) for mac, ip in clients)
                self.ctx.sleep(3)
        finally:
            self.ctx.cancel()
            self.thread.join(timeout=12)

    def check_child(self):
        self.ctx.check()
        if self.failure:
            raise RuntimeError(self.failure)
        if self.thread is not None and not self.thread.is_alive():
            code = self.result.returncode if self.result is not None else "unknown"
            raise RuntimeError("File service stopped unexpectedly ({})".format(code))

    def close(self):
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("File service has not stopped; isolation remains installed. "
                               "Recovery: profile {}; table inet {}".format(
                                   self.hotspot.uuid, self.hotspot.table))
        self.drain()
        self.board.write("Stopping hotspot and restoring the previous connection")
        self.hotspot.close()
        self.board.write("QuickShare stopped")


def shutdown(ctx):
    session = getattr(ctx, "_quickshare", None)
    if session is not None:
        session.close()


@app(name="QuickShare", group="Network", icon="icons/network.png",
     setup=setup, shutdown=shutdown)
def quickshare(ctx):
    user = owner(ctx.params["owner"])
    session = Session(ctx, user)
    ctx._quickshare = session
    session.run()
