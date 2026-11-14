import contextlib
import io
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tools import airodump_clients as tool


AP = "02:11:22:33:44:AA"
MAC = "12:11:22:33:44:BB"
HEADER = " BSSID              STATION            PWR    Rate    Lost   Frames  Notes  Probes"


def station(mac=MAC, bssid=AP, power=-45, packets=10):
    return f" {bssid}  {mac}  {power:4}    6e- 1     0 {packets:8}        probe,with,commas"


def frame(*rows):
    lines = [" CH  6 ][ Elapsed: 0 s", "", " BSSID PWR Beacons ESSID",
             f" {AP} -30 20 Example", "", HEADER, "", *rows]
    return ("\x1b[2;1H\x1b[22m"
            + "".join(line + "\x1b[0K\r\n" for line in lines) + "\x1b[0J")


class ParserTests(unittest.TestCase):
    def test_fragmented_escapes_utf8_redraws_filtering_and_empty_frames(self):
        display = tool.Display(AP.lower())
        text = frame(
            station(mac=MAC.lower(), bssid=AP.lower(), power=-1),
            station(bssid="02:00:00:00:00:01"),
            " (not associated)  12:00:00:00:00:02 -20 1-1 0 2",
        ) + frame()
        snapshots = []
        for char in text:
            snapshots.extend(display.feed(char))
        self.assertEqual(snapshots, [[tool.Client(MAC, None, 10)], []])

    def test_duplicate_stations_and_rate_variants(self):
        for rate in ("6e- 1", "54 -54e", "0 - 0"):
            text = frame(station().replace("6e- 1", rate),
                         station(mac=MAC.lower(), packets=20))
            self.assertEqual(tool.Display(AP).feed(text), [[tool.Client(MAC, -45, 20)]])

    def test_no_partial_or_incompatible_snapshot_is_accepted(self):
        display = tool.Display(AP)
        text = frame(station())
        self.assertEqual(display.feed(text[:-1]), [])
        self.assertEqual(display.feed(text[-1:]), [[tool.Client(MAC, -45, 10)]])
        for text in (
            frame(station()).replace("Frames", "Changed"),
            frame(station()).replace(HEADER, " BSSID POWER"),
            frame(station(), " malformed"),
            frame(station(power=5)),
            frame(station(mac="FF:FF:FF:FF:FF:FF")),
            frame(station())[:-4] + frame(),
            frame("x" * (tool.COLUMNS + 1)),
            "\x1b[2;1H\x1b[999B\x1b[0J",
            "\x1b[2;1H\x1b[1A",
            "\x1b]unsupported\x07",
        ):
            with self.subTest(text=text[:80]), self.assertRaises((tool.SnapshotError, ValueError)):
                tool.Display(AP).feed(text)

    def test_full_display_is_reported_instead_of_silently_truncating(self):
        text = frame(*[""] * (tool.ROWS - 10))
        with self.assertRaisesRegex(tool.SnapshotError, "full|overflow"):
            tool.Display(AP).feed(text)

    def test_json_contract_has_only_observations_and_unavailable_power(self):
        output = io.StringIO()
        target = {"interface": "wlan0mon", "bssid": AP, "channel": 6}
        tool.emit({MAC: tool.Client(MAC, None, 10)}, target, output)
        data = json.loads(output.getvalue())
        self.assertEqual(data, {
            "event": "clients", "source": "airodump-ng", "target": target,
            "clients": [{"mac": MAC, "power_dbm": None, "packets": 10}]})
        self.assertNotIn("demo", data)


class InterruptOutput(io.StringIO):
    def __init__(self, count):
        super().__init__()
        self.count = count

    def flush(self):
        self.count -= 1
        if self.count == 0:
            raise KeyboardInterrupt


