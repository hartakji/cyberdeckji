"""Shared discovery and safety checks for USB storage and SD cards on Linux.

Every tool in the Removable Storage group uses these helpers so the disk that
backs the running system is never offered, and mounted devices are refused."""

from dataclasses import dataclass
from datetime import datetime
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

from deckfw import Choice, Form
from apps._user_paths import directory


SYS_BLOCK = Path("/sys/class/block")
SYS_DEV_BLOCK = Path("/sys/dev/block")
PROC = Path("/proc")
DEV = Path("/dev")
ENV = {"LC_ALL": "C", "LANG": "C"}
CHUNK = 4 * 1024 * 1024
MIB = 1024 * 1024
EMPTY_HELP = "Connect or unmount a USB drive or SD card. Press B and reopen to refresh."
FS_NAMES = {"vfat": "FAT", "exfat": "exFAT", "ext2": "ext2", "ext3": "ext3",
            "ext4": "ext4", "ntfs": "NTFS", "ntfs3": "NTFS"}


def transport(disk):
    device = disk / "device"
    resolved = device.resolve(strict=True)
    if any(part.startswith("usb") and part[3:].isdigit() for part in resolved.parts):
        return "USB"
    if disk.name.startswith("mmcblk") and (device / "type").read_text(
            encoding="utf-8").strip() == "SD":
        return "SD"
    return None


@dataclass(frozen=True)
class Target:
    disk: str
    name: str
    diskseq: int
    disk_dev: str
    device_dev: str
    sectors: int
    start: int
    transport: str

    @property
    def whole(self):
        return self.name == self.disk

    @property
    def size(self):
        return self.sectors * 512

    def token(self):
        return json.dumps([self.disk, self.name, self.diskseq, self.disk_dev,
                           self.device_dev, self.sectors, self.start, self.transport],
                          separators=(",", ":"))


def _read(path):
    return path.read_text(encoding="utf-8").strip()


def _number(path):
    value = _read(path)
    if not value.isdecimal():
        raise ValueError(f"Invalid device metadata: {path}")
    return int(value)


def _device_number(path):
    number = _read(path)
    parts = number.split(":")
    if len(parts) != 2 or not all(part.isdecimal() for part in parts):
        raise ValueError(f"Invalid device number: {path}")
    return number


def _partitions(disk):
    return sorted(path.name for path in disk.iterdir()
                  if (path / "partition").is_file())


def _target(disk_name, name):
    if "/" in disk_name or "/" in name or disk_name in ("", ".", "..") or name in ("", ".", ".."):
        raise ValueError("Invalid device name")
    disk = SYS_BLOCK / disk_name
    if (disk / "partition").exists() or not disk.exists():
        raise ValueError("Device is not a physical disk")
    kind = transport(disk)
    if kind is None:
        raise ValueError("Device is not identified as USB storage or an SD card")
    if name != disk_name and name not in _partitions(disk):
        raise ValueError("Partition no longer belongs to the selected disk")
    device = SYS_BLOCK / name
    diskseq = _number(disk / "diskseq")
    disk_dev = _device_number(disk / "dev")
    device_dev = _device_number(device / "dev")
    sectors = _number(device / "size")
    start = _number(device / "start") if name != disk_name else 0
    if not sectors or _number(disk / "ro") or _number(device / "ro"):
        raise ValueError("Device is empty or read-only")
    return Target(disk_name, name, diskseq, disk_dev, device_dev, sectors, start, kind)


def discover(partitions=True):
    """Unmounted, writable removable disks (and their partitions), minus the system disk."""
    if sys.platform != "linux":
        raise ValueError("Removable storage tools require Linux.")
    targets = []
    try:
        system_disks = _system_disk_devices()
        mounted = _mounted_devices()
        for disk in sorted(SYS_BLOCK.iterdir()):
            if (disk / "partition").exists():
                continue
            try:
                whole = _target(disk.name, disk.name)
                if whole.disk_dev in system_disks:
                    continue
                _check_disk_busy(whole.disk, mounted)
                targets.append(whole)
                if partitions:
                    for name in _partitions(disk):
                        targets.append(_target(disk.name, name))
            except (OSError, ValueError):
                continue
    except OSError as exc:
        raise ValueError(f"Cannot discover block devices: {exc}") from exc
    return targets


def human_size(size):
    for unit, factor in (("TiB", 1024 ** 4), ("GiB", 1024 ** 3), ("MiB", MIB)):
        if size >= factor:
            return f"{size / factor:.1f}{unit}"
    return f"{size}B"


