from dataclasses import replace
import errno
import json
import os
from pathlib import Path, PurePosixPath
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

import pygame

from apps import backup_files as tool
from apps import _removable_storage as storage
from deckfw import Cancelled, Context
from deckfw.context import AppSpec, RunResult
from deckfw.screens import AppScreen, Navigator, Screen
from deckfw.theme import Theme
from test_storage_tools import StorageFixture


READ_MOUNTS = tool.mount_records


class FormTests(StorageFixture):
    def test_drive_partition_choices_and_changes(self):
        targets = storage.discover()
        second = replace(targets[0], disk="sdb", name="sdb", diskseq=124)
        other_part = replace(targets[1], disk="sdb", name="sdb1", diskseq=124)
        form = tool.BackupForm([*targets, second, other_part])
        values = form.defaults()
        self.assertEqual(form.submit_label, "Browse")
        self.assertEqual(storage.token_target(values["partition"]).name, "sda1")
        values["target"] = second.token()
        form.changed(values, "target")
        self.assertEqual(values["partition"], other_part.token())
        self.assertEqual(form.validate(values), values)
        values["partition"] = targets[1].token()
        with self.assertRaises(ValueError):
            form.validate(values)

    def test_no_drives_partitions_and_system_disk(self):
        self.assertEqual([target.disk for target in storage.discover()], ["sda", "sda"])
        self.assertEqual(len(tool.setup().fields[0].choices), 1)
        empty = tool.BackupForm([])
        with self.assertRaisesRegex(ValueError, "No eligible drives"):
            empty.validate(empty.defaults())
        no_parts = tool.BackupForm([self.whole()])
        with self.assertRaisesRegex(ValueError, "No eligible partitions"):
            no_parts.validate(no_parts.defaults())
        self.mount()
        form = tool.setup()
        with self.assertRaisesRegex(ValueError, "No eligible drives"):
            form.validate(form.defaults())

    def test_setup_does_not_mount_or_probe_filesystems(self):
        with patch.object(storage, "run_tool") as run, patch.object(storage, "fs_type") as probe:
            form = tool.setup()
            form.validate(form.defaults())
            form.changed(form.defaults(), "target")
        run.assert_not_called()
        probe.assert_not_called()

    def test_mountinfo_escaped_paths_and_invalid_records(self):
        path = self.proc / "self" / "mountinfo"
        path.write_text("1 0 8:1 / /tmp/drive\\040name ro,nodev - vfat /dev/sda1 ro\n")
        self.assertEqual(READ_MOUNTS()[0][2], "/tmp/drive name")
        for contents in ("", "bad mount record\n"):
            path.write_text(contents)
            with self.assertRaisesRegex(ValueError, "mount information"):
                READ_MOUNTS()

    def test_stale_selection_refused_before_mount(self):
        form = tool.setup()
        values = form.defaults()
        (self.disk / "diskseq").write_text("999")
        ctx = Context(AppSpec("Backup files", tool.backup_files), values)
        with patch.object(storage, "run_tool") as run:
            tool.backup_files(ctx)
        self.assertEqual(ctx.status, "error")
        run.assert_not_called()


class SourceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.backups = self.root / "Backups"
        dev = self.source.stat().st_dev
        self.number = f"{os.major(dev)}:{os.minor(dev)}"
        self.target = storage.Target("sda", "sda1", 1, self.number, self.number,
                                     10000, 2048, "USB")
        self.partition = tool.Partition(self.target)
        self.partition.path = self.source
        self.records = [("1", self.number, str(self.source), ["ro", "nodev", "nosuid", "noexec"])]
        for item in (patch.object(tool, "mount_records", side_effect=lambda: self.records),
                     patch.object(storage, "token_target", side_effect=lambda token:
                                  storage.Target(*json.loads(token))),
                     patch.object(storage, "backup_dir", return_value=self.backups),
                     patch.object(storage, "drive_label", return_value="Test_Drive"),
                     patch.object(storage, "timestamp", return_value="20261112-123456")):
            item.start()
            self.addCleanup(item.stop)
        self.partition.verify(force=True)
        self.ctx = Context(AppSpec("Backup files", tool.backup_files))

    def write(self, name, data=b"contents"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class CopyTests(SourceFixture):
    def test_multi_folder_backup_paths_hidden_empty_metadata_and_progress(self):
        self.write("Documents/sub/report.txt", b"report" * 100)
        self.write("Documents/.hidden", b"")
        (self.source / "Documents/empty").mkdir()
        image = self.write("Pictures/image.bin", b"image" * 200)
        os.chmod(image, 0o640)
        os.utime(image, ns=(1234560000000000, 1234560000000000))
        with patch.object(self.ctx, "progress", wraps=self.ctx.progress) as progress:
            output = tool.copy_selected(self.ctx, self.partition,
                                        ["Documents", "Documents/sub", "Pictures/image.bin"])
        self.assertEqual(output.name, "Test_Drive-20261112-123456")
        self.assertEqual((output / "Documents/sub/report.txt").read_bytes(), b"report" * 100)
        self.assertEqual((output / "Pictures/image.bin").read_bytes(), image.read_bytes())
        self.assertTrue((output / "Documents/empty").is_dir())
        self.assertEqual((output / "Documents/.hidden").stat().st_size, 0)
        self.assertEqual((output / "Pictures/image.bin").stat().st_mtime_ns, image.stat().st_mtime_ns)
        self.assertEqual((output / "Pictures/image.bin").stat().st_mode & 0o777, 0o640)
        self.assertIsNone(self.partition.partial)
        labels = [call.args[1] for call in progress.call_args_list if len(call.args) > 1]
        self.assertIn("Pictures/image.bin", labels)
        self.assertTrue(any("1600 / 1600 bytes" in call.kwargs.get("text", "")
                            for call in progress.call_args_list))
        self.assertEqual(progress.call_args.args[0], 1)

    def test_empty_files_folders_and_unique_destinations(self):
        (self.source / "empty").mkdir()
        self.write("zero", b"")
        first = tool.copy_selected(self.ctx, self.partition, ["empty", "zero"])
        second = tool.copy_selected(self.ctx, self.partition, ["empty", "zero"])
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, first.name + "-1")
        self.assertTrue((second / "empty").is_dir())

    def test_symlinks_preserved_without_following_and_special_files_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "not-backed-up").write_text("private")
        (self.source / "link").symlink_to(outside, target_is_directory=True)
        output = tool.copy_selected(self.ctx, self.partition, ["link"])
        self.assertTrue((output / "link").is_symlink())
        self.assertEqual(os.readlink(output / "link"), str(outside))
        with self.assertRaises(OSError):
            self.partition.open_directory("link")
        os.mkfifo(self.source / "pipe")
        with self.assertRaisesRegex(ValueError, "Unsupported special"):
            tool.copy_selected(self.ctx, self.partition, ["pipe"])

    def test_normalize_overlap_and_refuse_escape(self):
        self.assertEqual(tool.normalize(["a/b", "a", "c/file"]),
                         (PurePosixPath("a"), PurePosixPath("c/file")))
        for paths in (["../escape"], ["/escape"], ["."]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                tool.normalize(paths)
        with self.assertRaises(ValueError):
            self.partition.open_directory("../escape")
        with self.assertRaisesRegex(ValueError, "Select at least"):
            tool.copy_selected(self.ctx, self.partition, [])

    def test_low_space_prevents_backup_creation(self):
        self.write("file")
        with patch.object(tool.shutil, "disk_usage",
                          return_value=type("Usage", (), {"free": 1})()):
            with self.assertRaisesRegex(ValueError, "Not enough free"):
                tool.copy_selected(self.ctx, self.partition, ["file"])
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_cancel_retains_partial_and_closes_handles(self):
        self.write("file", b"x" * 300)
        original = self.ctx.progress

        def cancel_on_copy(value, label=None, text=None):
            original(value, label, text)
            if label == "file":
                self.ctx.cancel()

        with patch.object(self.ctx, "progress", side_effect=cancel_on_copy):
            with self.assertRaises(Cancelled):
                tool.copy_selected(self.ctx, self.partition, ["file"])
        self.assertTrue(self.partition.partial.is_dir())
        self.assertTrue(self.partition.partial.name.endswith(".partial"))
        self.assertFalse(any(path.name.endswith("123456") for path in self.backups.iterdir()))
        (self.partition.partial / "file").unlink()

    def test_metadata_failures_retain_partial(self):
        self.write("file")
        for error in (OSError("metadata failed"), OSError(errno.ENOSPC, "disk full")):
            with self.subTest(error=error):
                with patch.object(tool, "metadata", side_effect=error):
                    with self.assertRaises(OSError):
                        tool.copy_selected(self.ctx, self.partition, ["file"])
                self.assertTrue(self.partition.partial.is_dir())
                self.assertTrue((self.partition.partial / "file").exists())

    def test_disk_full_during_write_retains_partial(self):
        self.write("file")
        real_open = open

        class FullFile:
            def __init__(self, path, mode):
                self.file = real_open(path, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def write(self, data):
                raise OSError(errno.ENOSPC, "disk full")

        with patch.object(tool, "open", side_effect=FullFile, create=True):
            with self.assertRaisesRegex(OSError, "disk full"):
                tool.copy_selected(self.ctx, self.partition, ["file"])
        self.assertTrue(self.partition.partial.is_dir())
        self.assertEqual((self.partition.partial / "file").stat().st_size, 0)

    def test_source_disappearing_during_copy_and_missing_paths_fail(self):
        self.write("file")

        def detach(value, label=None, text=None):
            if label == "file":
                self.records.clear()
                self.partition.last_check = 0

        with patch.object(self.ctx, "progress", side_effect=detach):
            with self.assertRaisesRegex(ValueError, "no longer mounted"):
                tool.copy_selected(self.ctx, self.partition, ["file"])
        self.assertTrue(self.partition.partial.is_dir())
        self.records.append(("1", self.number, str(self.source), ["ro"]))
        with self.assertRaises(FileNotFoundError):
            tool.copy_selected(self.ctx, self.partition, ["missing"])

    def test_nested_mount_device_replacement_and_non_readonly_refused(self):
        self.write("folder/file")
        self.records.append(("2", self.number, str(self.source / "folder"), ["ro"]))
        self.partition.verify(force=True)
        with self.assertRaisesRegex(ValueError, "nested mount"):
            tool.copy_selected(self.ctx, self.partition, ["folder"])
        self.records[0] = ("3", self.number, str(self.source), ["ro"])
        with self.assertRaisesRegex(ValueError, "mount changed"):
            self.partition.verify(force=True)
        self.records[0] = ("1", self.number, str(self.source), ["rw"])
        with self.assertRaisesRegex(ValueError, "not read-only"):
            self.partition.verify(force=True)
        self.records.clear()
        with self.assertRaisesRegex(ValueError, "no longer mounted"):
            self.partition.verify(force=True)


class LifecycleTests(SourceFixture):
    def setUp(self):
        super().setUp()
        self.records.clear()
        self.partition.path = None
        self.partition.mount_id = None
        self.tools = {name: name for name in ("mount", "umount", "blkid", "timeout")}
        for item in (patch.object(storage, "require_tools", return_value=self.tools),
                     patch.object(storage, "check_safe", return_value=self.target),
                     patch.object(self.ctx, "run", side_effect=self.run_command)):
            item.start()
            self.addCleanup(item.stop)
        self.commands = []
        self.filesystem = "ext4"
        self.fail_unmount = False

    def run_command(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == "blkid":
            return RunResult(0, [self.filesystem])
        if args[0] == "mount":
            self.records.append(("10", self.number, args[-1], ["ro"]))
        elif "umount" in args:
            if self.fail_unmount:
                return RunResult(1, ["busy"])
            self.records.clear()
        return RunResult(0, [])

    def test_mount_options_and_unmount_cleanup(self):
        for filesystem, extra in (("ext4", "noload"), ("vfat", ""),
                                  ("ntfs", "norecover"), ("exfat", "")):
            with self.subTest(filesystem=filesystem):
                self.filesystem = filesystem
                partition = tool.Partition(self.target)
                partition.mount(self.ctx)
                path = partition.path
                command = next(command for command in reversed(self.commands) if command[0] == "mount")
                options = command[command.index("-o") + 1]
                for option in ("ro", "nodev", "nosuid", "noexec"):
                    self.assertIn(option, options.split(","))
                if extra:
                    self.assertIn(extra, options.split(","))
                partition.close(self.ctx)
                self.assertFalse(path.exists())

    def test_unknown_filesystem_refused_before_mount(self):
        self.filesystem = "btrfs"
        with self.assertRaisesRegex(ValueError, "not supported"):
            self.partition.mount(self.ctx)
        self.assertFalse(any(command[0] == "mount" for command in self.commands))

    def test_cancelled_mount_is_still_found_and_cleaned(self):
        normal_run = self.run_command

        def cancel_mount(args, **kwargs):
            result = normal_run(args, **kwargs)
            if args[0] == "mount":
                self.ctx.cancel()
                raise Cancelled()
            return result

        self.ctx.file_backup = self.partition
        with patch.object(self.ctx, "run", side_effect=cancel_mount):
            with self.assertRaises(Cancelled):
                self.partition.mount(self.ctx)
        path = self.partition.path
        self.ctx._shutting_down = True
        tool.shutdown(self.ctx)
        self.assertFalse(path.exists())
        self.assertEqual(self.records, [])

    def test_failed_mount_with_or_without_mount_record(self):
        normal_run = self.run_command
        for mounted in (False, True):
            partition = tool.Partition(self.target)

            def fail(args, **kwargs):
                result = normal_run(args, **kwargs)
                if args[0] == "mount":
                    if not mounted:
                        self.records.clear()
                    return RunResult(1, ["mount failure"])
                return result

            with patch.object(self.ctx, "run", side_effect=fail):
                with self.assertRaisesRegex(ValueError, "mount failed"):
                    partition.mount(self.ctx)
            path = partition.path
            partition.close(self.ctx)
            self.assertFalse(path.exists())

    def test_unmount_failure_retains_owned_path_and_foreign_mount_not_touched(self):
        self.partition.mount(self.ctx)
        self.ctx.file_backup = self.partition
        self.fail_unmount = True
        path = self.partition.path
        with self.assertRaisesRegex(ValueError, "Cannot unmount"):
            tool.shutdown(self.ctx)
        self.assertTrue(path.is_dir())
        self.records[0] = ("11", self.number, str(path), ["ro"])
        count = len(self.commands)
        with self.assertRaisesRegex(ValueError, "foreign mount"):
            tool.shutdown(self.ctx)
        self.assertEqual(len(self.commands), count)
        self.records.clear()
        path.rmdir()

    def test_appscreen_runs_shutdown_on_success_error_and_cancel(self):
        drive = replace(self.target, name="sda", start=0)
        for outcome in ("success", "error", "cancel"):
            with self.subTest(outcome=outcome):
                screen_partition = tool.Partition(self.target)
                spec = AppSpec("Backup files", tool.backup_files, shutdown=tool.shutdown)
                with patch.object(tool, "Partition", return_value=screen_partition), \
                        patch.object(tool, "choose_paths", return_value=None if outcome == "success"
                                     else (PurePosixPath("file"),)), \
                        patch.object(tool, "copy_selected",
                                     side_effect=ValueError("copy failed") if outcome == "error"
                                     else Cancelled()), \
                        patch.object(Context, "run", side_effect=self.run_command):
                    screen = AppScreen(spec, {"target": drive.token(),
                                              "partition": self.target.token()})
                    screen._thread.join(timeout=3)
                self.assertFalse(screen.alive)
                self.assertEqual(screen.ctx.status,
                                 {"success": "done", "error": "error", "cancel": "cancelled"}[outcome])
                self.assertIsNone(screen_partition.path)
                self.assertFalse(self.records)

    def test_worker_browses_copies_persistent_files_and_unmounts(self):
        browser = tool.Browser()
        ready = queue.Queue()
        observed = 0
        normal_run = self.run_command
        normal_sleep = Context.sleep

        def commands(args, **kwargs):
            if "umount" in args:
                mounted = Path(args[-1])
                (mounted / "Documents/notes.txt").unlink()
                (mounted / "Documents").rmdir()
            result = normal_run(args, **kwargs)
            if args[0] == "mount":
                mounted = Path(args[-1])
                (mounted / "Documents").mkdir()
                (mounted / "Documents/notes.txt").write_text("selected content")
            return result

        def observe(ctx, seconds):
            nonlocal observed
            if browser.view.generation != observed:
                observed = browser.view.generation
                ready.put(browser.view)
            normal_sleep(ctx, seconds)

        drive = replace(self.target, name="sda", start=0)
        spec = AppSpec("Backup files", tool.backup_files, shutdown=tool.shutdown)
        with patch.object(Context, "run", side_effect=commands), \
                patch.object(Context, "sleep", observe), \
                patch.object(tool, "Browser", return_value=browser):
            screen = AppScreen(spec, {"target": drive.token(), "partition": self.target.token()})
            try:
                view = ready.get(timeout=2)
                browser.requests.put((view.generation, "toggle", PurePosixPath("Documents")))
                view = ready.get(timeout=2)
                browser.requests.put((view.generation, "backup", None))
                screen._thread.join(timeout=3)
                self.assertFalse(screen.alive)
                self.assertEqual(screen.ctx.status, "done")
                output = self.backups / "Test_Drive-20261112-123456/Documents/notes.txt"
                self.assertEqual(output.read_text(), "selected content")
                self.assertFalse(self.records)
                self.assertIsNone(screen.ctx.file_backup.path)
                self.assertFalse(list(self.backups.glob("*.partial")))
            finally:
                screen.close()
                screen._thread.join(timeout=3)


class BrowserTests(SourceFixture):
    def test_browser_empty_selection_deselection_and_root_exit(self):
        self.write("file")
        browser = tool.Browser()
        actions = ["backup", "toggle", "toggle", "back"]

        def act(seconds):
            action = actions.pop(0)
            target = PurePosixPath("file") if action == "toggle" else None
            if action == "toggle" and len(actions) == 1:
                self.assertIn(PurePosixPath("file"), browser.view.selected)
            if action == "back":
                self.assertFalse(browser.view.selected)
            browser.requests.put((browser.view.generation, action, target))

        with patch.object(self.ctx, "sleep", side_effect=act):
            self.assertIsNone(tool.choose_paths(self.ctx, self.partition, browser))
        self.assertFalse(actions)

    def test_browser_navigation_multi_selection_and_stale_actions(self):
        self.write("first/a")
        self.write("second/b")
        browser = tool.Browser()
        result = []
        ready = queue.Queue()

        original_sleep = self.ctx.sleep
        generation = 0

        def observe(seconds):
            nonlocal generation
            if browser.view.generation != generation:
                generation = browser.view.generation
                ready.put(browser.view)
            original_sleep(seconds)

        def work():
            try:
                result.append(tool.choose_paths(self.ctx, self.partition, browser))
            except BaseException as exc:
                result.append(exc)

        with patch.object(self.ctx, "sleep", side_effect=observe):
            thread = threading.Thread(target=work)
            thread.start()
            try:
                view = ready.get(timeout=2)
                browser.requests.put((view.generation - 1, "back", None))
                browser.requests.put((view.generation, "open", PurePosixPath("first")))
                self.assertEqual(ready.get(timeout=2).path, PurePosixPath("first"))
                browser.action("down")
                browser.action("right")
                view = ready.get(timeout=2)
                self.assertEqual(view.selected, {PurePosixPath("first/a")})
                browser.action("b")
                ready.get(timeout=2)
                browser.requests.put((browser.view.generation, "toggle", PurePosixPath("second")))
                view = ready.get(timeout=2)
                self.assertEqual(len(view.selected), 2)
                browser.list.selected = 0
                browser.action("a")
                thread.join(timeout=2)
                self.assertEqual(result, [(PurePosixPath("first/a"), PurePosixPath("second"))])
            finally:
                self.ctx.cancel()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_browser_b_handled_locally_then_cancel_during_copy(self):
        browser = tool.Browser()
        ctx = self.ctx
        ctx.add(browser)
        screen = object.__new__(AppScreen)
        screen.ctx = ctx
        screen.spec = ctx.spec
        screen._thread = threading.Thread()
        nav = Navigator()
        nav.push(Screen())
        nav.push(screen)
        screen.action("b")
        self.assertFalse(ctx.cancelled)
        self.assertEqual(browser.requests.get_nowait()[1], "back")
        browser.view = tool.View(active=False)
        screen.action("b")
        self.assertEqual(len(nav.stack), 1)

    def test_widget_selection_deselection_and_layout_at_both_sizes(self):
        pygame.font.init()
        browser = tool.Browser()
        entries = tuple(tool.Entry(PurePosixPath(f"long-folder-name-{i:02}"), True)
                        for i in range(25))
        browser.view = tool.View(1, PurePosixPath("long-parent-folder"),
                                 entries, frozenset({entries[0].path}))
        for width, height in ((320, 240), (640, 480)):
            theme = Theme(width, height)
            surface = pygame.Surface((width, height))
            body = pygame.Rect(4, 45, width - 8, height - 90)
            browser.draw(surface, body, theme)
            browser.list.selected = 0
            browser.action("down")
            browser.action("x")
            self.assertEqual(browser.requests.get_nowait()[1], "toggle")
            browser.list.selected = len(entries)
            browser.draw(surface, body, theme)
            self.assertGreater(browser.list._top, 0)


if __name__ == "__main__":
    unittest.main()