class CaptureTests(unittest.TestCase):
    def stub(self, directory, body):
        executable = Path(directory) / "airodump-ng"
        executable.write_text(f"#!{sys.executable}\n" + body)
        executable.chmod(0o700)
        return str(executable)

    def test_real_pty_stub_stream_retention_no_files_and_process_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.stub(directory,
                "import os, sys, time\n"
                "assert os.isatty(0) and os.isatty(1)\n"
                "assert os.get_terminal_size(0).lines == 1000\n"
                "assert '--write' not in sys.argv and '--output-format' not in sys.argv\n"
                f"frames = {[frame(station()), frame(), frame(station(packets=2, power=-60))]!r}\n"
                "for text in frames:\n"
                "    for chunk in [text[:7], text[7:]]:\n"
                "        os.write(1, chunk.encode()); time.sleep(0.01)\n"
                "time.sleep(30)\n")
            output = InterruptOutput(3)
            real_popen = subprocess.Popen
            with patch.object(tool.shutil, "which", return_value=executable), \
                    patch.object(tool.subprocess, "Popen", wraps=real_popen) as launch:
                tool.capture("wlan0mon", AP, 6, output)
            snapshots = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(snapshots), 3)
            self.assertTrue(all(data["clients"][0]["mac"] == MAC for data in snapshots))
            self.assertEqual(snapshots[-1]["clients"][0]["packets"], 10)
            self.assertEqual(snapshots[-1]["clients"][0]["power_dbm"], -60)
            self.assertEqual(list(Path(directory).iterdir()), [Path(executable)])
            argv = launch.call_args.args[0]
            self.assertEqual(argv, [executable, "--bssid", AP, "--channel", "6",
                                    "--update", "2", "--background", "0", "wlan0mon"])
            self.assertNotIn("shell", launch.call_args.kwargs)
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            for name in ("stdin", "stdout"):
                with self.assertRaises(OSError):
                    os.fstat(launch.call_args.kwargs[name])

    def test_missing_tool_and_launch_failure(self):
        with patch.object(tool.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                tool.capture("wlan0mon", AP, 6, io.StringIO())
        with patch.object(tool.shutil, "which", return_value="/stub/airodump-ng"), \
                patch.object(tool.subprocess, "Popen", side_effect=PermissionError("denied")) as launch:
            with self.assertRaisesRegex(RuntimeError, "cannot launch.*denied"):
                tool.capture("wlan0mon", AP, 6, io.StringIO())
            with self.assertRaises(OSError):
                os.fstat(launch.call_args.kwargs["stdin"])

    def test_unexpected_exit_preserves_bounded_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.stub(directory,
                "import sys\nprint('permission denied', file=sys.stderr, flush=True)\n"
                "print('cannot open adapter', flush=True)\nsys.exit(7)\n")
            with patch.object(tool.shutil, "which", return_value=executable):
                with self.assertRaisesRegex(RuntimeError, "status 7") as error:
                    tool.capture("wlan0mon", AP, 6, io.StringIO())
            self.assertIn("permission denied", str(error.exception))
            self.assertIn("capture permissions", str(error.exception))

    def test_missing_live_display_times_out_and_reaps_stub(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.stub(directory, "import time\ntime.sleep(30)\n")
            with patch.object(tool.shutil, "which", return_value=executable), \
                    patch.object(tool.time, "monotonic", side_effect=[0, 20]):
                with self.assertRaisesRegex(RuntimeError, "no complete live station display"):
                    tool.capture("wlan0mon", AP, 6, io.StringIO())

    def test_malformed_display_is_not_a_successful_empty_result(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.stub(directory,
                f"import os, time\nos.write(1, {frame('bad row').encode()!r})\ntime.sleep(30)\n")
            output = io.StringIO()
            with patch.object(tool.shutil, "which", return_value=executable):
                with self.assertRaisesRegex(RuntimeError, "malformed station row"):
                    tool.capture("wlan0mon", AP, 6, output)
            self.assertEqual(output.getvalue(), "")

    def test_bounded_signal_escalation_and_already_exited_process(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("stub", 2), subprocess.TimeoutExpired("stub", 2), 0]
        tool.stop_capture(process)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 3)
        process.reset_mock(side_effect=True)
        process.poll.return_value = 7
        tool.stop_capture(process)
        process.wait.assert_called_once_with()
        process.send_signal.assert_not_called()

    def test_repeated_sigterm_cannot_interrupt_capture_reaping(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "running"
            executable = self.stub(directory,
                "import os, signal, sys, time\nfrom pathlib import Path\n"
                f"marker = Path({str(marker)!r})\n"
                "def stop(sig, frame):\n    time.sleep(0.3)\n    sys.exit(0)\n"
                "signal.signal(signal.SIGINT, stop)\n"
                "try:\n"
                "    marker.write_text(str(os.getpid()))\n"
                f"    os.write(1, {frame(station()).encode()!r})\n"
                "    time.sleep(30)\n"
                "finally:\n"
                "    marker.unlink()\n")
            code = ("import sys; sys.platform = 'linux'; "
                    "from tools.airodump_clients import main; "
                    f"sys.exit(main({['--interface', 'wlan0mon', '--bssid', AP, '--channel', '6']!r}))")
            env = {**os.environ, "PATH": directory + os.pathsep + os.environ.get("PATH", "")}
            with subprocess.Popen([sys.executable, "-u", "-c", code], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=5), "scanner produced no output")
                    self.assertEqual(json.loads(process.stdout.readline())["source"], "airodump-ng")
                    child = int(marker.read_text())
                    process.send_signal(signal.SIGTERM)
                    time.sleep(0.1)
                    process.send_signal(signal.SIGTERM)
                    _, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, stderr.decode())
                    self.assertFalse(marker.exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child, 0)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)


class CliTests(unittest.TestCase):
    def test_required_invalid_and_linux_preconditions(self):
        valid = ["--interface", "wlan0mon", "--bssid", AP, "--channel", "6"]
        for args in ([], [*valid[:-1], "0"], [*valid[:-1], "234"], [*valid[:-1], "six"],
                     ["--interface", "wlan0;touch x", *valid[2:]],
                     [*valid[:3], "FF:FF:FF:FF:FF:FF", *valid[4:]]):
            with self.subTest(args=args), patch.object(tool, "capture") as capture, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                tool.main(args)
            capture.assert_not_called()
        with patch.object(tool.sys, "platform", "darwin"), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(tool.main(valid), 1)
            self.assertIn("requires Linux", stderr.getvalue())

    def test_sigterm_cleanup_and_error_status(self):
        args = ["--interface", "wlan0mon", "--bssid", AP.lower(), "--channel", "6"]
        previous = signal.getsignal(signal.SIGTERM)

        def capture(interface, bssid, channel, output):
            self.assertEqual((interface, bssid, channel), ("wlan0mon", AP, 6))
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        with patch.object(tool.sys, "platform", "linux"), \
                patch.object(tool, "capture", side_effect=capture):
            self.assertEqual(tool.main(args), 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        with patch.object(tool.sys, "platform", "linux"), \
                patch.object(tool, "capture", side_effect=RuntimeError("capture failed")), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(tool.main(args), 1)
            self.assertIn("capture failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
