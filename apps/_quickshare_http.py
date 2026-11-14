"""Temporary, local-only file service. This helper deliberately has no GUI imports."""

import argparse
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import secrets
import signal
from socketserver import TCPServer
import stat
import threading
from urllib.parse import quote, unquote, urlsplit


RESERVE = 256 * 1024 * 1024
CHUNK = 128 * 1024
REPORT_LOCK = threading.Lock()


def emit(event, **values):
    with REPORT_LOCK:
        print(json.dumps({"event": event, **values}, ensure_ascii=True), flush=True)


def filename(value):
    if (not value or value.startswith(".") or "/" in value or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or len(value.encode("utf-8")) > 255):
        raise ValueError("Invalid filename")
    return value


def available(fd):
    info = os.fstatvfs(fd)
    return info.f_bavail * info.f_frsize


class ShareServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, address, root, *, reserve=RESERVE, report=emit):
        self.root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.reserve = reserve
        self.report = report
        self.stopping = threading.Event()
        self.upload_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(8)
        self.timeout = 0.2
        try:
            super().__init__(address, Handler)
        except BaseException:
            os.close(self.root_fd)
            raise

    @property
    def origin(self):
        return "http://{}:{}".format(*self.server_address)

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.report("rejected", peer=address[0], detail="Connection limit reached")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def server_close(self):
        self.stopping.set()
        try:
            super().server_close()
        finally:
            if self.root_fd is not None:
                os.close(self.root_fd)
                self.root_fd = None


