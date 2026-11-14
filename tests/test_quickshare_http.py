import http.client
import os
from pathlib import Path
import pwd
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from apps import _quickshare_http as share


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.events = []
        self.error = threading.Event()
        self.uploading = threading.Event()

        def report(event, **values):
            self.events.append((event, values))
            if event == "error":
                self.error.set()
            if event == "upload_started":
                self.uploading.set()

        self.server = share.ShareServer(("127.0.0.1", 0), self.root, reserve=0, report=report)
        self.thread = threading.Thread(target=share.serve, args=(self.server,), daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.stopping.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), "HTTP service did not stop")
        self.directory.cleanup()

    def request(self, method="GET", path="/", body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, name, data=b"example", headers=None):
        values = {"Origin": self.server.origin, "X-QuickShare-Upload": "1"}
        if headers:
            values.update(headers)
        return self.request("POST", "/files/" + quote(name, safe=""), data, values)

    def test_browser_upload_listing_download_and_head(self):
        name = "a <b>& caf\u00e9.txt"
        data = bytes(range(256)) * 2100
        self.assertEqual(self.upload(name, data)[0], 201)
        self.assertEqual((self.root / name).read_bytes(), data)
        status, headers, page = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"&lt;b&gt;&amp;", page)
        self.assertNotIn(b"<b>", page)
        self.assertIn(b"encodeURIComponent(file.name)", page)
        self.assertIn("script-src 'nonce-", headers["Content-Security-Policy"])
        status, headers, received = self.request(path="/files/" + quote(name, safe=""))
        self.assertEqual(status, 200)
        self.assertEqual(received, data)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, headers, received = self.request("HEAD", "/files/" + quote(name, safe=""))
        self.assertEqual((status, received), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(data))
        transfers = [(kind, details["bytes"]) for kind, details in self.events
                     if kind.endswith("_complete")]
        self.assertEqual(transfers, [("upload_complete", len(data)),
                                     ("download_complete", len(data))])

    def test_collision_never_overwrites_or_follows_symlink(self):
        (self.root / "file.txt").write_text("original")
        (self.root / "file-1.txt").symlink_to(self.root / "file.txt")
        self.assertEqual(self.upload("file.txt", b"new")[0], 201)
        self.assertEqual((self.root / "file.txt").read_text(), "original")
        self.assertEqual((self.root / "file-2.txt").read_bytes(), b"new")
        self.assertFalse(any(path.name.startswith(".quickshare") for path in self.root.iterdir()))
        long_name = "z" * 255
        self.assertEqual(self.upload(long_name)[0], 201)
        self.assertEqual(self.upload(long_name)[0], 201)
        self.assertTrue((self.root / ("z" * 253 + "-1")).exists())

    def test_no_traversal_symlinks_hidden_files_or_special_files(self):
        (self.root / "outside").symlink_to("/etc/passwd")
        (self.root / ".private").write_text("not shared")
        os.mkfifo(self.root / "fifo")
        for path, expected in [
            ("/files/../secret", 400), ("/files/%2Fetc%2Fpasswd", 400),
            ("/files/%00", 400), ("/files/%FF", 400),
            ("/files/.private", 400), ("/files/outside", 403),
            ("/files/fifo", 403), ("/files/missing", 404),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], expected)
        self.assertNotIn(b"outside", self.request()[2])
        self.assertNotIn(b".private", self.request()[2])
        for name in ("../escape", "/absolute", "sub/file", ".hidden", "a\\b", "a\nb"):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name)[0], 400)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            share.ShareServer(("127.0.0.1", 0), alias)

    def test_reject_cross_origin_bad_host_and_request_framing(self):
        self.assertEqual(self.upload("x", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.upload("x", headers={"X-QuickShare-Upload": "0"})[0], 403)
        self.assertEqual(self.request("POST", "/files/x", b"data")[0], 403)
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 400)
        self.assertEqual(self.upload("x", headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.upload("x", headers={"Content-Length": "-1"})[0], 411)
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.putrequest("POST", "/files/x")
            connection.putheader("Origin", self.server.origin)
            connection.putheader("X-QuickShare-Upload", "1")
            connection.putheader("Content-Length", "0")
            connection.putheader("Content-Length", "0")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 411)
            response.read()
        finally:
            connection.close()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_disk_reserve_initial_and_during_stream(self):
        self.server.reserve = 256
        with patch.object(share, "available", return_value=260):
            self.assertEqual(self.upload("too-large", b"12345")[0], 507)
        with patch.object(share, "available", side_effect=[261, 256]):
            self.assertEqual(self.upload("shrinking-space", b"12345")[0], 507)
        with patch.object(share, "available", return_value=255):
            self.assertEqual(self.upload("empty", b"")[0], 507)
        with patch.object(share, "available", side_effect=[261, 261, 255]):
            self.assertEqual(self.upload("block-allocation", b"12345")[0], 507)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(self.error.is_set())

    def test_disconnected_bad_request_does_not_crash_service(self):
        connection = socket.create_connection(self.server.server_address, timeout=5)
        connection.sendall(b"GET /files/../bad HTTP/1.1\r\nHost: invalid\r\n\r\n")
        connection.close()
        self.assertEqual(self.request()[0], 200)
        self.assertTrue(self.thread.is_alive())

    def incomplete_upload(self):
        connection = socket.create_connection(self.server.server_address, timeout=5)
        request = ("POST /files/partial HTTP/1.1\r\nHost: {}\r\nOrigin: {}\r\n"
                   "X-QuickShare-Upload: 1\r\nContent-Length: 100\r\n\r\nx").format(
                       self.server.origin[len("http://"):], self.server.origin)
        connection.sendall(request.encode("ascii"))
        self.assertTrue(self.uploading.wait(3))
        return connection

    def test_concurrent_upload_rejected_and_disconnect_removes_partial(self):
        connection = self.incomplete_upload()
        try:
            self.assertEqual(self.upload("other")[0], 409)
        finally:
            connection.close()
        self.assertTrue(self.error.wait(4))
        self.server.stopping.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_shutdown_interrupts_idle_upload_and_removes_partial(self):
        connection = self.incomplete_upload()
        self.server.stopping.set()
        self.thread.join(5)
        connection.close()
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_service_stops_when_launcher_parent_is_gone(self):
        other = share.ShareServer(("127.0.0.1", 0), self.root,
                                  reserve=0, report=lambda *args, **kwargs: None)
        share.serve(other, parent=-1)
        self.assertIsNone(other.root_fd)
        self.assertTrue(other.stopping.is_set())

    def test_file_swapped_to_symlink_before_open_is_not_read(self):
        target = self.root / "race.txt"
        target.write_text("safe")
        original_open = os.open

        def swapped(name, flags, *args, **kwargs):
            if name == "race.txt":
                target.unlink()
                target.symlink_to("/etc/passwd")
            return original_open(name, flags, *args, **kwargs)

        with patch.object(share.os, "open", side_effect=swapped):
            status, _, data = self.request(path="/files/race.txt")
        self.assertEqual(status, 403)
        self.assertNotIn(b"root:", data)


class PrivilegeTests(unittest.TestCase):
    def test_root_drops_groups_gid_uid_before_serving(self):
        user = pwd.struct_passwd(("share", "x", 1234, 2345, "", "/home/share", "/bin/sh"))
        calls = []
        with patch.object(share.pwd, "getpwuid", return_value=user), \
                patch.object(share.os, "geteuid", side_effect=[0, 1234]), \
                patch.object(share.os, "getegid", return_value=2345), \
                patch.object(share.os, "initgroups", side_effect=lambda *v: calls.append(("groups", v))), \
                patch.object(share.os, "setgid", side_effect=lambda v: calls.append(("gid", v))), \
                patch.object(share.os, "setuid", side_effect=lambda v: calls.append(("uid", v))):
            self.assertEqual(share.drop_privileges(1234), user)
        self.assertEqual(calls, [("groups", ("share", 2345)), ("gid", 2345), ("uid", 1234)])

    def test_root_owner_is_never_allowed(self):
        user = pwd.struct_passwd(("root", "x", 0, 0, "", "/root", "/bin/sh"))
        with patch.object(share.pwd, "getpwuid", return_value=user), \
                self.assertRaisesRegex(ValueError, "non-root"):
            share.drop_privileges(0)
