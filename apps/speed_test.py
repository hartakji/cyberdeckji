"""Measure the read and write speed of a USB drive or SD card."""

import errno
import os
import tempfile
import time

from deckfw import app, Cancelled, Graph, Stat
from apps import _removable_storage as storage

TEST_SIZE = 256 * storage.MIB
RESERVE = 16 * storage.MIB
MINIMUM_FREE = 32 * storage.MIB
MOUNTABLE = ("vfat", "exfat", "ext2", "ext3", "ext4", "ntfs")


def setup():
    targets = storage.discover(partitions=False)
    if not targets:
        return storage.empty_form()
    return storage.StorageForm([
        storage.drive_field(targets, help="Read test is raw and read-only. Write test mounts "
                                          "the first partition and uses a temporary file."),
    ], submit_label="Test")


def write_size(free):
    """Bytes to write for a given amount of free space; 0 means skip the test."""
    if free < MINIMUM_FREE:
        return 0
    size = min(TEST_SIZE, free - RESERVE)
    return size - size % storage.CHUNK


def _measure(ctx, graph, total, step, label):
    """Call step(bytes) repeatedly; push MiB/s per chunk and return the average."""
    done = 0
    started = time.monotonic()
    while done < total:
        ctx.check()
        size = min(storage.CHUNK, total - done)
        before = time.monotonic()
        moved = step(size)
        if moved <= 0:
            break
        done += moved
        graph.push(moved / storage.MIB / max(time.monotonic() - before, 1e-6))
        ctx.progress(done / total, label, text=f"{done // storage.MIB} / {total // storage.MIB} MiB")
    return done, done / storage.MIB / max(time.monotonic() - started, 1e-6)


def read_test(ctx, selected, graph):
    total = min(TEST_SIZE, selected.size)
    total -= total % 4096
    try:
        fd = storage.open_device(selected, direct=True)
    except OSError as exc:
        if exc.errno != errno.EINVAL:
            raise
        fd = storage.open_device(selected)
    buffer = storage.aligned_buffer()
    try:
        view = memoryview(buffer)
        try:
            return _measure(ctx, graph, total, lambda n: os.readv(fd, [view[:n]]),
                            f"Reading {selected.name}")[1]
        finally:
            view.release()
    finally:
        os.close(fd)
        buffer.close()


def _open_test_file(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    direct = getattr(os, "O_DIRECT", 0)
    if direct:
        try:
            return os.open(path, flags | direct, 0o600)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
    return os.open(path, flags | getattr(os, "O_DSYNC", 0), 0o600)


def write_test(ctx, selected, graph):
    """Mount the first supported partition, write a temporary file, always unmount."""
    partition = None
    for name in storage._partitions(storage.SYS_BLOCK / selected.disk):
        if storage.fs_type(name) in MOUNTABLE:
            partition = storage._target(selected.disk, name)
            break
    if partition is None:
        ctx.log("Write test skipped: no mountable partition.", color="warn")
        return None
    tools = storage.require_tools("mount", "umount")
    storage.check_safe(partition)
    mount_point = tempfile.mkdtemp(prefix="deck-speed-")
    mounted = False
    path = os.path.join(mount_point, ".deck-speed-test")
    try:
        storage.run_tool(ctx, [tools["mount"], "-o", "noatime",
                               str(storage.DEV / partition.name), mount_point])
        mounted = True
        info = os.statvfs(mount_point)
        size = write_size(info.f_bavail * info.f_frsize)
        if not size:
            ctx.log(f"Write test skipped: less than {MINIMUM_FREE // storage.MIB} MiB free "
                    f"on {partition.name}.", color="warn")
            return None
        fd = _open_test_file(path)
        buffer = storage.aligned_buffer()
        buffer.write(os.urandom(storage.CHUNK))
        view = memoryview(buffer)
        try:
            started = time.monotonic()
            done, _ = _measure(ctx, graph, size, lambda n: os.write(fd, view[:n]),
                               f"Writing to {partition.name}")
            os.fsync(fd)
            return done / storage.MIB / max(time.monotonic() - started, 1e-6)
        finally:
            os.close(fd)
            view.release()
            buffer.close()
    finally:
        if os.path.exists(path):
            os.unlink(path)
        if mounted:
            result = storage.run_tool(ctx, [tools["umount"], mount_point], check=False)
            if result.returncode:
                ctx.log(f"Could not unmount {mount_point}; unmount it before removing the "
                        "drive.", color="crit")
                mount_point = None
        if mount_point:
            os.rmdir(mount_point)


@app(name="Speed test", group="Removable Storage", icon="icons/speed.png", setup=setup)
def speed_test(ctx):
    read_stat, write_stat = Stat("Read", "-"), Stat("Write", "-")
    ctx.row(read_stat, write_stat)
    read_graph = ctx.add(Graph(title="Read", unit=" MiB/s", fmt="{:.1f}"))
    write_graph = ctx.add(Graph(title="Write", unit=" MiB/s", fmt="{:.1f}"))
    try:
        selected = storage.token_target(ctx.params["target"])
        read_speed = read_test(ctx, selected, read_graph)
        read_stat.value = f"{read_speed:.1f} MiB/s"
        write_speed = write_test(ctx, selected, write_graph)
        write_stat.value = f"{write_speed:.1f} MiB/s" if write_speed else "skipped"
        ctx.progress(1, "Speed test complete", text="Done")
        ctx.log(f"Read {read_speed:.1f} MiB/s, write "
                f"{f'{write_speed:.1f} MiB/s' if write_speed else 'skipped'}.", color="ok")
    except Cancelled:
        ctx.log("Speed test cancelled.", color="crit")
        raise
    except (OSError, ValueError, KeyError) as exc:
        ctx.status = "error"
        ctx.progress(0, "Speed test failed", text="Failed")
        ctx.log(str(exc), color="crit")
