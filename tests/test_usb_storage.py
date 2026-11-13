import os
from pathlib import Path
import pwd
from datetime import datetime
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from apps import _usb_otg, usb_storage
from deckfw import context
from deckfw.config import DEFAULTS
from deckfw.loader import discover
from deckfw.menu import default_menu
from deckfw.screens import AppScreen


class UsbStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = patch.object(Path, "home", return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)
        self.identity = patch.dict(os.environ)
        self.identity.start()
        self.addCleanup(self.identity.stop)
        os.environ.pop("SUDO_UID", None)

    def test_media_folders_are_directly_in_user_home(self):
        for kind in ("Images", "ISO"):
            self.assertEqual(usb_storage.directory(kind), self.root / kind)

    def test_sudo_uses_invoking_user_home_instead_of_root_home(self):
        user = pwd.struct_passwd(("user", "x", 1234, 1234, "", "/home/user", "/bin/sh"))
        with patch.object(os, "geteuid", return_value=0), \
             patch.dict(os.environ, {"SUDO_UID": "1234", "HOME": "/root"}), \
             patch.object(pwd, "getpwuid", return_value=user) as lookup:
            for kind in ("Images", "ISO"):
                self.assertEqual(usb_storage.directory(kind), Path("/home/user") / kind)
            lookup.assert_called_with(1234)

    def test_non_root_does_not_use_sudo_environment(self):
        with patch.object(os, "geteuid", return_value=1234), \
             patch.dict(os.environ, {"SUDO_UID": "5678"}), \
             patch.object(pwd, "getpwuid") as lookup:
            self.assertEqual(usb_storage.directory("Images"), self.root / "Images")
            lookup.assert_not_called()

    def test_tool_path_includes_system_admin_directories_without_changing_environment(self):
        ctx = context.Context(context.AppSpec("Storage", lambda ctx: None))
        for tool in ("parted", "mkfs.fat", "mkfs.exfat", "mkfs.ext4", "mkfs.ntfs"):
            with self.subTest(tool=tool), \
                 patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), \
                 patch.object(ctx, "run", return_value=context.RunResult(0, [])) as run:
                usb_storage.run_tool(ctx, [tool, "--help"], log=False)
                run.assert_called_once_with(
                    [tool, "--help"], log=False,
                    env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
                self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")

    def test_group_and_conditional_forms(self):
        with patch.object(context, "_REGISTRY", []):
            specs = discover(".", DEFAULTS)
        apps = default_menu(specs).items[0].menu
        group = next(item for item in apps.items if item.label == "USB-OTG")
        self.assertEqual({item.label for item in group.menu.items},
                         {"USB-MTP", "USB Storage", "USB CD-ROM emulator"})
        self.assertEqual(group.icon, "icons/usb-otg.png")
        self.assertEqual({item.label: item.icon for item in group.menu.items}, {
            "USB-MTP": "icons/usb-mtp.png",
            "USB Storage": "icons/usb-drive.png",
            "USB CD-ROM emulator": "icons/usb-cdrom.png",
        })
        root = Path(__file__).resolve().parents[1]
        for item in [group, *group.menu.items]:
            self.assertTrue((root / item.icon).is_file())
        form = usb_storage.storage_setup()
        values = form.defaults()
        self.assertEqual(values["action"], "new")
        self.assertEqual(set(form.validate(values)), {"action", "size", "filesystem"})
        self.assertEqual([value for _, value in form.fields[2].choices], [1, 4, 8, 16, 32])
        values["action"] = "existing"
        with self.assertRaisesRegex(ValueError, "No image"):
            form.validate(values)
        iso = usb_storage.cdrom_setup()
        with self.assertRaisesRegex(ValueError, "No ISO"):
            iso.validate(iso.defaults())

    def test_selected_file_requires_regular_nonempty_file(self):
        folder = usb_storage.directory("Images")
        folder.mkdir(parents=True)
        (folder / "test.img").write_bytes(b"abc")
        (folder / "link.img").symlink_to(folder / "test.img")
        (folder / "empty.img").touch()
        self.assertEqual(usb_storage.selected_file("Images", "test.img", ".img")[1], 3)
        for name in ("../test.img", "link.img", "empty.img"):
            with self.assertRaises(ValueError):
                usb_storage.selected_file("Images", name, ".img")
        self.assertEqual([value for _, value in usb_storage.choices("Images", ".img")],
                         ["empty.img", "test.img"])

    def test_configuration_preserves_other_settings_and_quotes_paths(self):
        config = self.root / "default"
        config.write_text("# existing\nOTHER=keep\nUSB_MASS_STORAGE_MODE=old\n"
                          "USB_MASS_STORAGE_IMAGE=old\nUSB_MASS_STORAGE_SIZE=old\n")
        path = self.root / "spaces 'quote' \"double\" $value `cmd` \\ slash.img"
        with patch.object(usb_storage, "CONFIG", config):
            usb_storage.write_config("disk", path, 12345)
            lines = config.read_text().splitlines()
            self.assertIn("OTHER=keep", lines)
            values = dict(line.split("=", 1) for line in lines if "=" in line)
            result = subprocess.run(
                ["sh", "-c", '. "$1"; printf "%s" "$USB_MASS_STORAGE_IMAGE"',
                 "sh", str(config)], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout, str(path))
            self.assertIn(r"\$value \`cmd\`", values["USB_MASS_STORAGE_IMAGE"])
            self.assertEqual(values["USB_MASS_STORAGE_SIZE"], "12345")
            usb_storage.write_config("cdrom", path)
            values = dict(line.split("=", 1) for line in config.read_text().splitlines()
                          if "=" in line)
            self.assertEqual(values["USB_MASS_STORAGE_MODE"], "cdrom")
            self.assertEqual(values["USB_MASS_STORAGE_SIZE"], "12345")

    def test_config_refuses_multiline_assignment(self):
        config = self.root / "default"
        original = "USB_MASS_STORAGE_IMAGE='first\nsecond'\nOTHER=keep\n"
        config.write_text(original)
        with patch.object(usb_storage, "CONFIG", config):
            with self.assertRaisesRegex(ValueError, "multiline"):
                usb_storage.write_config("disk", self.root / "x.img", 1)
        self.assertEqual(config.read_text(), original)

    def test_image_creation_layout_and_cleanup(self):
        size = 4 * 1024 * 1024
        calls = []

        def fake_tool(ctx, args, log=True):
            calls.append(args)
            if args[0] == "fallocate":
                with open(args[-1], "r+b") as output:
                    output.truncate(size)
            elif args[0] == "parted" and "print" in args:
                start = 1024 * 1024
                return context.RunResult(0, [f"1:{start}B:{size-1}B:{size-start}B:;"])
            elif args[0].startswith("mkfs."):
                with open(args[-1], "r+b") as part:
                    part.write(b"filesystem")
            return context.RunResult(0, [])

        for index, (filesystem, part_type, formatter) in enumerate((
            ("FAT32", "fat32", "mkfs.fat"), ("exFAT", "ntfs", "mkfs.exfat"),
            ("ext4", "ext4", "mkfs.ext4"), ("NTFS", "ntfs", "mkfs.ntfs"),
        )):
            calls.clear()
            ctx = context.Context(context.AppSpec("Storage", lambda ctx: None))
            user = pwd.struct_passwd(("user", "x", 1234, 5678, "",
                                     str(self.root), "/bin/sh"))
            with patch.object(usb_storage, "GIB", size), \
                 patch.object(usb_storage, "run_tool", side_effect=fake_tool), \
                 patch.object(usb_storage, "invoking_user", return_value=user), \
                 patch.object(usb_storage, "datetime") as clock, \
                 patch.object(os, "chown") as chown:
                clock.now.return_value = datetime(2026, 11, 12)
                path, actual = usb_storage.create_image(ctx, 1, filesystem)
            self.assertEqual(path.name, f"1G-20261112-{index}.img")
            for existing in path.parent.glob("*.img"):
                with existing.open("rb") as image:
                    image.seek(1024 * 1024)
                    self.assertEqual(image.read(10), b"filesystem")
            self.assertEqual(chown.call_args.args[1:], (1234, 5678))
            if filesystem == "FAT32":
                self.assertEqual(chown.call_args_list[0].args,
                                 (self.root / "Images", 1234, 5678))
            self.assertEqual(actual, size)
            with open(path, "rb") as image:
                image.seek(1024 * 1024)
                self.assertEqual(image.read(10), b"filesystem")
            self.assertEqual(calls[1][7], part_type)
            self.assertEqual(calls[3][0], formatter)
            if filesystem == "FAT32":
                self.assertEqual(calls[3][-3:-1], ["-h", "2048"])
            if filesystem == "NTFS":
                self.assertEqual(calls[3][-3:-1], ["-p", "2048"])
            self.assertEqual(list(path.parent.glob("*.partial")), [])

    def test_image_creation_failure_removes_partials(self):
        ctx = context.Context(context.AppSpec("Storage", lambda ctx: None))
        with patch.object(usb_storage, "GIB", 4 * 1024 * 1024), \
             patch.object(usb_storage, "run_tool", side_effect=context.Cancelled):
            with self.assertRaises(context.Cancelled):
                usb_storage.create_image(ctx, 1, "FAT32")
        self.assertEqual(list(usb_storage.directory("Images").iterdir()), [])

    def test_storage_and_cdrom_use_selected_file_and_cleanup(self):
        for kind, filename, runner, params in (
            ("Images", "disk.img", usb_storage.usb_storage,
             {"action": "existing", "image": "disk.img"}),
            ("ISO", "disc.iso", usb_storage.usb_cdrom, {"iso": "disc.iso"}),
        ):
            folder = usb_storage.directory(kind)
            folder.mkdir(parents=True)
            (folder / filename).write_bytes(b"hello")
            config = self.root / "default"
            config.write_text("OTHER=keep\n")
            spec = context.AppSpec("USB", runner, shutdown=_usb_otg.shutdown)
            calls = []
            def command(ctx, *args):
                calls.append(args[0])
                if args[0] == "show":
                    return context.RunResult(0, ["LoadState=loaded", "ActiveState=inactive"])
                if args[0] == "start":
                    ctx.cancel()
                return context.RunResult(0, [])
            with patch.object(usb_storage, "CONFIG", config), \
                 patch.object(_usb_otg, "command", side_effect=command):
                screen = AppScreen(spec, params)
                screen._thread.join(2)
            self.assertFalse(screen.alive)
            self.assertEqual(calls, ["show", "show", "start", "stop"])
            content = config.read_text()
            self.assertIn("USB_MASS_STORAGE_MODE=" +
                          ("disk" if kind == "Images" else "cdrom"), content)
            self.assertIn(str(folder / filename), content)
            if kind == "Images":
                self.assertIn("USB_MASS_STORAGE_SIZE=5", content)

    def test_running_service_does_not_change_config_or_image(self):
        config = self.root / "default"
        config.write_text("OTHER=keep\n")
        spec = context.AppSpec("USB", usb_storage.usb_storage,
                               shutdown=_usb_otg.shutdown)
        params = {"action": "new", "size": 1, "filesystem": "FAT32"}
        with patch.object(usb_storage, "CONFIG", config), \
             patch.object(_usb_otg, "command", return_value=context.RunResult(
                 0, ["LoadState=loaded", "ActiveState=active"])) as command:
            screen = AppScreen(spec, params)
            screen._thread.join(2)
        self.assertEqual(screen.ctx.status, "error")
        self.assertEqual(command.call_count, 1)
        self.assertEqual(config.read_text(), "OTHER=keep\n")
        self.assertFalse(usb_storage.directory("Images").exists())

    def test_new_image_size_uses_gigabyte_suffix(self):
        config = self.root / "default"
        config.write_text("")
        spec = context.AppSpec("USB", usb_storage.usb_storage,
                               shutdown=_usb_otg.shutdown)
        params = {"action": "new", "size": 4, "filesystem": "NTFS"}

        def command(ctx, *args):
            if args[0] == "show":
                return context.RunResult(0, ["LoadState=loaded", "ActiveState=inactive"])
            if args[0] == "start":
                ctx.cancel()
            return context.RunResult(0, [])

        with patch.object(usb_storage, "CONFIG", config), \
             patch.object(usb_storage, "create_image",
                          return_value=(self.root / "disk.img", 4 * usb_storage.GIB)), \
             patch.object(_usb_otg, "command", side_effect=command):
            screen = AppScreen(spec, params)
            screen._thread.join(2)
        self.assertIn("USB_MASS_STORAGE_SIZE=4G\n", config.read_text())

    def test_cdrom_real_runner_starts_and_stops_stub_service(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        timeout = bin_dir / "timeout"
        timeout.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
        systemctl = bin_dir / "systemctl"
        systemctl.write_text(
            '#!/bin/sh\n'
            '[ "$1" = "--no-pager" ] || exit 1\n'
            'shift\n'
            'printf "%s %s\\n" "$1" "$2" >> "$FAKE_SERVICE_LOG"\n'
            'case "$1" in\n'
            '  show) printf "LoadState=loaded\\nActiveState=inactive\\n";;\n'
            '  start) : > "$FAKE_SERVICE_READY";;\n'
            '  stop) : > "$FAKE_SERVICE_STOPPED";;\n'
            'esac\n')
        timeout.chmod(0o755)
        systemctl.chmod(0o755)
        iso_dir = usb_storage.directory("ISO")
        iso_dir.mkdir(parents=True)
        (iso_dir / "disk.iso").write_bytes(b"synthetic ISO")
        config = self.root / "default"
        config.write_text("")
        log, ready, stopped = (self.root / name for name in ("log", "ready", "stopped"))
        environment = {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_SERVICE_LOG": str(log),
            "FAKE_SERVICE_READY": str(ready),
            "FAKE_SERVICE_STOPPED": str(stopped),
        }
        spec = context.AppSpec("CD-ROM", usb_storage.usb_cdrom, shutdown=_usb_otg.shutdown)
        with patch.object(usb_storage, "CONFIG", config), patch.dict(os.environ, environment):
            screen = AppScreen(spec, {"iso": "disk.iso"})
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and screen.alive and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), screen.ctx.error)
            finally:
                screen.ctx.cancel()
                screen._thread.join(5)
        self.assertFalse(screen.alive)
        self.assertTrue(stopped.exists())
        self.assertEqual(log.read_text().splitlines(),
                         ["show usb-massstorage", "show usb-massstorage",
                          "start usb-massstorage", "stop usb-massstorage"])
        self.assertIn("USB_MASS_STORAGE_MODE=cdrom", config.read_text())


if __name__ == "__main__":
    unittest.main()