def label(target):
    kind = "disk" if target.whole else "part"
    return f"{target.name} {target.size / (1024 ** 3):.1f}GiB {target.transport} {kind}"


def drive_label(disk_name):
    """A file-name-safe drive name such as 'SanDisk_Ultra', else the kernel name."""
    device = SYS_BLOCK / disk_name / "device"
    parts = []
    for key in ("vendor", "model", "name"):
        try:
            value = _read(device / key)
        except OSError:
            continue
        if value and value not in parts:
            parts.append(value)
        if key == "model" and parts:
            break
    name = re.sub(r"[^A-Za-z0-9.-]+", "_", " ".join(parts)).strip("._-")
    return name[:40] or disk_name


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def fs_type(name):
    """Filesystem signature of /dev/<name> using blkid, or '' when unknown."""
    try:
        result = subprocess.run(["blkid", "-p", "-o", "value", "-s", "TYPE", str(DEV / name)],
                                capture_output=True, text=True, timeout=10,
                                env={**os.environ, **ENV})
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def token_target(token):
    try:
        values = json.loads(token)
        if not isinstance(values, list) or len(values) != 8:
            raise ValueError("Invalid selection")
        selected = Target(*values)
        current = _target(selected.disk, selected.name)
    except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot verify selected target: {exc}") from exc
    if current != selected:
        raise ValueError("Selected device changed; return to setup and select it again.")
    return current


def _mounted_devices():
    result = set()
    try:
        mount_files = [PROC / "self" / "mountinfo"]
        for process in PROC.iterdir():
            if not process.name.isdecimal():
                continue
            mount_files.append(process / "mountinfo")
        for mount_file in mount_files:
            try:
                lines = mount_file.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                if mount_file == PROC / "self" / "mountinfo":
                    raise
                continue
            if not lines and mount_file == PROC / "self" / "mountinfo":
                raise ValueError(f"Empty mount information: {mount_file}")
            for line in lines:
                fields = line.split()
                number = fields[2].split(":") if len(fields) >= 6 else []
                if len(number) != 2 or not all(part.isdecimal() for part in number):
                    raise ValueError("Invalid mount information")
                result.add(fields[2])
        lines = (PROC / "swaps").read_text(encoding="utf-8").splitlines()
        if not lines or not lines[0].startswith("Filename"):
            raise ValueError("Invalid swap information")
        for line in lines[1:]:
            path = line.split()[0].replace("\\040", " ").replace("\\011", "\t")
            info = os.stat(path)
            dev = info.st_rdev if stat.S_ISBLK(info.st_mode) else info.st_dev
            result.add(f"{os.major(dev)}:{os.minor(dev)}")
    except OSError as exc:
        raise ValueError(f"Cannot verify mounts and swap: {exc}") from exc
    return result


def _system_disk_devices():
    """Resolve the root filesystem through partitions and block-device slaves."""
    try:
        lines = (PROC / "self" / "mountinfo").read_text(encoding="utf-8").splitlines()
        roots = []
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or "-" not in fields:
                raise ValueError("Invalid mount information")
            if fields[4] == "/":
                roots.append(fields[2])
        if len(roots) != 1:
            raise ValueError("Cannot uniquely identify the root filesystem")
        root = roots[0].split(":")
        if len(root) != 2 or not all(part.isdecimal() for part in root):
            raise ValueError("Invalid root filesystem device")

        disks, visited, active = set(), set(), set()

        def visit(number):
            if number in active:
                raise ValueError("Cycle in system storage dependencies")
            if number in visited:
                return
            active.add(number)
            node = (SYS_DEV_BLOCK / number).resolve(strict=True)
            if _device_number(node / "dev") != number:
                raise ValueError("System storage device identity changed")
            if (node / "partition").is_file():
                parent = node.parent
                visit(_device_number(parent / "dev"))
            else:
                slaves = list((node / "slaves").iterdir())
                if slaves:
                    for slave in slaves:
                        visit(_device_number(slave / "dev"))
                else:
                    if not (node / "device").exists():
                        raise ValueError("System storage backing device is unknown")
                    disks.add(number)
            active.remove(number)
            visited.add(number)

        visit(roots[0])
        if not disks:
            raise ValueError("No system backing disks found")
        return disks
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot identify system storage: {exc}") from exc


