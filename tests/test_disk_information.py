import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pygame

from deckfw import Context, context
from deckfw.config import DEFAULTS
from deckfw.context import AppSpec, RunResult
from deckfw.loader import discover
from deckfw.menu import default_menu
from deckfw.theme import Theme
from apps import disk_information as info


class DiskInformationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.sys = root / "sys"
        self.sys.mkdir()
        usb = root / "usb1" / "device"
        usb.mkdir(parents=True)
        self.disk = self.sys / "sda"
        self.disk.mkdir()
        (self.disk / "device").symlink_to(usb)
        (self.disk / "dev").write_text("8:0")
        (self.disk / "size").write_text("2097152")
        (self.disk / "diskseq").write_text("123")
        self.patches = [patch.object(info, "SYS_BLOCK", self.sys),
                        patch.object(info, "DEV", root / "dev"),
                        patch.object(info.sys, "platform", "linux")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def result(self, children=None):
        return json.dumps({"blockdevices": [{
            "name": "sda", "type": "disk", "size": 1073741824,
            "maj:min": "8:0", "children": children if children is not None else [
                {"name": "sda1", "type": "part", "size": 536870912, "fstype": "vfat"},
                {"name": "sda2", "type": "part", "size": 268435456, "fstype": "ext4"},
                {"name": "sda3", "type": "part", "size": 1024, "fstype": None},
            ]}]})

    def test_setup_includes_read_only_and_mounted_drives(self):
        (self.disk / "ro").write_text("1")
        form = info.setup()
        selected = info.selected_disk(form.defaults()["disk"])
        self.assertEqual((selected.name, selected.transport, selected.size),
                         ("sda", "USB", 1073741824))
        (self.disk / "diskseq").write_text("124")
        with self.assertRaisesRegex(ValueError, "changed"):
            info.selected_disk(form.defaults()["disk"])

    def test_sd_card_and_unknown_devices(self):
        card_device = Path(self.tmp.name) / "card"
        card_device.mkdir()
        (card_device / "type").write_text("SD")
        card = self.sys / "mmcblk0"
        card.mkdir()
        (card / "device").symlink_to(card_device)
        (card / "dev").write_text("179:0")
        (card / "diskseq").write_text("125")
        (card / "size").write_text("4096")
        self.assertEqual(info.disk_info("mmcblk0").transport, "SD")
        (card_device / "type").write_text("MMC")
        with self.assertRaisesRegex(ValueError, "not USB"):
            info.disk_info("mmcblk0")

    def test_missing_tool_is_displayed_as_error(self):
        disk = info.disk_info("sda")
        ctx = Context(AppSpec("Disk", lambda ctx: None), {"disk": disk.token()})
        with patch.object(info.shutil, "which", return_value=None):
            info.disk_information(ctx)
        self.assertEqual(ctx.status, "error")
        self.assertIn("lsblk", str(ctx._log._raw))

    def test_reads_filesystems_and_sizes_without_writes(self):
        disk = info.disk_info("sda")
        ctx = Context(AppSpec("Disk", lambda ctx: None))
        with patch.object(info.shutil, "which", return_value="/tools/lsblk"), \
                patch.object(ctx, "run", return_value=RunResult(0, [self.result()])) as run:
            parts = info.read_partitions(ctx, disk)
        self.assertEqual([(p.name, p.size, p.filesystem) for p in parts],
                         [("sda1", 536870912, "FAT"), ("sda2", 268435456, "ext4"),
                          ("sda3", 1024, "Unknown")])
        self.assertEqual(run.call_args.args[0],
                         ["/tools/lsblk", "--json", "--bytes", "--output",
                          "NAME,TYPE,SIZE,FSTYPE,MAJ:MIN", str(info.DEV / "sda")])

    def test_empty_and_bad_output_is_reported(self):
        disk = info.disk_info("sda")
        ctx = Context(AppSpec("Disk", lambda ctx: None))
        with patch.object(info.shutil, "which", return_value="/tools/lsblk"):
            with patch.object(ctx, "run", return_value=RunResult(0, [self.result([])])):
                self.assertEqual(info.read_partitions(ctx, disk), ())
            for output in ("not json", "{}", self.result([None])):
                with self.subTest(output=output), \
                        patch.object(ctx, "run", return_value=RunResult(0, [output])), \
                        self.assertRaisesRegex(ValueError, "Cannot read"):
                    info.read_partitions(ctx, disk)
            with patch.object(ctx, "run", return_value=RunResult(1, ["permission denied"])), \
                    self.assertRaisesRegex(ValueError, "permission denied"):
                info.read_partitions(ctx, disk)
        with patch.object(info.shutil, "which", return_value=None), \
                self.assertRaisesRegex(ValueError, "lsblk"):
            info.read_partitions(ctx, disk)

    def test_disk_change_after_command_is_rejected(self):
        disk = info.disk_info("sda")
        ctx = Context(AppSpec("Disk", lambda ctx: None))

        def run(*args, **kwargs):
            (self.disk / "diskseq").write_text("124")
            return RunResult(0, [self.result()])

        with patch.object(info.shutil, "which", return_value="lsblk"), \
                patch.object(ctx, "run", side_effect=run), \
                self.assertRaisesRegex(ValueError, "changed"):
            info.read_partitions(ctx, disk)

    def test_render_and_scroll_at_supported_sizes(self):
        pygame.font.init()
        disk = info.disk_info("sda")
        parts = tuple(info.Partition(f"sda{i}", 1024 * i, "ext4") for i in range(1, 21))
        for size in ((320, 240), (640, 480)):
            view = info.PartitionTable(disk, parts)
            theme = Theme(*size)
            with patch.object(theme, "render", wraps=theme.render) as render:
                view.draw(pygame.Surface(size), pygame.Rect(0, 30, size[0], size[1] - 60), theme)
            texts = [call.args[1] for call in render.call_args_list]
            for text in ("Drive: sda | USB", "Disk size: 1.0 GiB", "Partition", "Size",
                         "Filesystem", "sda1", "ext4"):
                self.assertIn(text, texts)
            for _ in range(30):
                view.action("down")
            self.assertEqual(view.top, len(parts) - view.rows)
            self.assertFalse(view.action("a"))
            view.action("right")
            self.assertLessEqual(view.horizontal, view.max_horizontal)

    def test_native_discovery_group_and_no_duplicate_registration(self):
        root = str(Path(__file__).resolve().parents[1])
        with patch.object(context, "_REGISTRY", []):
            specs = discover(root, DEFAULTS)
        specs_named = [spec for spec in specs if spec.name == "Disk information"]
        self.assertEqual(len(specs_named), 1)
        spec = specs_named[0]
        self.assertEqual((spec.source, spec.group), ("native", "Removable Storage"))
        group = next(item for item in default_menu(specs).items[0].menu.items
                     if item.label == "Removable Storage")
        self.assertTrue(any(item.spec is spec for item in group.menu.items))


if __name__ == "__main__":
    unittest.main()