class Handler(BaseHTTPRequestHandler):
    server_version = "QuickShare"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, fmt, *args):
        self.server.report("http", peer=self.client_address[0], detail=(fmt % args)[:1000])

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError) as exc:
            self.server.report("error", peer=self.client_address[0], detail=str(exc))

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        super().end_headers()
        self._response_sent = True

    def reply(self, status, message):
        data = (message + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def route(self):
        host = self.headers.get_all("Host", [])
        if host != [self.server.origin[len("http://"):]]:
            raise ValueError("Use the displayed hotspot URL")
        if sum(len(key) + len(value) for key, value in self.headers.items()) > 16384:
            raise ValueError("Headers too large")
        if self.headers.get_all("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Invalid request target")
        return unquote(parsed.path, encoding="utf-8", errors="strict")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path = self.route()
            if path == "/":
                self.page()
            elif path.startswith("/files/"):
                self.download(filename(path[len("/files/"):]))
            else:
                self.reply(404, "Not found")
        except (ValueError, UnicodeError) as exc:
            self.reply(400, str(exc))
        except FileNotFoundError:
            self.reply(404, "Not found")
        except OSError as exc:
            self.server.report("error", peer=self.client_address[0], detail=str(exc))
            if not getattr(self, "_response_sent", False):
                self.reply(403, "File cannot be shared")
            else:
                self.close_connection = True

    def page(self):
        rows = []
        with os.scandir(self.server.root_fd) as entries:
            for entry in entries:
                name = entry.name
                try:
                    filename(name)
                    info = entry.stat(follow_symlinks=False)
                except ValueError:
                    continue
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    rows.append('<li><a href="/files/{}">{}</a> ({} bytes)</li>'.format(
                        quote(name, safe=""), html.escape(name), info.st_size))
                if len(rows) == 1000:
                    rows.append("<li>Listing limited to 1000 files.</li>")
                    break
        nonce = secrets.token_hex(16)
        page = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>QuickShare</title><h1>QuickShare</h1>
<p>Local sharing. Everyone connected can read and add files. No internet access.</p>
<input id="files" type="file" multiple><button id="upload">Upload</button>
<pre id="status"></pre><ul>ROWS</ul>
<script nonce="NONCE">
document.getElementById('upload').onclick = async () => {
  const status = document.getElementById('status');
  const button = document.getElementById('upload');
  button.disabled = true;
  try {
    for (const file of document.getElementById('files').files) {
      status.textContent = 'Uploading ' + file.name;
      const response = await fetch('/files/' + encodeURIComponent(file.name), {
        method: 'POST', headers: {'X-QuickShare-Upload': '1',
                                 'Content-Type': 'application/octet-stream'}, body: file
      });
      if (!response.ok) throw new Error(await response.text());
    }
    location.reload();
  } catch (error) { status.textContent = String(error); }
  finally { button.disabled = false; }
};
</script>"""
        data = page.replace("NONCE", nonce).replace("ROWS", "".join(rows)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'nonce-{}'; "
                         "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'".format(nonce))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def download(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=self.server.root_fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                self.reply(403, "Only regular files are shared")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + quote(name, safe=""))
            self.send_header("Content-Length", str(info.st_size))
            self.end_headers()
            count = 0
            if self.command != "HEAD":
                self.server.report("download_started", peer=self.client_address[0], name=name)
                remaining = info.st_size
                while remaining and not self.server.stopping.is_set():
                    data = stream.read(min(CHUNK, remaining))
                    if not data:
                        raise OSError("Shared file changed during download")
                    self.wfile.write(data)
                    count += len(data)
                    remaining -= len(data)
                if remaining:
                    raise OSError("Download stopped")
                self.server.report("download_complete", peer=self.client_address[0],
                                   name=name, bytes=count)

    def do_POST(self):
        locked = False
        try:
            path = self.route()
            if not path.startswith("/files/"):
                self.reply(404, "Not found")
                return
            name = filename(path[len("/files/"):])
            if (self.headers.get_all("Origin") != [self.server.origin]
                    or self.headers.get_all("X-QuickShare-Upload") != ["1"]):
                self.reply(403, "Uploads must originate from the QuickShare page")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                self.reply(411, "One valid Content-Length is required")
                return
            length = int(lengths[0])
            locked = self.server.upload_lock.acquire(blocking=False)
            if not locked:
                self.reply(409, "Another upload is in progress; retry when it finishes")
                return
            if available(self.server.root_fd) - length < self.server.reserve:
                self.reply(507, "Insufficient space; 256 MiB is reserved")
                return
            saved = self.upload(name, length)
            self.reply(201, "Saved " + saved)
        except (ValueError, UnicodeError) as exc:
            self.reply(400, str(exc))
        except OSError as exc:
            self.server.report("error", peer=self.client_address[0], detail=str(exc))
            try:
                self.reply(507 if exc.errno == 28 else 400, "Upload failed or interrupted")
            except OSError:
                self.close_connection = True
        finally:
            if locked:
                self.server.upload_lock.release()

    def upload(self, name, length):
        root = self.server.root_fd
        temporary = ".quickshare-" + secrets.token_hex(16) + ".partial"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=root)
        self.server.report("upload_started", peer=self.client_address[0], name=name, bytes=length)
        try:
            with os.fdopen(fd, "wb") as stream:
                remaining = length
                while remaining:
                    if self.server.stopping.is_set():
                        raise OSError("Upload stopped")
                    data = self.rfile.read1(min(CHUNK, remaining))
                    if not data:
                        raise OSError("Upload ended before Content-Length")
                    if available(root) - len(data) < self.server.reserve:
                        raise OSError(28, "Disk reserve reached")
                    stream.write(data)
                    stream.flush()
                    if available(root) < self.server.reserve:
                        raise OSError(28, "Disk reserve reached")
                    remaining -= len(data)
                os.fsync(stream.fileno())
            if available(root) < self.server.reserve:
                raise OSError(28, "Disk reserve reached")
            if self.server.stopping.is_set():
                raise OSError("Upload stopped")
            stem, suffix = os.path.splitext(name)
            index = 0
            while True:
                if self.server.stopping.is_set():
                    raise OSError("Upload stopped")
                if index == 0:
                    target = name
                else:
                    addition = "-{}".format(index)
                    tail = addition + suffix
                    if len(tail.encode("utf-8")) >= 200:
                        stem, tail = name, addition
                    budget = 255 - len(tail.encode("utf-8"))
                    prefix = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
                    target = filename(prefix + tail)
                try:
                    os.link(temporary, target, src_dir_fd=root, dst_dir_fd=root,
                            follow_symlinks=False)
                    break
                except FileExistsError:
                    index += 1
            os.fsync(root)
            if available(root) < self.server.reserve:
                os.unlink(target, dir_fd=root)
                raise OSError(28, "Disk reserve reached")
            self.server.report("upload_complete", peer=self.client_address[0],
                               name=target, bytes=length)
            return target
        finally:
            os.unlink(temporary, dir_fd=root)


def serve(server, parent=None):
    try:
        while not server.stopping.is_set():
            if parent is not None and os.getppid() != parent:
                server.report("stopped", detail="Launcher exited")
                break
            server.handle_request()
    finally:
        server.server_close()


def drop_privileges(uid):
    user = pwd.getpwuid(uid)
    if user.pw_uid == 0:
        raise ValueError("The HTTP service requires a non-root owner")
    if os.geteuid() == 0:
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    if os.geteuid() != user.pw_uid or os.getegid() != user.pw_gid:
        raise PermissionError("Cannot run as the selected owner")
    return user


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--parent", type=int, required=True)
    args = parser.parse_args()
    if os.getppid() != args.parent:
        raise ValueError("Launcher exited before file service startup")
    user = drop_privileges(args.uid)
    os.umask(0o077)
    root = Path(user.pw_dir) / "QuickShare"
    root.mkdir(mode=0o700, exist_ok=True)
    server = ShareServer((args.host, args.port), root)
    signal.signal(signal.SIGTERM, lambda *_: server.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: server.stopping.set())
    emit("ready", root=str(root), free=available(server.root_fd),
         uid=os.geteuid(), gid=os.getegid(), url=server.origin)
    serve(server, parent=args.parent)


if __name__ == "__main__":
    main()
