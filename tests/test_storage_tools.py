import gzip
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from deckfw import Cancelled, Context
from deckfw.context import AppSpec, RunResult
from deckfw.widgets import Graph
from apps import _removable_storage as storage
from apps import backup_drive, fake_capacity, filesystem_check, restore_drive, secure_wipe
from apps import speed_test

SECTORS = 16384  # 8 MiB synthetic drive


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.sys, self.dev = root / "sys", root / "dev"
        self.proc, self.sys_dev = root / "proc", root / "dev-block"
        self.backups = root / "home" / "Backups"
        for path in (self.sys, self.dev, self.proc / "self", self.sys_dev):
            path.mkdir(parents=True)
        system = self.sys / "vda"
        system.mkdir()
        self.write_device(system, "8:98", SECTORS)
        (system / "device").mkdir()
        (system / "slaves").mkdir()
        (system / "diskseq").write_text("1")
        system_part = system / "vda1"
        system_part.mkdir()
        (system_part / "dev").write_text("8:99")
        (system_part / "partition").write_text("1")
        (self.sys_dev / "8:98").symlink_to(system)
        (self.sys_dev / "8:99").symlink_to(system_part)
        self.root_mount = "1 0 8:99 / / rw - ext4 /dev/other rw\n"
        (self.proc / "self" / "mountinfo").write_text(self.root_mount)
        (self.proc / "swaps").write_text("Filename Type Size Used Priority\n")
        usb = root / "usb1" / "device"
        usb.mkdir(parents=True)
        (usb / "vendor").write_text("SanDisk \n")
        (usb / "model").write_text("Ultra Fit\n")
        node = os.stat("/dev/null").st_rdev
        self.number = f"{os.major(node)}:{os.minor(node)}"
        self.disk = self.sys / "sda"
        self.disk.mkdir()
        (self.disk / "device").symlink_to(usb)
        self.write_device(self.disk, self.number, SECTORS)
        (self.disk / "diskseq").write_text("123")
        part = self.sys / "sda1"
        part.mkdir()
        self.write_device(part, self.number, SECTORS - 2048)
        (part / "start").write_text("2048")
        (part / "partition").write_text("1")
        (self.disk / "sda1").symlink_to(part)
        for name in ("sda", "sda1"):
            (self.dev / name).symlink_to("/dev/null")
        self.image = root / "device.bin"
        self.image.write_bytes(bytes(range(256)) * (SECTORS * 2))
        patches = [
            patch.object(storage, "SYS_BLOCK", self.sys),
            patch.object(storage, "SYS_DEV_BLOCK", self.sys_dev),
            patch.object(storage, "DEV", self.dev),
            patch.object(storage, "PROC", self.proc),
            patch.object(storage.sys, "platform", "linux"),
            patch.object(storage.stat, "S_ISBLK", return_value=True),
            patch.object(storage, "backup_dir", return_value=self.backups),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    @staticmethod
    def write_device(path, number, size):
        (path / "dev").write_text(number)
        (path / "size").write_text(str(size))
        (path / "ro").write_text("0")
        (path / "holders").mkdir()

    def fake_open(self, target, write=False, direct=False):
        storage.check_safe(target, write=write)
        return os.open(self.image, os.O_RDWR)

    def ctx(self, **params):
        return Context(AppSpec("tool", lambda ctx: None), params)

    def whole(self):
        return storage._target("sda", "sda")

    def mount(self):
        (self.proc / "self" / "mountinfo").write_text(
            self.root_mount + f"2 1 {self.number} / /mnt rw - vfat /dev/sda1 rw\n")


class SharedTests(StorageFixture):
    def test_root_and_mounted_drives_excluded_and_empty_forms(self):
        self.assertEqual([t.name for t in storage.discover(partitions=False)], ["sda"])
        self.mount()
        self.assertEqual(storage.discover(), [])
        for module in (backup_drive, restore_drive, secure_wipe, speed_test, fake_capacity,
                       filesystem_check):
            with self.subTest(module=module.__name__):
                form = module.setup()
                values = form.defaults()
                self.assertEqual(values["target"], "")
                with self.assertRaisesRegex(ValueError, "No eligible drives"):
                    form.validate(values)

    def test_drive_label_is_file_name_safe(self):
        self.assertEqual(storage.drive_label("sda"), "SanDisk_Ultra_Fit")
        self.assertEqual(storage.drive_label("missing"), "missing")


class BackupRestoreTests(StorageFixture):
    def test_raw_backup_name_content_and_sidecar(self):
        with patch.object(storage, "open_device", self.fake_open):
            path = backup_drive.backup(self.ctx(), self.whole(), "raw")
        self.assertRegex(path.name, r"^SanDisk_Ultra_Fit-\d{8}-\d{6}\.img$")
        self.assertEqual(path.parent, self.backups)
        self.assertEqual(path.read_bytes(), self.image.read_bytes())
        meta = json.loads(path.with_name(path.name + ".json").read_text())
        self.assertEqual(meta["size"], SECTORS * 512)
        self.assertEqual(list(self.backups.glob("*.partial")), [])

    def test_gzip_backup_restores_to_identical_bytes(self):
        with patch.object(storage, "open_device", self.fake_open):
            path = backup_drive.backup(self.ctx(), self.whole(), "gzip")
        self.assertTrue(path.name.endswith(".img.gz"))
        original = self.image.read_bytes()
        self.assertEqual(gzip.decompress(path.read_bytes()), original)
        self.image.write_bytes(bytes(len(original)))
        self.assertEqual(restore_drive.image_size(path), len(original))
        with patch.object(storage, "open_device", self.fake_open), \
                patch.object(restore_drive.shutil, "which", return_value=None):
            restore_drive.restore(self.ctx(), path, self.whole())
        self.assertEqual(self.image.read_bytes(), original)

    def test_backup_refuses_mounted_and_low_space_and_cleans_partial(self):
        self.mount()
        ctx = self.ctx(target=self.whole().token(), format="raw")
        backup_drive.backup_drive(ctx)
        self.assertEqual(ctx.status, "error")
        self.assertFalse(self.backups.exists() and any(self.backups.iterdir()))
        (self.proc / "self" / "mountinfo").write_text(self.root_mount)
        with patch.object(backup_drive.shutil, "disk_usage",
                          return_value=type("Usage", (), {"free": 10})()):
            with self.assertRaisesRegex(ValueError, "Not enough free space"):
                backup_drive.backup(self.ctx(), self.whole(), "raw")
        ctx = self.ctx()
        with patch.object(storage, "open_device", self.fake_open), \
                patch.object(ctx, "check", side_effect=Cancelled):
            with self.assertRaises(Cancelled):
                backup_drive.backup(ctx, self.whole(), "gzip")
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_restore_lists_images_and_guards_size_name_and_confirmation(self):
        self.backups.mkdir(parents=True)
        old = self.backups / "old.img"
        old.write_bytes(b"x" * 10)
        os.utime(old, (1, 1))
        new = self.backups / "new.img.gz"
        new.write_bytes(gzip.compress(b"y"))
        (self.backups / "notes.txt").write_text("skip")
        self.assertEqual([p.name for p in restore_drive.list_images()], ["new.img.gz", "old.img"])
        self.assertIsNone(restore_drive.image_size(new))
        self.assertEqual(restore_drive.image_size(old), 10)
        form = restore_drive.setup()
        values = form.defaults()
        with self.assertRaisesRegex(ValueError, "overwritten"):
            form.validate(values)
        for name in ("../x.img", "notes.txt", "missing.img"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                restore_drive.resolve_image(name)
        big = self.backups / "big.img"
        big.write_bytes(b"")
        (self.backups / "big.img.json").write_text(json.dumps({"size": SECTORS * 1024}))
        with patch.object(storage, "open_device") as opened:
            with self.assertRaisesRegex(ValueError, "Image needs"):
                restore_drive.restore(self.ctx(), big, self.whole())
            opened.assert_not_called()
        ctx = self.ctx(image="old.img", target=self.whole().token(), confirm=False)
        with patch.object(storage, "open_device") as opened:
            restore_drive.restore_drive(ctx)
            opened.assert_not_called()
        self.assertEqual(ctx.status, "error")

    def test_restore_stops_when_unknown_size_overflows_drive(self):
        self.backups.mkdir(parents=True)
        path = self.backups / "huge.img.gz"
        path.write_bytes(gzip.compress(bytes(SECTORS * 512 + 1)))
        with patch.object(storage, "open_device", self.fake_open):
            with self.assertRaisesRegex(ValueError, "larger than"):
                restore_drive.restore(self.ctx(), path, self.whole())


class FilesystemCheckTests(StorageFixture):
    def test_discovery_conditional_confirm_and_commands(self):
        with patch.object(storage, "fs_type", return_value="vfat"):
            form = filesystem_check.setup()
        values = form.defaults()
        self.assertNotIn("confirm", [f.key for f in form.visible_fields(values)])
        self.assertEqual(form.validate(values)["mode"], "check")
        values["mode"] = "repair"
        form.changed(values, "mode")
        with self.assertRaisesRegex(ValueError, "Confirm"):
            form.validate(values)
        target = storage._target("sda", "sda1")
        for kind, mode, expected in (("vfat", "check", ["fsck.fat", "-n"]),
                                     ("ext4", "repair", ["e2fsck", "-f", "-y"]),
                                     ("ntfs", "check", ["ntfsfix", "-n"])):
            ctx = self.ctx()
            with self.subTest(kind=kind), \
                    patch.object(storage.shutil, "which", side_effect=lambda n: n), \
                    patch.object(ctx, "run", return_value=RunResult(0, [])) as run:
                self.assertEqual(filesystem_check.check(ctx, target, kind, mode),
                                 "Filesystem is clean.")
                self.assertEqual(run.call_args.args[0],
                                 [*expected, str(self.dev / "sda1")])

    def test_mounted_partition_refused_and_verdicts(self):
        self.mount()
        with patch.object(storage.shutil, "which", side_effect=lambda n: n), \
                self.assertRaisesRegex(ValueError, "mounted"):
            filesystem_check.check(self.ctx(), storage._target("sda", "sda1"), "ext4", "check")
        self.assertEqual(filesystem_check.verdict("e2fsck", "repair", 1)[1], "ok")
        self.assertEqual(filesystem_check.verdict("e2fsck", "repair", 4)[1], "crit")
        self.assertEqual(filesystem_check.verdict("e2fsck", "check", 4)[1], "crit")
        self.assertEqual(filesystem_check.verdict("fsck.fat", "repair", 1)[1], "ok")
        self.assertEqual(filesystem_check.verdict("ntfsfix", "check", 1)[1], "crit")


class WipeTests(StorageFixture):
    def test_scramble_offsets_cover_every_stretch(self):
        size = 1024 * storage.MIB
        offsets = secure_wipe.scramble_offsets(size, random.Random(1))
        self.assertEqual(len(offsets), (size - 2 * secure_wipe.SCRAMBLE_EDGE) // secure_wipe.STRETCH)
        for offset in offsets:
            self.assertEqual(offset % 4096, 0)
            stretch = (offset - secure_wipe.SCRAMBLE_EDGE) // secure_wipe.STRETCH
            start = secure_wipe.SCRAMBLE_EDGE + stretch * secure_wipe.STRETCH
            self.assertLessEqual(offset + storage.MIB, start + secure_wipe.STRETCH)
        self.assertEqual(secure_wipe.scramble_offsets(8 * storage.MIB), [])

    def test_quick_and_zero_wipe(self):
        for mode in ("quick", "scramble", "zero"):
            self.image.write_bytes(b"\xff" * SECTORS * 512)
            ctx = self.ctx()
            with self.subTest(mode=mode), \
                    patch.object(storage, "open_device", self.fake_open), \
                    patch.object(storage.shutil, "which", side_effect=lambda n: "/t/" + n), \
                    patch.object(ctx, "run", return_value=RunResult(0, [])) as run:
                secure_wipe.wipe(ctx, self.whole(), mode)
                self.assertEqual([c.args[0][-1] for c in run.call_args_list],
                                 [str(self.dev / "sda1"), str(self.dev / "sda")])
                data = self.image.read_bytes()
                if mode == "quick":
                    self.assertEqual(data[:storage.MIB], bytes(storage.MIB))
                    self.assertEqual(data[-storage.MIB:], bytes(storage.MIB))
                    self.assertIn(b"\xff", data)
                elif mode == "zero":
                    self.assertEqual(data, bytes(len(data)))
                else:
                    self.assertNotIn(b"\xff" * 4096, data)

    def test_wipe_requires_confirmation(self):
        ctx = self.ctx(target=self.whole().token(), mode="zero", confirm=False)
        with patch.object(storage, "open_device") as opened:
            secure_wipe.secure_wipe(ctx)
            opened.assert_not_called()
        self.assertEqual(ctx.status, "error")


class SpeedTests(StorageFixture):
    def test_write_size(self):
        mib = storage.MIB
        self.assertEqual(speed_test.write_size(0), 0)
        self.assertEqual(speed_test.write_size(31 * mib), 0)
        self.assertEqual(speed_test.write_size(100 * mib), 84 * mib)
        self.assertEqual(speed_test.write_size(10 ** 12), 256 * mib)

    def test_read_graph(self):
        graph = Graph()
        with patch.object(storage, "open_device", self.fake_open):
            self.assertGreater(speed_test.read_test(self.ctx(), self.whole(), graph), 0)
        self.assertEqual(len(graph.series[0]), 2)

    def test_write_skips_without_space_and_always_unmounts(self):
        mount_dir = Path(self.tmp.name) / "mnt"
        mount_dir.mkdir()
        free = {"value": 0}

        class Stat:
            f_frsize = 1

            @property
            def f_bavail(self):
                return free["value"]

        ctx = self.ctx()
        with patch.object(storage, "fs_type", return_value="vfat"), \
                patch.object(storage.shutil, "which", side_effect=lambda n: "/t/" + n), \
                patch.object(speed_test.tempfile, "mkdtemp", return_value=str(mount_dir)), \
                patch.object(speed_test.os, "statvfs", return_value=Stat()), \
                patch.object(ctx, "run", return_value=RunResult(0, [])) as run:
            self.assertIsNone(speed_test.write_test(ctx, self.whole(), Graph()))
            self.assertEqual([Path(c.args[0][0]).name for c in run.call_args_list],
                             ["mount", "umount"])
            self.assertFalse(mount_dir.exists())
            mount_dir.mkdir()
            run.reset_mock()
            free["value"] = 64 * storage.MIB
            with patch.object(speed_test, "_measure", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    speed_test.write_test(ctx, self.whole(), Graph())
            self.assertEqual([Path(c.args[0][0]).name for c in run.call_args_list],
                             ["mount", "umount"])
            self.assertFalse(mount_dir.exists())


class FakeCapacityTests(StorageFixture):
    def test_verdicts_and_missing_tool(self):
        self.assertEqual(fake_capacity.verdict(
            ["Good news: The device `/dev/sda' is the real thing"], 0)[1], "ok")
        message, color = fake_capacity.verdict([
            "Bad news: The device `/dev/sda' is a counterfeit of type limbo",
            "*Usable* size: 7.48 GB (15687680 blocks)",
            "Announced size: 29.72 GB (62333952 blocks)"], 0)
        self.assertEqual(color, "crit")
        self.assertIn("usable 7.48 GB of 29.72 GB", message)
        ctx = self.ctx(target=self.whole().token())
        with patch.object(storage.shutil, "which", return_value=None):
            fake_capacity.fake_capacity(ctx)
        self.assertIn("apt install f3", str(ctx._log._raw))

    def test_probe_command(self):
        ctx = self.ctx(target=self.whole().token())
        with patch.object(storage.shutil, "which", return_value="/t/f3probe"), \
                patch.object(ctx, "run", return_value=RunResult(
                    0, ["Good news: The device `/dev/sda' is the real thing"])) as run:
            fake_capacity.fake_capacity(ctx)
        self.assertEqual(run.call_args.args[0], ["/t/f3probe", "--time-ops", str(self.dev / "sda")])
        self.assertNotEqual(ctx.status, "error")


if __name__ == "__main__":
    unittest.main()
