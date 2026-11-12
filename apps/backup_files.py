"""Browse a read-only removable partition and back up selected files."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import stat
import tempfile
import time

from deckfw import app, Cancelled, Choice, Form, Label, ListView, Widget
from apps import _removable_storage as storage


MOUNT_OPTIONS = {
    "vfat": ("vfat", ""),
    "exfat": ("exfat", ""),
    "ext2": ("ext2", ""),
    "ext3": ("ext3", ",noload"),
    "ext4": ("ext4", ",noload"),
    "ntfs": ("ntfs3", ",norecover"),
    "ntfs3": ("ntfs3", ",norecover"),
}


class BackupForm(Form):
    def __init__(self, targets):
        self.drives = [target for target in targets if target.whole]
        self.partitions = [target for target in targets if not target.whole]
        drive = storage.drive_field(self.drives)
        choices = self.partition_choices(drive.default)
        super().__init__([
            drive,
            Choice("partition", "Partition", choices, choices[0][1],
                   help="Browse read-only; select multiple files or folders."),
        ], submit_label="Browse")

    def partition_choices(self, token):
        disk = next((drive.disk for drive in self.drives if drive.token() == token), None)
        return [(storage.label(part), part.token()) for part in self.partitions
                if part.disk == disk] or [("No eligible partitions", "")]

    def changed(self, values, key):
        if key == "target":
            field = self.fields[1]
            field.choices = self.partition_choices(values["target"])
            values["partition"] = field.choices[0][1]

    def validate(self, values):
        result = super().validate(values)
        if not result["target"]:
            raise ValueError("No eligible drives. Connect or unmount one, then reopen.")
        if not result["partition"]:
            raise ValueError("No eligible partitions on this drive. Choose another drive.")
        if result["partition"] not in {value for _, value in
                                       self.partition_choices(result["target"])}:
            raise ValueError("Partition does not belong to the selected drive.")
        return result


def setup():
    return BackupForm(storage.discover())


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def mount_records():
    records = []
    for line in (storage.PROC / "self" / "mountinfo").read_text(
            encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            raise ValueError("Cannot verify mount information.")
        records.append((fields[0], fields[2], _unescape(fields[4]), fields[5].split(",")))
    if not records:
        raise ValueError("Cannot verify empty mount information.")
    return records


class Partition:
    def __init__(self, target):
        self.target = target
        self.path = None
        self.tools = {}
        self.mount_id = None
        self.device = None
        self.nested = set()
        self.last_check = 0
        self.partial = None

    def mount(self, ctx):
        self.tools = storage.require_tools("mount", "umount", "blkid", "timeout")
        storage.check_safe(self.target, write=False)
        result = storage.run_tool(ctx, [self.tools["blkid"], "-p", "-o", "value",
                                        "-s", "TYPE", str(storage.DEV / self.target.name)])
        filesystem = "".join(result.lines).strip()
        if filesystem not in MOUNT_OPTIONS:
            raise ValueError(f"Safe read-only mounting is not supported for {filesystem or 'unknown filesystem'}.")
        driver, extra = MOUNT_OPTIONS[filesystem]
        storage.check_safe(self.target, write=False)
        self.path = Path(tempfile.mkdtemp(prefix="deck-backup-"))
        ctx.progress(None, f"Mounting {self.target.name} read-only")
        storage.run_tool(ctx, [self.tools["mount"], "-t", driver, "-o",
                               "ro,nodev,nosuid,noexec" + extra,
                               str(storage.DEV / self.target.name), str(self.path)])
        self.verify(force=True)

    def verify(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_check < 0.25:
            return
        storage.token_target(self.target.token())
        records = mount_records()
        matches = [record for record in records if record[2] == str(self.path)]
        if len(matches) != 1 or matches[0][1] != self.target.device_dev:
            raise ValueError("Source partition is no longer mounted at its owned mount point.")
        record = matches[0]
        if "ro" not in record[3] or (self.mount_id is not None and record[0] != self.mount_id):
            raise ValueError("Source mount changed or is not read-only.")
        self.mount_id = record[0]
        info = self.path.stat()
        if f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}" != self.target.device_dev:
            raise ValueError("Source filesystem no longer matches the selected partition.")
        self.device = info.st_dev
        self.nested = {PurePosixPath(record[2]).relative_to(str(self.path))
                       for record in records
                       if record[2].startswith(str(self.path) + "/")}
        self.last_check = now

    def open_directory(self, relative):
        self.verify()
        parts = PurePosixPath(relative).parts
        if PurePosixPath(relative).is_absolute() or ".." in parts:
            raise ValueError("Path must stay inside the selected partition.")
        if any(mount == PurePosixPath(relative) or mount in PurePosixPath(relative).parents
               for mount in self.nested):
            raise ValueError("Nested mounts cannot be backed up.")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self.path, flags)
        try:
            if os.fstat(fd).st_dev != self.device:
                raise ValueError("Source mount disappeared or was replaced.")
            for name in parts:
                child = os.open(name, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                if os.fstat(fd).st_dev != self.device:
                    raise ValueError("Path crosses into another filesystem.")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def close(self, ctx):
        if self.path is None:
            return
        records = [record for record in mount_records() if record[2] == str(self.path)]
        if records:
            if (len(records) != 1 or records[0][1] != self.target.device_dev
                    or (self.mount_id is not None and records[0][0] != self.mount_id)):
                raise ValueError(f"Mount changed at {self.path}; refusing to unmount a foreign mount.")
            ctx.log(f"Unmounting {self.path}")
            result = storage.run_tool(ctx, [self.tools["timeout"], "--kill-after=2s", "10s",
                                            self.tools["umount"], "--", str(self.path)],
                                      check=False)
            if result.returncode:
                raise ValueError(f"Cannot unmount {self.path}: "
                                 + (" ".join(result.lines[-8:]) or
                                    f"exit status {result.returncode}"))
            if any(record[2] == str(self.path) for record in mount_records()):
                raise ValueError(f"Partition is still mounted at {self.path}.")
        self.path.rmdir()
        self.path = None
        ctx.log("Source unmounted.", color="ok")


def shutdown(ctx):
    partition = getattr(ctx, "file_backup", None)
    if partition is not None:
        try:
            partition.close(ctx)
        except (OSError, ValueError):
            ctx.log(f"Cleanup failed at {partition.path}. Do not remove the drive; "
                    "unmount it manually before removing it.", color="crit")
            raise


@dataclass(frozen=True)
class Entry:
    path: PurePosixPath
    directory: bool


@dataclass(frozen=True)
class View:
    generation: int = 0
    path: PurePosixPath = PurePosixPath(".")
    entries: tuple = ()
    selected: frozenset = frozenset()
    message: str = ""
    active: bool = True


def selected_by(path, selected):
    return next((parent for parent in (path, *path.parents) if parent in selected), None)


class Browser(Widget):
    focusable = True

    def __init__(self):
        super().__init__(flex=1)
        self.view = View()
        self.requests = queue.Queue()
        self.list = ListView(on_select=self.activate)
        self._view = None

    @property
    def handles_back(self):
        return self.view.active

    @property
    def hints(self):
        return "A open  X/Right select  B up/exit"

    def sync(self):
        view = self.view
        if view is self._view:
            return
        old = self._view
        self._view = view
        self.list.items = [(f"Back up selected ({len(view.selected)})", None, "A")]
        for entry in view.entries:
            mark = "[x]" if selected_by(entry.path, view.selected) is not None else "[ ]"
            self.list.items.append((f"{mark} {entry.path.name}", entry,
                                    "DIR" if entry.directory else ""))
        if old is None or old.path != view.path:
            self.list.selected = self.list._top = 0
        self.list.selected = min(self.list.selected, len(self.list.items) - 1)

    def activate(self, entry):
        self.requests.put((self._view.generation, "backup" if entry is None else "open",
                           entry.path if entry else None))

    def action(self, name):
        self.sync()
        if not self.view.active:
            return False
        if name == "b":
            self.requests.put((self._view.generation, "back", None))
            return True
        if name in ("x", "right"):
            entry = self.list.items[self.list.selected][1]
            if entry is not None:
                self.requests.put((self._view.generation, "toggle", entry.path))
            return True
        return self.list.action(name)

    def draw(self, surf, rect, theme):
        self.sync()
        line = theme.f_small.get_linesize()
        pad = theme.pad
        Label("/" if self._view.path == PurePosixPath(".") else "/" + str(self._view.path),
              size="small", color="accent").draw(
                  surf, rect.__class__(rect.x, rect.y, rect.w, line), theme)
        self.list.draw(surf, rect.__class__(rect.x, rect.y + line + pad,
                                           rect.w, max(0, rect.h - 3 * line - 2 * pad)), theme)
        message = self._view.message or "Folders include all contents; X deselects."
        Label(message, size="small", color="warn" if self._view.message else "dim").draw(
            surf, rect.__class__(rect.x, rect.bottom - 2 * line, rect.w, line), theme)
        entry = self.list.items[self.list.selected][1]
        Label(entry.path.name if entry else "A: start backup",
              size="small").draw(
                  surf, rect.__class__(rect.x, rect.bottom - line, rect.w, line), theme)


def list_directory(ctx, partition, relative):
    fd = partition.open_directory(relative)
    try:
        result = []
        for name in os.listdir(fd):
            ctx.check()
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            result.append(Entry(relative / name, stat.S_ISDIR(info.st_mode)))
        return tuple(sorted(result, key=lambda entry: (not entry.directory,
                                                       entry.path.name.casefold())))
    finally:
        os.close(fd)


def choose_paths(ctx, partition, browser):
    path, selected, generation = PurePosixPath("."), set(), 0
    entries = list_directory(ctx, partition, path)
    message = ""
    while True:
        generation += 1
        browser.view = View(generation, path, entries, frozenset(selected), message)
        while True:
            ctx.check()
            partition.verify()
            try:
                version, action, target = browser.requests.get_nowait()
            except queue.Empty:
                ctx.sleep(0.05)
                continue
            if version == generation:
                break
        message = ""
        if action == "backup":
            if selected:
                browser.view = View(generation + 1, active=False)
                return normalize(selected)
            message = "Select at least one file or folder."
        elif action == "back":
            if path == PurePosixPath("."):
                return None
            path = path.parent
            entries = list_directory(ctx, partition, path)
        elif target in {entry.path for entry in entries}:
            entry = next(entry for entry in entries if entry.path == target)
            if action == "open" and entry.directory:
                try:
                    children = list_directory(ctx, partition, target)
                except (OSError, ValueError) as exc:
                    message = str(exc)
                else:
                    path, entries = target, children
            elif action in ("toggle", "open"):
                parent = selected_by(target, selected)
                if parent == target:
                    selected.remove(target)
                elif parent is not None:
                    message = f"Included by {parent}; deselect that folder first."
                else:
                    selected.add(target)


def normalize(paths):
    candidates = {PurePosixPath(path) for path in paths}
    if any(path.is_absolute() or ".." in path.parts or path == PurePosixPath(".")
           for path in candidates):
        raise ValueError("Invalid source selection.")
    return tuple(sorted((path for path in candidates
                         if not any(parent in candidates for parent in path.parents)),
                        key=str))


def walk_selection(ctx, partition, paths):
    stack = [(path, False) for path in reversed(paths)]
    while stack:
        ctx.check()
        partition.verify()
        path, after = stack.pop()
        if any(mount == path or mount in path.parents for mount in partition.nested):
            raise ValueError(f"Cannot back up a nested mount: {path}")
        fd = partition.open_directory(path.parent)
        try:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != partition.device:
                raise ValueError(f"Cannot cross into another filesystem: {path}")
            if stat.S_ISDIR(info.st_mode):
                if not after:
                    child = partition.open_directory(path)
                    try:
                        names = sorted(os.listdir(child))
                    finally:
                        os.close(child)
                    stack.append((path, True))
                    stack.extend((path / name, False) for name in reversed(names))
                yield path, info, "directory_end" if after else "directory"
            elif stat.S_ISREG(info.st_mode):
                yield path, info, "file"
            elif stat.S_ISLNK(info.st_mode):
                yield path, info, "link"
            else:
                raise ValueError(f"Unsupported special file: {path}")
        finally:
            os.close(fd)


def destination(partition):
    folder = storage.backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{storage.drive_label(partition.target.disk)}-{storage.timestamp()}"
    index = 0
    while True:
        suffix = f"-{index}" if index else ""
        final = folder / (name + suffix)
        partial = final.with_name(final.name + ".partial")
        if not final.exists():
            try:
                partial.mkdir(mode=0o700)
                return final, partial
            except FileExistsError:
                pass
        index += 1


def metadata(path, info, link=False):
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
    if not link:
        os.chmod(path, stat.S_IMODE(info.st_mode) & 0o777)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_selected(ctx, partition, paths):
    paths = normalize(paths)
    if not paths:
        raise ValueError("Select at least one file or folder.")
    ctx.progress(None, "Counting selected files")
    total = files = count = 0
    for _, info, kind in walk_selection(ctx, partition, paths):
        if kind != "directory_end":
            count += 1
        if kind == "file":
            total += info.st_size
            files += 1
    folder = storage.backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(folder).free < total + count * 4096 + storage.MIB:
        raise ValueError(f"Not enough free space in {folder} for {storage.human_size(total)}.")
    final, partial = destination(partition)
    partition.partial = partial
    copied = completed = 0
    started = last = time.monotonic()
    for path, info, kind in walk_selection(ctx, partition, paths):
        output = partial / path
        if kind == "directory":
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        if kind == "directory_end":
            sync_directory(output)
            metadata(output, info)
            continue
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = partition.open_directory(path.parent)
        try:
            if kind == "link":
                output.symlink_to(os.readlink(path.name, dir_fd=fd))
                metadata(output, info, link=True)
                continue
            source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=fd)
        finally:
            os.close(fd)
        with os.fdopen(source, "rb") as stream, open(output, "xb") as out:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                    info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                raise ValueError(f"Source file changed: {path}")
            moved = 0
            ctx.progress(copied / total if total else 0, str(path),
                         text=f"{copied} / {total} bytes")
            while True:
                ctx.check()
                partition.verify()
                data = stream.read(storage.CHUNK)
                if not data:
                    break
                out.write(data)
                moved += len(data)
                copied += len(data)
                if moved > info.st_size or copied > total:
                    raise ValueError(f"Source grew during backup: {path}")
                now = time.monotonic()
                if now - last >= 0.15 or copied == total:
                    speed = copied / storage.MIB / max(now - started, 1e-6)
                    ctx.progress(copied / total if total else 0, str(path),
                                 text=f"{copied} / {total} bytes | {speed:.1f} MiB/s")
                    last = now
            if moved != info.st_size:
                raise ValueError(f"Source ended early: {path}")
            out.flush()
            os.fsync(out.fileno())
        metadata(output, info)
        completed += 1
    if copied != total or completed != files:
        raise ValueError("Source contents changed; backup is incomplete.")
    ctx.check()
    partition.verify(force=True)
    sync_directory(partial)
    if final.exists():
        raise ValueError(f"Backup destination already exists: {final}")
    os.rename(partial, final)
    partition.partial = None
    try:
        sync_directory(final.parent)
    except OSError as exc:
        raise OSError(f"Backup is at {final}, but flushing its parent directory failed: {exc}") from exc
    ctx.progress(1, "Backup complete", text="Done")
    ctx.log(f"Saved {files} regular files ({count} entries), "
            f"{storage.human_size(total)} to {final}", color="ok")
    return final


@app(name="Backup files", group="Removable Storage", icon="icons/disk-backup.png",
     setup=setup, shutdown=shutdown)
def backup_files(ctx):
    partition = None
    try:
        drive = storage.token_target(ctx.params["target"])
        target = storage.token_target(ctx.params["partition"])
        if not drive.whole or target.whole or target.disk != drive.disk:
            raise ValueError("Choose a partition belonging to the selected drive.")
        partition = ctx.file_backup = Partition(target)
        partition.mount(ctx)
        ctx.clear()
        browser = ctx.add(Browser())
        paths = choose_paths(ctx, partition, browser)
        ctx.clear()
        if paths is None:
            ctx.log("Browser closed without creating a backup.")
        else:
            copy_selected(ctx, partition, paths)
    except Cancelled:
        ctx.clear()
        ctx.log("File backup cancelled.", color="warn")
        if partition is not None and partition.partial is not None:
            ctx.log(f"Incomplete backup retained at {partition.partial}", color="warn")
        raise
    except (OSError, ValueError, KeyError) as exc:
        ctx.clear()
        ctx.status = "error"
        ctx.log(f"File backup failed: {exc}", color="crit")
        if partition is not None and partition.partial is not None:
            ctx.log(f"Incomplete backup retained at {partition.partial}", color="warn")
