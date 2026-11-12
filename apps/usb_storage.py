"""USB mass-storage and CD-ROM gadget apps."""

import errno
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import tempfile
import uuid

from deckfw import app, Choice, Form
from apps import _usb_otg


SERVICE = "usb-massstorage"
CONFIG = Path("/etc/default/usb-massstorage")
GIB = 1024 ** 3
PARTITION_START = 1024 ** 2
FORMATS = {
    "FAT32": ("fat32", ["mkfs.fat", "-F", "32", "-I", "-h",
                        str(PARTITION_START // 512)]),
    "exFAT": ("ntfs", ["mkfs.exfat"]),
    "ext4": ("ext4", ["mkfs.ext4", "-F"]),
    "NTFS": ("ntfs", ["mkfs.ntfs", "-F", "-Q", "-p",
                      str(PARTITION_START // 512)]),
}


def invoking_user():
    if os.geteuid() == 0 and "SUDO_UID" in os.environ:
        return pwd.getpwuid(int(os.environ["SUDO_UID"]))
    return None


def directory(kind):
    user = invoking_user()
    home = Path(user.pw_dir) if user is not None else Path.home()
    return home / kind


def choices(kind, suffix):
    folder = directory(kind)
    if not folder.exists():
        return [("No files; reopen after adding one", "")]
    files = sorted((p for p in folder.iterdir()
                    if p.suffix.lower() == suffix and not p.is_symlink() and p.is_file()),
                   key=lambda p: p.name.lower())
    return [(p.name, p.name) for p in files] or [("No files; reopen after adding one", "")]


class SelectionForm(Form):
    def validate(self, values):
        result = super().validate(values)
        if "image" in result and not result["image"]:
            raise ValueError("No image available. Add one and reopen the form.")
        if "iso" in result and not result["iso"]:
            raise ValueError("No ISO available. Add one and reopen the form.")
        return result


def storage_setup():
    images = choices("Images", ".img")
    return SelectionForm([
        Choice("action", "Image", [("Existing", "existing"), ("Create new", "new")],
               "existing" if images[0][1] else "new"),
        Choice("image", "Existing image", images, images[0][1]),
        Choice("size", "Size", [(f"{n} GB", n) for n in (1, 4, 8, 16, 32)], 1),
        Choice("filesystem", "Format", [(name, name) for name in FORMATS], "FAT32"),
    ], submit_label="Start", visible_when={
        "image": lambda values: values["action"] == "existing",
        "size": lambda values: values["action"] == "new",
        "filesystem": lambda values: values["action"] == "new",
    })


def cdrom_setup():
    images = choices("ISO", ".iso")
    return SelectionForm([Choice("iso", "ISO", images, images[0][1])],
                         submit_label="Start")


def selected_file(kind, name, suffix):
    if not name or Path(name).name != name or Path(name).suffix.lower() != suffix:
        raise ValueError("Invalid selected file.")
    folder = directory(kind).resolve(strict=True)
    path = folder / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError(f"{name} must be a non-empty regular file.")
    return path, info.st_size


def run_tool(ctx, args, *, log=True):
    path = os.pathsep.join([os.environ.get("PATH", os.defpath), "/usr/sbin", "/sbin"])
    result = ctx.run(args, log=log, env={"PATH": path})
    if result.returncode:
        detail = f": {result.lines[-1]}" if result.lines else ""
        raise RuntimeError(f"{args[0]} failed ({result.returncode}){detail}")
    return result


def copy_sparse(ctx, source, destination, offset, length):
    with open(source, "rb") as src, open(destination, "r+b") as dst:
        position = 0
        sparse = hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE")
        while position < length:
            ctx.check()
            if sparse:
                try:
                    position = os.lseek(src.fileno(), position, os.SEEK_DATA)
                    end = min(os.lseek(src.fileno(), position, os.SEEK_HOLE), length)
                except OSError as exc:
                    if exc.errno == errno.ENXIO:
                        break
                    if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                        raise
                    sparse = False
                    continue
            else:
                end = length
            src.seek(position)
            dst.seek(offset + position)
            while position < end:
                ctx.check()
                data = src.read(min(4 * 1024 * 1024, end - position))
                if not data:
                    raise ValueError("Filesystem image ended unexpectedly.")
                dst.write(data)
                position += len(data)


def create_image(ctx, size_gib, filesystem):
    if size_gib not in (1, 4, 8, 16, 32) or filesystem not in FORMATS:
        raise ValueError("Invalid image size or filesystem.")
    folder = directory("Images")
    new_folder = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    user = invoking_user()
    if new_folder and user is not None:
        os.chown(folder, user.pw_uid, user.pw_gid)
    size = size_gib * GIB
    if shutil.disk_usage(folder).free < size:
        raise ValueError("Not enough free space for the requested image.")
    partition_type, formatter = FORMATS[filesystem]
    target = folder / f"usb-{uuid.uuid4().hex}.img"
    image = partition = None
    try:
        fd, name = tempfile.mkstemp(prefix=".usb-image-", suffix=".partial", dir=folder)
        os.close(fd)
        image = Path(name)
        fd, name = tempfile.mkstemp(prefix=".usb-partition-", suffix=".partial", dir=folder)
        os.close(fd)
        partition = Path(name)
        ctx.progress(None, "Allocating image")
        run_tool(ctx, ["fallocate", "-l", str(size), str(image)])
        run_tool(ctx, ["parted", "-s", str(image), "mklabel", "msdos", "mkpart",
                       "primary", partition_type, "1MiB", "100%"])
        output = run_tool(ctx, ["parted", "-m", "-s", str(image), "unit", "B", "print"],
                          log=False)
        lines = [line for line in output.lines if line.startswith("1:")]
        if len(lines) != 1:
            raise ValueError("Could not verify the new partition layout.")
        fields = lines[0].split(":")
        if len(fields) < 4 or not all(re.fullmatch(r"\d+B", field)
                                      for field in fields[1:4]):
            raise ValueError("Could not parse the new partition layout.")
        start, end, length = (int(field[:-1]) for field in fields[1:4])
        if start != PARTITION_START or end + 1 != size or length != end - start + 1:
            raise ValueError("New partition does not match the requested image size.")
        with open(partition, "r+b") as file:
            file.truncate(length)
        ctx.progress(None, f"Formatting {filesystem}")
        run_tool(ctx, [*formatter, str(partition)])
        if partition.stat().st_size != length:
            raise ValueError("Formatter changed the partition image size.")
        copy_sparse(ctx, partition, image, start, length)
        ctx.check()
        if user is not None:
            os.chown(image, user.pw_uid, user.pw_gid)
        os.link(image, target)
        ctx.log(f"Created {target.name}.", color="ok")
        return target, size
    finally:
        if partition is not None:
            partition.unlink(missing_ok=True)
        if image is not None:
            image.unlink(missing_ok=True)


def write_config(mode, image, size=None):
    values = {"USB_MASS_STORAGE_MODE": mode, "USB_MASS_STORAGE_IMAGE": str(image)}
    if size is not None:
        values["USB_MASS_STORAGE_SIZE"] = str(size)
    if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for value in values.values()):
        raise ValueError("Configuration values cannot contain control characters.")
    info = CONFIG.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("The mass-storage configuration must be a regular file.")
    original = CONFIG.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=")
    output = []
    for line in lines:
        match = pattern.match(line)
        if match and match.group(1) in values:
            if line.rstrip().endswith("\\"):
                raise ValueError("Cannot safely replace a continued configuration assignment.")
            try:
                shlex.split(line[match.end():], comments=True)
            except ValueError as exc:
                raise ValueError("Cannot safely replace a multiline configuration assignment.") from exc
            continue
        output.append(line)
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    def quote(value):
        if re.fullmatch(r"[a-zA-Z0-9_@%+=:,./-]+", value):
            return value
        escaped = re.sub(r'([\\\"$`])', r"\\\1", value)
        return f'"{escaped}"'

    output.extend(f"{key}={quote(value)}\n" for key, value in values.items())
    fd, name = tempfile.mkstemp(prefix=".usb-massstorage-", dir=CONFIG.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            if (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid()):
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            stream.writelines(output)
            stream.flush()
            os.fsync(stream.fileno())
        current = CONFIG.lstat()
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
                info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size):
            raise ValueError("Mass-storage configuration changed; retry.")
        os.replace(temporary, CONFIG)
    finally:
        temporary.unlink(missing_ok=True)


@app(name="USB Storage", group="USB-OTG", icon="icons/usb-drive.png",
     setup=storage_setup, shutdown=_usb_otg.shutdown)
def usb_storage(ctx):
    _usb_otg.check_inactive(ctx, SERVICE)
    if ctx.params["action"] == "new":
        image, _ = create_image(ctx, ctx.params["size"], ctx.params["filesystem"])
        size = f'{ctx.params["size"]}G'
    elif ctx.params["action"] == "existing":
        image, size = selected_file("Images", ctx.params["image"], ".img")
    else:
        raise ValueError("Invalid image selection.")
    ctx.check()
    _usb_otg.check_inactive(ctx, SERVICE)
    write_config("disk", image, size)
    _usb_otg.start(ctx, SERVICE)


@app(name="USB CD-ROM emulator", group="USB-OTG", icon="icons/usb-cdrom.png",
     setup=cdrom_setup,
     shutdown=_usb_otg.shutdown)
def usb_cdrom(ctx):
    _usb_otg.check_inactive(ctx, SERVICE)
    image, _ = selected_file("ISO", ctx.params["iso"], ".iso")
    ctx.check()
    _usb_otg.check_inactive(ctx, SERVICE)
    write_config("cdrom", image)
    _usb_otg.start(ctx, SERVICE)