def _check_disk_busy(disk_name, mounted):
    disk = SYS_BLOCK / disk_name
    names = [disk_name, *_partitions(disk)]
    for name in names:
        path = SYS_BLOCK / name
        if _device_number(path / "dev") in mounted:
            raise ValueError(f"{name} is mounted or used as swap; unmount it first.")
        if any((path / "holders").iterdir()):
            raise ValueError(f"{name} has dependent block devices; release them first.")


def check_safe(target, write=True):
    current = token_target(target.token())
    if current.disk_dev in _system_disk_devices():
        raise ValueError("Cannot use the disk backing the running system.")
    _check_disk_busy(current.disk, _mounted_devices())
    info = os.stat(DEV / current.name)
    if not stat.S_ISBLK(info.st_mode):
        raise ValueError("Target is not a block device.")
    if f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != current.device_dev:
        raise ValueError("Device node no longer matches the selected target.")
    if not os.access(DEV / current.name, os.W_OK if write else os.R_OK):
        raise ValueError("No permission for the selected device; run as root.")
    return current


def open_device(target, write=False, direct=False):
    """Check safety, then open /dev/<name> and confirm the descriptor is that device."""
    current = check_safe(target, write=write)
    flags = (os.O_WRONLY if write else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    if direct:
        flags |= getattr(os, "O_DIRECT", 0)
    fd = os.open(DEV / current.name, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISBLK(info.st_mode)
                or f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != current.device_dev):
            raise ValueError("Opened device does not match the selected target.")
    except BaseException:
        os.close(fd)
        raise
    return fd


def require_tools(*names):
    tools = {}
    for name in names:
        tools[name] = shutil.which(name)
        if not tools[name]:
            raise ValueError(f"Required tool not found: {name}")
    return tools


def run_tool(ctx, args, check=True, log=False):
    result = ctx.run(args, log=log, env=ENV)
    if check and result.returncode:
        detail = " ".join(line.strip() for line in result.lines if line.strip())
        raise ValueError(f"{Path(args[0]).name} failed ({result.returncode}): "
                         f"{detail or 'no diagnostic output'}")
    return result


def aligned_buffer(size=CHUNK):
    """Page-aligned memory, as required by O_DIRECT reads and writes."""
    return mmap.mmap(-1, size)


def copy_stream(ctx, read, write, total, label, limit=None):
    """Copy chunks from read(n) to write(data) with progress and cancellation.

    total may be None (indeterminate). limit stops with an error if more data arrives."""
    done = 0
    started = last = time.monotonic()
    while True:
        ctx.check()
        data = read(CHUNK)
        if not data:
            break
        if limit is not None and done + len(data) > limit:
            raise ValueError("Image is larger than the selected drive.")
        write(data)
        done += len(data)
        now = time.monotonic()
        if now - last >= 0.25 or (total and done >= total):
            last = now
            speed = done / MIB / max(now - started, 1e-6)
            text = f"{done // MIB} MiB · {speed:.1f} MiB/s"
            ctx.progress(min(done / total, 1) if total else None, label,
                         text=f"{done * 100 // total}% · {text}" if total else text)
    return done


def write_all(fd, data, offset=None):
    view = memoryview(data)
    while view:
        written = os.write(fd, view) if offset is None else os.pwrite(fd, view, offset)
        if written <= 0:
            raise OSError("Short write to device")
        view = view[written:]
        if offset is not None:
            offset += written


def drive_field(targets, title="Drive", help=""):
    if not targets:
        return Choice("target", title, [("No eligible drives", "")], "", help=EMPTY_HELP)
    choices = [(label(target), target.token()) for target in targets]
    return Choice("target", title, choices, choices[0][1], help=help)


class StorageForm(Form):
    """Requires a selected drive and, when visible, an explicit confirmation.

    Any change other than the confirmation itself resets it to No."""

    confirm_message = "Confirm before continuing."

    def changed(self, values, key):
        if key != "confirm" and "confirm" in values:
            values["confirm"] = False

    def validate(self, values):
        result = super().validate(values)
        if "target" in result and not result["target"]:
            raise ValueError("No eligible drives. Connect or unmount one, then reopen.")
        if "image" in result and not result["image"]:
            raise ValueError("No images found in ~/Backups.")
        if "confirm" in result and not result["confirm"]:
            raise ValueError(self.confirm_message)
        return result

    def submitted(self, values):
        if "confirm" in values:
            values["confirm"] = False


def empty_form():
    return StorageForm([drive_field([])], submit_label="No drives available")


def backup_dir():
    return directory("Backups")


def sync_device(ctx, fd, name):
    ctx.progress(None, f"Flushing {name}")
    os.fsync(fd)
