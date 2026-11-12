"""Format explicitly selected USB storage or SD card partitions on Linux."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from deckfw import app, Cancelled, Choice, Form, Toggle
from apps._removable_storage import transport as _transport


SYS_BLOCK = Path("/sys/class/block")
SYS_DEV_BLOCK = Path("/sys/dev/block")
PROC = Path("/proc")
DEV = Path("/dev")
FORMATS = {"FAT32": ("mkfs.fat", "-F", "32"),
           "ext4": ("mkfs.ext4", "-F"),
           "NTFS": ("mkfs.ntfs", "-f")}
PARTED_TYPES = {"FAT32": "fat32", "ext4": "ext4", "NTFS": "ntfs"}


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
    transport = _transport(disk)
    if transport is None:
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
    return Target(disk_name, name, diskseq, disk_dev, device_dev, sectors, start, transport)


def discover():
    if sys.platform != "linux":
        raise ValueError("Formatting removable storage requires Linux.")
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
                for name in _partitions(disk):
                    targets.append(_target(disk.name, name))
            except (OSError, ValueError):
                continue
    except OSError as exc:
        raise ValueError(f"Cannot discover block devices: {exc}") from exc
    return targets


def _label(target):
    size = target.sectors * 512 / (1024 ** 3)
    kind = "disk" if target.whole else "part"
    return f"{target.name} {size:.1f}GiB {target.transport} {kind}"


class FormatForm(Form):
    def changed(self, values, key):
        if key != "confirm":
            values["confirm"] = False

    def validate(self, values):
        result = super().validate(values)
        if not result["target"]:
            raise ValueError("No eligible drives. Connect or unmount one, then reopen.")
        if not result["confirm"]:
            raise ValueError("Confirm permanent data loss before formatting.")
        return result

    def submitted(self, values):
        values["confirm"] = False


def setup():
    targets = discover()
    if not targets:
        return FormatForm([
            Choice("target", "Drive", [("No eligible drives", "")], "",
                   help="Connect or unmount a USB drive or SD card. "
                        "Press B and reopen to refresh."),
        ], submit_label="No drives available")
    choices = [(_label(target), target.token()) for target in targets]
    return FormatForm([
        Choice("target", "Drive / partition", choices, choices[0][1],
               help="Check device name and capacity. Formatting erases data."),
        Choice("filesystem", "Format", [(name, name) for name in FORMATS], "FAT32"),
        Choice("table", "Partition table", [("MBR", "msdos"), ("GPT", "gpt")], "msdos"),
        Toggle("confirm", "Erase data?", help="Yes permanently erases the selected target."),
    ], submit_label="Format",
       visible_when={"table": lambda values: json.loads(values["target"])[0]
                     == json.loads(values["target"])[1]})


def _token_target(token):
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


def _check_safe(target):
    current = _token_target(target.token())
    if current.disk_dev in _system_disk_devices():
        raise ValueError("Cannot format the disk backing the running system.")
    _check_disk_busy(current.disk, _mounted_devices())
    info = os.stat(DEV / current.name)
    if not stat.S_ISBLK(info.st_mode):
        raise ValueError("Target is not a block device.")
    if f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != current.device_dev:
        raise ValueError("Device node no longer matches the selected target.")
    if not os.access(DEV / current.name, os.W_OK):
        raise ValueError("No write permission for selected device.")
    return current


def _run(ctx, args):
    result = ctx.run(args, log=False, env={"LC_ALL": "C", "LANG": "C"})
    if result.returncode:
        detail = " ".join(line.strip() for line in result.lines if line.strip())
        raise ValueError(f"{Path(args[0]).name} failed ({result.returncode}): "
                         f"{detail or 'no diagnostic output'}")


def format_target(ctx, selected, filesystem, table):
    if filesystem not in FORMATS or (selected.whole and table not in ("msdos", "gpt")):
        raise ValueError("Invalid format or partition table.")
    if selected.whole and table == "msdos" and selected.sectors > 0xFFFFFFFF:
        raise ValueError("Drive is too large for MBR; choose GPT.")
    required = [FORMATS[filesystem][0], *(['parted', 'partprobe'] if selected.whole else [])]
    tools = {}
    for name in required:
        tools[name] = shutil.which(name)
        if not tools[name]:
            raise ValueError(f"Required tool not found: {name}")
    _check_safe(selected)
    target_path = str(DEV / selected.name)
    if selected.whole:
        ctx.progress(None, "Creating partition table")
        _check_safe(selected)
        _run(ctx, [tools["parted"], "-s", target_path, "mklabel", table])
        for _ in range(50):
            _check_safe(selected)
            if not _partitions(SYS_BLOCK / selected.disk):
                break
            ctx.sleep(0.1)
        else:
            raise ValueError("Old partitions are still present; disk may be partially formatted.")
        _check_safe(selected)
        _run(ctx, [tools["parted"], "-s", target_path, "mkpart", "primary",
                   PARTED_TYPES[filesystem], "1MiB", "100%"])
        _run(ctx, [tools["partprobe"], target_path])
        partition = None
        for _ in range(50):
            _check_safe(selected)
            names = _partitions(SYS_BLOCK / selected.disk)
            if len(names) == 1 and _number(SYS_BLOCK / names[0] / "partition") == 1:
                candidate = _target(selected.disk, names[0])
                remaining = selected.sectors - candidate.start - candidate.sectors
                if candidate.start < 2048 or not 0 <= remaining <= 32768:
                    raise ValueError("New partition does not match the requested layout.")
                partition = candidate
                break
            ctx.sleep(0.1)
        if partition is None:
            raise ValueError("New partition did not appear; disk may be partially formatted.")
        selected = partition
        target_path = str(DEV / selected.name)
    ctx.progress(None, f"Formatting {selected.name} as {filesystem}")
    _check_safe(selected)
    _run(ctx, [tools[FORMATS[filesystem][0]], *FORMATS[filesystem][1:], target_path])
    ctx.progress(1, "Format complete")
    ctx.log(f"Formatted {selected.name} as {filesystem}.", color="ok")


@app(name="Format drive", group="Removable Storage", setup=setup)
def format_drive(ctx):
    try:
        if ctx.params.get("confirm") is not True:
            raise ValueError("Formatting was not confirmed.")
        selected = _token_target(ctx.params["target"])
        format_target(ctx, selected, ctx.params["filesystem"], ctx.params.get("table"))
    except Cancelled:
        ctx.log("Formatting cancelled; the device may be partially formatted.", color="crit")
        raise
    except (OSError, ValueError, KeyError) as exc:
        ctx.status = "error"
        ctx.progress(0, "Format failed", text="Failed")
        ctx.log(f"{exc} The device may be partially formatted.", color="crit")
