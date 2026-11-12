import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pygame

from deckfw import Cancelled, Context, context
from deckfw.config import DEFAULTS
from deckfw.context import AppSpec, RunResult
from deckfw.forms import SetupScreen
from deckfw.loader import discover
from deckfw.menu import Launch, Menu, MenuScreen, default_menu
from deckfw.screens import Navigator
from deckfw.theme import Theme
from apps import format_drive as formatter


class FormatDriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.sys = root / "sys"
        self.dev = root / "dev"
        self.proc = root / "proc"
        self.sys_dev = root / "dev-block"
        for path in (self.sys, self.dev, self.proc / "self", self.sys_dev):
            path.mkdir(parents=True)
        system = self.sys / "vda"
        system.mkdir()
        self.write_device(system, "8:98", 2097152)
        (system / "device").mkdir()
        (system / "slaves").mkdir()
        system_part = system / "vda1"
        system_part.mkdir()
        (system_part / "dev").write_text("8:99")
        (system_part / "partition").write_text("1")
        (self.sys_dev / "8:98").symlink_to(system)
        (self.sys_dev / "8:99").symlink_to(system_part)
        self.root_mount = "1 0 8:99 / / rw - ext4 /dev/other rw\n"
        (self.proc / "self" / "mountinfo").write_text(
            self.root_mount)
        (self.proc / "swaps").write_text("Filename Type Size Used Priority\n")
        usb = root / "usb1" / "device"
        usb.mkdir(parents=True)
        node = os.stat("/dev/null").st_rdev
        self.number = f"{os.major(node)}:{os.minor(node)}"
        self.disk = self.sys / "sda"
        self.disk.mkdir()
        (self.disk / "device").symlink_to(usb)
        self.write_device(self.disk, self.number, 2097152)
        (self.disk / "diskseq").write_text("123")
        self.part = self.add_partition()
        for name in ("sda", "sda1"):
            (self.dev / name).symlink_to("/dev/null")
        self.patches = [
            patch.object(formatter, "SYS_BLOCK", self.sys),
            patch.object(formatter, "SYS_DEV_BLOCK", self.sys_dev),
            patch.object(formatter, "DEV", self.dev),
            patch.object(formatter, "PROC", self.proc),
            patch.object(formatter.sys, "platform", "linux"),
            patch.object(formatter.stat, "S_ISBLK", return_value=True),
        ]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    @staticmethod
    def write_device(path, number, size):
        (path / "dev").write_text(number)
        (path / "size").write_text(str(size))
        (path / "ro").write_text("0")
        (path / "holders").mkdir()

    def add_partition(self):
        part = self.sys / "sda1"
        part.mkdir()
        self.write_device(part, self.number, 2097152 - 2048 - 33)
        (part / "start").write_text("2048")
        (part / "partition").write_text("1")
        (self.disk / "sda1").symlink_to(part)
        return part

    def test_discovery_form_conditional_and_confirmation(self):
        targets = formatter.discover()
        self.assertEqual([(t.name, t.whole) for t in targets], [("sda", True), ("sda1", False)])
        form = formatter.setup()
        values = form.defaults()
        self.assertIn("table", [field.key for field in form.visible_fields(values)])
        with self.assertRaisesRegex(ValueError, "Confirm"):
            form.validate(values)
        values["target"] = targets[1].token()
        values["confirm"] = True
        self.assertNotIn("table", form.validate(values))
        form.changed(values, "filesystem")
        self.assertFalse(values["confirm"])
        values["confirm"] = True
        form.submitted(values)
        self.assertFalse(values["confirm"])

    def test_safety_rejects_mounts_holders_and_changed_identity(self):
        target = formatter._target("sda", "sda1")
        self.assertEqual(formatter._check_safe(target), target)
        mountinfo = self.proc / "self" / "mountinfo"
        mountinfo.write_text(self.root_mount + f"2 1 {self.number} / /mnt rw - ext4 /dev/sda1 rw\n")
        with self.assertRaisesRegex(ValueError, "mounted"):
            formatter._check_safe(target)
        mountinfo.write_text(self.root_mount)
        (self.part / "holders" / "dm-0").touch()
        with self.assertRaisesRegex(ValueError, "dependent"):
            formatter._check_safe(target)
        (self.part / "holders" / "dm-0").unlink()
        (self.disk / "diskseq").write_text("124")
        with self.assertRaisesRegex(ValueError, "changed"):
            formatter._check_safe(target)

    def test_refuses_unverifiable_mounts_and_other_namespace_mounts(self):
        target = formatter._target("sda", "sda1")
        other = self.proc / "456"
        other.mkdir()
        (other / "mountinfo").write_text(
            f"2 0 {self.number} / /mnt rw - ext4 /dev/sda1 rw\n")
        with self.assertRaisesRegex(ValueError, "mounted"):
            formatter._check_safe(target)
        (other / "mountinfo").unlink()
        (self.proc / "self" / "mountinfo").unlink()
        with self.assertRaisesRegex(ValueError, "Cannot identify system storage"):
            formatter._check_safe(target)
        (self.proc / "self" / "mountinfo").touch()
        with self.assertRaisesRegex(ValueError, "Cannot identify system storage"):
            formatter._check_safe(target)

    def test_running_system_disk_and_all_partitions_are_excluded(self):
        (self.disk / "slaves").mkdir()
        (self.sys_dev / self.number).symlink_to(self.disk)
        (self.proc / "self" / "mountinfo").write_text(
            f"1 0 {self.number} / / rw - ext4 /dev/sda rw\n")
        with patch.object(formatter, "_mounted_devices", return_value=set()):
            self.assertEqual(formatter.discover(), [])
            for name in ("sda", "sda1"):
                with self.subTest(name=name), \
                        self.assertRaisesRegex(ValueError, "running system"):
                    formatter._check_safe(formatter._target("sda", name))

    def test_system_partition_is_resolved_to_parent_disk(self):
        self.assertEqual(formatter._system_disk_devices(), {"8:98"})
        (self.disk / "slaves").mkdir()
        partition = self.disk / "systempart"
        partition.mkdir()
        (partition / "dev").write_text("8:15")
        (partition / "partition").write_text("2")
        (self.sys_dev / "8:15").symlink_to(partition)
        (self.sys_dev / self.number).symlink_to(self.disk)
        (self.proc / "self" / "mountinfo").write_text(
            "1 0 8:15 / / rw - ext4 /dev/sda2 rw\n")
        with patch.object(formatter, "_mounted_devices", return_value=set()), \
                self.assertRaisesRegex(ValueError, "running system"):
            formatter._check_safe(formatter._target("sda", "sda1"))

    def test_layered_root_resolves_all_backing_disks(self):
        layer = self.sys / "dm-0"
        layer.mkdir()
        (layer / "dev").write_text("253:0")
        (layer / "slaves").mkdir()
        (layer / "slaves" / "vda1").symlink_to(self.sys / "vda" / "vda1")
        (self.disk / "slaves").mkdir()
        (layer / "slaves" / "sda").symlink_to(self.disk)
        (self.sys_dev / self.number).symlink_to(self.disk)
        (self.sys_dev / "253:0").symlink_to(layer)
        (self.proc / "self" / "mountinfo").write_text(
            "1 0 253:0 / / rw - ext4 /dev/mapper/root rw\n")
        self.assertEqual(formatter._system_disk_devices(), {"8:98", self.number})
        with patch.object(formatter, "_mounted_devices", return_value=set()):
            self.assertEqual(formatter.discover(), [])

    def test_no_eligible_drives_is_safe_empty_setup(self):
        pygame.font.init()
        (self.disk / "ro").write_text("1")
        self.assertEqual(formatter.discover(), [])
        spec = AppSpec("Format drive", formatter.format_drive, setup=formatter.setup)
        nav = Navigator()
        self.addCleanup(nav.close_all)
        menu = MenuScreen(Menu("Main", [Launch(spec)]))
        nav.push(menu)
        menu.action("a")
        screen = nav.top
        self.assertIsInstance(screen, SetupScreen)
        self.assertEqual(screen.form.fields[0].display(screen.values["target"]),
                         "No eligible drives")
        self.assertEqual(len(screen.form.fields), 1)
        for size in ((320, 240), (640, 480)):
            screen.draw(pygame.Surface(size), pygame.Rect(0, 30, size[0], size[1] - 60),
                        Theme(*size))
        with patch.object(formatter, "format_target") as format_target:
            screen.action("down")
            screen.action("a")
            self.assertIs(nav.top, screen)
            self.assertIn("No eligible drives", screen.error)
            format_target.assert_not_called()
        screen.action("b")
        self.assertIs(nav.top, menu)
        (self.disk / "ro").write_text("0")
        menu.action("a")
        self.assertEqual(len(nav.top.form.fields), 4)

    def test_external_disk_remains_available_when_system_is_removable(self):
        boot = self.sys / "mmcblk0"
        boot.mkdir()
        card = Path(self.tmp.name) / "sdcard"
        card.mkdir()
        (card / "type").write_text("SD")
        (boot / "device").symlink_to(card)
        self.write_device(boot, "179:0", 2097152)
        (boot / "slaves").mkdir()
        (boot / "diskseq").write_text("500")
        (self.sys_dev / "179:0").symlink_to(boot)
        (self.proc / "self" / "mountinfo").write_text(
            "1 0 179:0 / / rw - ext4 /dev/mmcblk0 rw\n")
        with patch.object(formatter, "_mounted_devices", return_value=set()):
            self.assertEqual([target.name for target in formatter.discover()], ["sda", "sda1"])

    def test_unidentifiable_root_blocks_discovery_and_formatting(self):
        target = formatter._target("sda", "sda1")
        ctx = Context(AppSpec("format", lambda ctx: None))
        (self.proc / "self" / "mountinfo").write_text(
            "1 0 0:42 / / rw - overlay overlay rw\n")
        with self.assertRaisesRegex(ValueError, "Cannot identify system storage"):
            formatter.discover()
        with patch.object(formatter.shutil, "which", return_value="/tools/fake"), \
                patch.object(ctx, "run") as run:
            with self.assertRaisesRegex(ValueError, "Cannot identify system storage"):
                formatter.format_target(ctx, target, "ext4", None)
            run.assert_not_called()

    def test_swap_and_unknown_device_are_excluded(self):
        target = formatter._target("sda", "sda1")
        (self.proc / "swaps").write_text(
            f"Filename Type Size Used Priority\n{self.dev / 'sda1'} partition 1 0 -2\n")
        with self.assertRaisesRegex(ValueError, "swap"):
            formatter._check_safe(target)
        (self.proc / "swaps").write_text("Filename Type Size Used Priority\n")
        internal = self.sys / "nvme0n1"
        internal.mkdir()
        (internal / "device").symlink_to(self.tmp.name)
        self.write_device(internal, "8:4", 100000)
        (internal / "diskseq").write_text("125")
        self.assertEqual([entry.name for entry in formatter.discover()], ["sda", "sda1"])

    def test_sd_card_detection_and_mmc_rejection(self):
        mmc = self.sys / "mmcblk0"
        mmc.mkdir()
        card = Path(self.tmp.name) / "card"
        card.mkdir()
        (mmc / "device").symlink_to(card)
        (card / "type").write_text("SD")
        self.write_device(mmc, "8:8", 100000)
        (mmc / "diskseq").write_text("126")
        self.assertEqual(formatter._target("mmcblk0", "mmcblk0").transport, "SD")
        (card / "type").write_text("MMC")
        with self.assertRaisesRegex(ValueError, "not identified"):
            formatter._target("mmcblk0", "mmcblk0")

    def test_partition_format_uses_fixed_args_and_reports_success(self):
        target = formatter._target("sda", "sda1")
        ctx = Context(AppSpec("format", lambda ctx: None))
        with patch.object(formatter.shutil, "which", side_effect=lambda name: "/tools/" + name), \
                patch.object(ctx, "run", return_value=RunResult(0, [])) as run:
            formatter.format_target(ctx, target, "FAT32", None)
        self.assertEqual(run.call_args.args[0],
                         ["/tools/mkfs.fat", "-F", "32", str(self.dev / "sda1")])
        self.assertEqual(ctx._bar.value, 1)

    def test_full_disk_preflights_tools_and_creates_partition(self):
        target = formatter._target("sda", "sda")
        ctx = Context(AppSpec("format", lambda ctx: None))
        with patch.object(formatter.shutil, "which", side_effect=lambda name: None), \
                patch.object(ctx, "run") as run:
            with self.assertRaisesRegex(ValueError, "Required tool"):
                formatter.format_target(ctx, target, "ext4", "gpt")
            run.assert_not_called()
        commands = []
        original_partitions = formatter._partitions
        state = {"cleared": False}

        def fake_run(args, **kwargs):
            commands.append(args)
            if "mklabel" in args:
                state["cleared"] = True
            elif "mkpart" in args:
                state["cleared"] = False
            return RunResult(0, [])

        with patch.object(formatter.shutil, "which", side_effect=lambda name: "/tools/" + name), \
                patch.object(formatter, "_partitions",
                             side_effect=lambda disk: [] if state["cleared"]
                             else original_partitions(disk)), \
                patch.object(ctx, "run", side_effect=fake_run):
            formatter.format_target(ctx, target, "ext4", "gpt")
        self.assertEqual([Path(cmd[0]).name for cmd in commands],
                         ["parted", "parted", "partprobe", "mkfs.ext4"])
        self.assertEqual(commands[0][-2:], ["mklabel", "gpt"])
        self.assertEqual(commands[-1][-1], str(self.dev / "sda1"))

    def test_mount_or_missing_confirmation_prevents_commands(self):
        target = formatter._target("sda", "sda1")
        ctx = Context(AppSpec("format", lambda ctx: None),
                      {"target": target.token(), "filesystem": "NTFS", "confirm": True})
        (self.proc / "self" / "mountinfo").write_text(
            self.root_mount + f"2 1 {self.number} / /mnt rw - ext4 /dev/sda1 rw\n")
        with patch.object(formatter.shutil, "which", return_value="/tools/fake"), \
                patch.object(ctx, "run") as run:
            formatter.format_drive(ctx)
        self.assertEqual(ctx.status, "error")
        run.assert_not_called()
        ctx.params["confirm"] = False
        with patch.object(ctx, "run") as run:
            formatter.format_drive(ctx)
        run.assert_not_called()

    def test_mbr_size_limit_and_stale_device_fail_before_commands(self):
        (self.disk / "size").write_text(str(0x100000000))
        target = formatter._target("sda", "sda")
        ctx = Context(AppSpec("format", lambda ctx: None))
        with patch.object(ctx, "run") as run:
            with self.assertRaisesRegex(ValueError, "too large"):
                formatter.format_target(ctx, target, "NTFS", "msdos")
            run.assert_not_called()
        (self.disk / "diskseq").write_text("999")
        with patch.object(formatter.shutil, "which", return_value="/tools/fake"), \
                patch.object(ctx, "run") as run:
            with self.assertRaisesRegex(ValueError, "changed"):
                formatter.format_target(ctx, target, "NTFS", "gpt")
            run.assert_not_called()

    def test_error_stops_sequence_and_no_false_success(self):
        target = formatter._target("sda", "sda1")
        ctx = Context(AppSpec("format", lambda ctx: None),
                      {"target": target.token(), "filesystem": "NTFS", "confirm": True})
        with patch.object(formatter.shutil, "which", return_value="/tools/mkfs.ntfs"), \
                patch.object(ctx, "run", return_value=RunResult(1, ["Permission denied"])) as run:
            formatter.format_drive(ctx)
        self.assertEqual(ctx.status, "error")
        self.assertEqual(ctx._bar.text, "Failed")
        self.assertIn("Permission denied", str(ctx._log._raw))
        run.assert_called_once()

    def test_hotplug_change_after_partition_table_stops_before_partitioning(self):
        target = formatter._target("sda", "sda")
        ctx = Context(AppSpec("format", lambda ctx: None))

        def fake_run(args, **kwargs):
            (self.disk / "diskseq").write_text("999")
            return RunResult(0, [])

        with patch.object(formatter.shutil, "which", side_effect=lambda name: "/tools/" + name), \
                patch.object(ctx, "run", side_effect=fake_run) as run:
            with self.assertRaisesRegex(ValueError, "changed"):
                formatter.format_target(ctx, target, "ext4", "gpt")
            self.assertEqual(run.call_count, 1)

    def test_format_cancellation_is_reported(self):
        target = formatter._target("sda", "sda1")
        ctx = Context(AppSpec("format", lambda ctx: None),
                      {"target": target.token(), "filesystem": "NTFS", "confirm": True})
        with patch.object(formatter.shutil, "which", return_value="/tools/mkfs.ntfs"), \
                patch.object(ctx, "run", side_effect=Cancelled):
            with self.assertRaises(Cancelled):
                formatter.format_drive(ctx)
        self.assertIn("partially formatted", str(ctx._log._raw))

    def test_setup_navigation_and_native_group(self):
        pygame.font.init()
        spec = AppSpec("Format drive", formatter.format_drive,
                       group="Removable Storage", setup=formatter.setup)
        other = AppSpec("Example", lambda ctx: None, group="Other")
        apps = default_menu([spec, other]).items[0].menu
        group = next(item for item in apps.items if item.label == "Removable Storage")
        self.assertEqual(group.menu.items[0].spec, spec)
        nav = Navigator()
        self.addCleanup(nav.close_all)
        menu = MenuScreen(Menu("Main", [Launch(spec)]))
        nav.push(menu)
        menu.action("a")
        screen = nav.top
        self.assertIsInstance(screen, SetupScreen)
        screen.action("right")
        self.assertNotIn("table", [f.key for f in screen.form.visible_fields(screen.values)])
        for size in ((320, 240), (640, 480)):
            screen.draw(pygame.Surface(size), pygame.Rect(0, 30, size[0], size[1] - 60),
                        Theme(*size))
        screen.action("down")
        screen.action("down")
        self.assertEqual(screen.form.visible_fields(screen.values)[screen.selected].key, "confirm")

    def test_native_loader_registers_storage_app(self):
        root = str(Path(__file__).resolve().parents[1])
        with patch.object(context, "_REGISTRY", []):
            specs = discover(root, DEFAULTS)
        spec = next(spec for spec in specs if spec.name == "Format drive")
        self.assertEqual((spec.source, spec.group), ("native", "Removable Storage"))
        apps = default_menu(specs).items[0].menu
        group = next(item for item in apps.items if item.label == "Removable Storage")
        self.assertTrue(any(item.spec is spec for item in group.menu.items))


if __name__ == "__main__":
    unittest.main()
