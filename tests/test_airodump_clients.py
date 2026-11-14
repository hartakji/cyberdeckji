import contextlib
from dataclasses import replace
import io
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import airodump_clients as tool


REAL_POPEN = subprocess.Popen
AP = "02:11:22:33:44:AA"
MAC = "12:11:22:33:44:BB"
FIRST = "2026-11-14 10:00:00"
LAST = "2026-11-14 10:00:02"
HEADER = ", ".join(tool.FIELDS) + "\n"


def station(mac=MAC, bssid=AP, first=FIRST, last=LAST, power="-45", packets="10"):
    return f"{mac}, {first}, {last}, {power}, {packets}, {bssid}, \n"


def snapshot(*rows):
    return ("BSSID, First time seen, Last time seen, channel, Power, ESSID\n"
            f"{AP}, {FIRST}, {LAST}, 6, -30, Example\n\n"
            + HEADER + "".join(rows) + "\n")


class ParserTests(unittest.TestCase):
    def test_sections_filtering_normalization_and_unknown_power(self):
        clients = tool.parse_snapshot(snapshot(
            station(mac=MAC.lower(), bssid=AP.lower(), power="-1"),
            station(mac="12:00:00:00:00:01", bssid="02:00:00:00:00:01"),
            station(mac="12:00:00:00:00:02", bssid="(not associated)"),
        ), AP.lower())
        self.assertEqual(clients, [tool.Client(MAC, AP, FIRST, LAST, -1, 10)])
        output = io.StringIO()
        tool.render({MAC: clients[0]}, AP, "Loaded", output)
        self.assertIn("unavailable", output.getvalue())
        self.assertNotIn("-1 dBm", output.getvalue())
        self.assertIn("OBSERVED since startup", output.getvalue())
        self.assertIn("Not an authoritative", output.getvalue())

    def test_columns_are_mapped_and_probed_essids_are_not_station_rows(self):
        fields = list(reversed(tool.FIELDS[1:]))
        fields.insert(0, "Station MAC")
        values = dict(zip(tool.FIELDS, [MAC, FIRST, LAST, "-50", "5", AP, "probe"]))
        text = (", ".join(fields) + "\n"
                + ", ".join(values[field] for field in fields) + "\n")
        self.assertEqual(tool.parse_snapshot(text, AP)[0].mac, MAC)
        text = snapshot(station().rstrip("\n") + '"one,two", extra\n')
        self.assertEqual(tool.parse_snapshot(text, AP)[0].packets, 10)

    def test_deduplication_retention_and_stale_snapshots(self):
        first = tool.parse_snapshot(snapshot(
            station(), station(mac=MAC.lower(), last="2026-11-14 10:00:04", packets="12")
        ), AP)
        self.assertEqual(len(first), 1)
        observed = {}
        tool.merge_clients(observed, first)
        tool.merge_clients(observed, tool.parse_snapshot(snapshot(), AP))
        self.assertEqual(observed[MAC].packets, 12)
        tool.merge_clients(observed, tool.parse_snapshot(snapshot(
            station(first="2026-11-14 09:59:59", power="-20", packets="2")
        ), AP))
        self.assertEqual(observed[MAC].first_seen, "2026-11-14 09:59:59")
        self.assertEqual(observed[MAC].last_seen, "2026-11-14 10:00:04")
        self.assertEqual(observed[MAC].power, -45)
        self.assertEqual(observed[MAC].packets, 12)
        newer = replace(observed[MAC], last_seen="2026-11-14 10:00:06", power=-60)
        tool.merge_clients(observed, [newer])
        self.assertEqual(observed[MAC].power, -60)

    def test_invalid_required_fields_are_not_an_empty_success(self):
        for row in [
            station(mac="not-a-mac"), station(bssid="broken"),
            station(first="yesterday"), station(last="2026-11-14 09:00:00"),
            station(first="2026-1-14 10:00:00"),
            station(power="unknown"), station(packets="-3"), station(packets="1.5"),
            f"{MAC}, {FIRST}\n",
        ]:
            with self.subTest(row=row), self.assertRaises(tool.SnapshotError):
                tool.parse_snapshot(snapshot(row), AP)
        for text in ["", "BSSID, Power\n", snapshot(station()).rstrip(),
                     HEADER.replace("Power", "No power"),
                     HEADER + HEADER, HEADER + '"unclosed\n']:
            with self.subTest(text=text), self.assertRaises(tool.SnapshotError):
                tool.parse_snapshot(text, AP)

    def test_valid_empty_station_section_is_distinct_from_waiting(self):
        self.assertEqual(tool.parse_snapshot(snapshot(), AP), [])
        output = io.StringIO()
        tool.render({}, AP, "Snapshot loaded; no matching clients observed yet.", output)
        self.assertIn("no matching clients", output.getvalue())

    def test_missing_partial_and_rewritten_files_retry_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture-01.csv"
            with patch.object(tool.time, "sleep") as sleep:
                with self.assertRaises(tool.WaitingForFile):
                    tool.read_snapshot(path, AP)
                self.assertEqual(sleep.call_count, 2)
            path.write_text(HEADER + station() + f"{MAC}, truncated", encoding="utf-8")
            observed = {}
            with patch.object(tool.time, "sleep") as sleep:
                with self.assertRaises(tool.SnapshotError):
                    tool.merge_clients(observed, tool.read_snapshot(path, AP))
                self.assertEqual(observed, {})
                self.assertEqual(sleep.call_count, 2)
            with patch.object(tool.time, "sleep",
                              side_effect=lambda delay: path.write_text(snapshot(station()))):
                self.assertEqual(len(tool.read_snapshot(path, AP)), 1)

    def test_detects_concurrent_rewrite_and_surfaces_file_permissions(self):
        path = Mock()
        path.stat.side_effect = [
            Mock(st_ino=1, st_size=100, st_mtime_ns=1),
            Mock(st_ino=1, st_size=100, st_mtime_ns=2),
        ]
        path.read_text.return_value = snapshot(station())
        with self.assertRaisesRegex(tool.SnapshotError, "changed while reading"):
            tool.read_snapshot(path, AP, attempts=1)
        path = Mock()
        path.stat.side_effect = PermissionError("denied")
        with self.assertRaises(PermissionError):
            tool.read_snapshot(path, AP)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tool.shutil, "which", return_value="/stub/airodump-ng"))
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = self.stack.enter_context(
            patch.object(tool.subprocess, "Popen", return_value=self.process))

    def test_launch_arguments_interrupt_wait_and_private_directory_cleanup(self):
        directories = []

        def launch(command, **kwargs):
            directory = Path(command[command.index("--write") + 1]).parent
            directories.append(directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            return self.process

        self.popen.side_effect = launch
        with patch.object(tool, "read_snapshot", return_value=[]), \
                patch.object(tool.time, "sleep", side_effect=KeyboardInterrupt):
            tool.capture("wlan0mon", AP, 6, io.StringIO())
        command = self.popen.call_args.args[0]
        prefix = str(directories[0] / "capture")
        self.assertEqual(command, [
            "/stub/airodump-ng", "--bssid", AP, "--channel", "6",
            "--output-format", "csv", "--write", prefix,
            "--write-interval", "2", "--background", "1", "wlan0mon",
        ])
        self.assertTrue(self.popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("shell", self.popen.call_args.kwargs)
        self.process.send_signal.assert_called_once_with(signal.SIGINT)
        self.process.wait.assert_called_once_with(timeout=2)
        self.assertFalse(directories[0].exists())
        self.assertTrue(self.popen.call_args.kwargs["stderr"].closed)

    def test_missing_executable_and_launch_failure(self):
        with patch.object(tool.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                tool.capture("wlan0mon", AP, 6, io.StringIO())
        self.popen.assert_not_called()
        self.popen.side_effect = PermissionError("execution denied")
        with self.assertRaisesRegex(RuntimeError, "cannot launch.*execution denied"):
            tool.capture("wlan0mon", AP, 6, io.StringIO())
        prefix = self.popen.call_args.args[0][8]
        self.assertFalse(Path(prefix).parent.exists())

    def test_early_exit_includes_stderr_and_reaps_process(self):
        def launch(command, **kwargs):
            kwargs["stderr"].write(b"monitor interface: Permission denied\n")
            return self.process

        self.popen.side_effect = launch
        self.process.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "Permission denied") as failure:
            tool.capture("wlan0mon", AP, 6, io.StringIO())
        self.assertIn("status 1", str(failure.exception))
        self.assertIn("capture permissions", str(failure.exception))
        self.process.wait.assert_called_once_with()
        self.process.send_signal.assert_not_called()

    def test_persistent_malformed_or_missing_snapshots_fail_explicitly(self):
        for error in [tool.SnapshotError("bad packet count"),
                      tool.WaitingForFile("waiting for initial CSV")]:
            with self.subTest(error=error), \
                    patch.object(tool, "read_snapshot", side_effect=error) as read, \
                    patch.object(tool.time, "sleep"):
                output = io.StringIO()
                with self.assertRaisesRegex(RuntimeError, "no usable capture snapshot"):
                    tool.capture("wlan0mon", AP, 6, output)
                self.assertEqual(read.call_count, 5)
                self.assertIn("retry 5/5", output.getvalue())
                self.assertIn("retaining previous", output.getvalue())

    def test_transient_error_retains_table_and_recovers(self):
        client = tool.parse_snapshot(snapshot(station()), AP)[0]
        output = io.StringIO()
        with patch.object(tool, "read_snapshot", side_effect=[
                [client], tool.SnapshotError("truncated"), [], KeyboardInterrupt]), \
                patch.object(tool.time, "sleep"):
            tool.capture("wlan0mon", AP, 6, output)
        self.assertEqual(output.getvalue().count(MAC), 3)
        self.assertIn("absent clients are retained", output.getvalue())

    def test_termination_escalation_is_bounded_and_only_targets_owned_process(self):
        self.process.wait.side_effect = [
            subprocess.TimeoutExpired("stub", 2),
            subprocess.TimeoutExpired("stub", 2), 0,
        ]
        tool.stop_capture(self.process)
        self.process.send_signal.assert_called_once_with(signal.SIGINT)
        self.process.terminate.assert_called_once_with()
        self.process.kill.assert_called_once_with()
        self.assertEqual(self.process.wait.call_count, 3)
        self.assertTrue(all(call.kwargs == {"timeout": 2}
                            for call in self.process.wait.call_args_list))

    def test_real_stub_executable_failure_is_reaped_without_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "airodump-ng"
            executable.write_text("#!/bin/sh\necho 'stub permission denied' >&2\nexit 7\n")
            executable.chmod(0o700)
            with patch.object(tool.shutil, "which", return_value=str(executable)), \
                    patch.object(tool.subprocess, "Popen", side_effect=REAL_POPEN) as launch:
                with self.assertRaisesRegex(RuntimeError, "stub permission denied"):
                    tool.capture("wlan0mon", AP, 6, io.StringIO())
                prefix = launch.call_args.args[0][8]
                self.assertFalse(Path(prefix).parent.exists())


class CliTests(unittest.TestCase):
    def test_required_and_invalid_arguments_never_launch(self):
        for args in [
            [], ["--interface", "-oops", "--bssid", AP, "--channel", "6"],
            ["--interface", "wlan0mon", "--bssid", "FF:FF:FF:FF:FF:FF", "--channel", "6"],
            ["--interface", "wlan0mon", "--bssid", AP, "--channel", "0"],
            ["--interface", "wlan0mon", "--bssid", AP, "--channel", "234"],
            ["--interface", "wlan0mon", "--bssid", AP, "--channel", "six"],
            ["--interface", "wlan0;touch x", "--bssid", AP, "--channel", "6"],
        ]:
            with self.subTest(args=args), patch.object(tool, "capture") as capture, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                tool.main(args)
            capture.assert_not_called()

    def test_linux_precondition_and_error_exit_status(self):
        args = ["--interface", "wlan0mon", "--bssid", AP.lower(), "--channel", "6"]
        with patch.object(tool.sys, "platform", "darwin"), \
                patch.object(tool, "capture") as capture, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(tool.main(args), 1)
            self.assertIn("requires Linux", stderr.getvalue())
            capture.assert_not_called()
        with patch.object(tool.sys, "platform", "linux"), \
                patch.object(tool, "capture", side_effect=RuntimeError("capture failed")), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(tool.main(args), 1)
            self.assertIn("capture failed", stderr.getvalue())

    def test_sigterm_uses_interrupt_cleanup_and_restores_handler(self):
        args = ["--interface", "wlan0mon", "--bssid", AP.lower(), "--channel", "6"]
        previous = signal.getsignal(signal.SIGTERM)

        def capture(interface, bssid, channel, output):
            self.assertEqual((interface, bssid, channel), ("wlan0mon", AP, 6))
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        with patch.object(tool.sys, "platform", "linux"), \
                patch.object(tool, "capture", side_effect=capture):
            self.assertEqual(tool.main(args), 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


if __name__ == "__main__":
    unittest.main()
