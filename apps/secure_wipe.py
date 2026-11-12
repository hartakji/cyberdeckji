"""Erase a USB drive or SD card: quick, scramble or full zero."""

import os
import random

from deckfw import app, Cancelled, Choice, Toggle
from apps import _removable_storage as storage

EDGE = storage.MIB
SCRAMBLE_EDGE = 64 * storage.MIB
STRETCH = 64 * storage.MIB


class WipeForm(storage.StorageForm):
    confirm_message = "Confirm permanent data loss before wiping."


def setup():
    targets = storage.discover(partitions=False)
    if not targets:
        return storage.empty_form()
    return WipeForm([
        storage.drive_field(targets, help="The whole drive is wiped."),
        Choice("mode", "Mode", [("Quick", "quick"), ("Scramble", "scramble"),
                                ("Full zero", "zero")], "quick",
               help="Quick: erase signatures (seconds). Scramble: also overwrite ~2% with random "
                    "data. Full zero: overwrite everything. Flash wear leveling means no "
                    "software wipe is guaranteed."),
        Toggle("confirm", "Erase data?", help="Yes permanently erases the drive."),
    ], submit_label="Wipe")


def scramble_offsets(size, rng=random):
    """Start of one random 1 MiB block in every 64 MiB stretch past the edges."""
    offsets = []
    start = min(SCRAMBLE_EDGE, size)
    end = max(start, size - SCRAMBLE_EDGE)
    for stretch in range(start, end, STRETCH):
        room = min(STRETCH, end - stretch) - EDGE
        if room < 0:
            continue
        offsets.append(stretch + rng.randrange(0, room // 4096 + 1) * 4096)
    return offsets


def _overwrite(ctx, fd, offset, length, data_fn):
    end = offset + length
    while offset < end:
        ctx.check()
        size = min(storage.CHUNK, end - offset)
        storage.write_all(fd, data_fn(size), offset)
        offset += size


def wipe(ctx, selected, mode):
    if mode not in ("quick", "scramble", "zero"):
        raise ValueError("Invalid wipe mode.")
    tools = storage.require_tools("wipefs")
    storage.check_safe(selected)
    device = str(storage.DEV / selected.name)
    ctx.progress(None, "Erasing signatures")
    for name in [*storage._partitions(storage.SYS_BLOCK / selected.disk)]:
        storage.check_safe(selected)
        storage.run_tool(ctx, [tools["wipefs"], "-a", str(storage.DEV / name)])
    storage.check_safe(selected)
    storage.run_tool(ctx, [tools["wipefs"], "-a", device])
    size = selected.size
    zeros = bytes(storage.CHUNK)
    fd = storage.open_device(selected, write=True)
    try:
        edge = min(EDGE, size)
        _overwrite(ctx, fd, 0, edge, lambda n: zeros[:n])
        _overwrite(ctx, fd, size - edge, edge, lambda n: zeros[:n])
        if mode == "scramble":
            edge = min(SCRAMBLE_EDGE, size)
            ctx.progress(0, "Scrambling")
            _overwrite(ctx, fd, 0, edge, os.urandom)
            _overwrite(ctx, fd, size - edge, edge, os.urandom)
            offsets = scramble_offsets(size)
            for index, offset in enumerate(offsets, 1):
                _overwrite(ctx, fd, offset, EDGE, os.urandom)
                ctx.progress(index / len(offsets), "Scrambling",
                             text=f"{index} / {len(offsets)} blocks")
        elif mode == "zero":
            done = 0
            while done < size:
                ctx.check()
                chunk = min(storage.CHUNK, size - done)
                storage.write_all(fd, zeros[:chunk], done)
                done += chunk
                ctx.progress(done / size, "Writing zeros",
                             text=f"{done * 100 // size}% · {done // storage.MIB} MiB")
        storage.sync_device(ctx, fd, selected.name)
    finally:
        os.close(fd)
    ctx.progress(1, "Wipe complete", text="Done")
    ctx.log(f"Wiped {selected.name} ({mode}).", color="ok")


@app(name="Secure wipe", group="Removable Storage", icon="icons/disk-wipe.png", setup=setup)
def secure_wipe(ctx):
    try:
        if ctx.params.get("confirm") is not True:
            raise ValueError("Wipe was not confirmed.")
        selected = storage.token_target(ctx.params["target"])
        if not selected.whole:
            raise ValueError("Select a whole drive.")
        wipe(ctx, selected, ctx.params["mode"])
    except Cancelled:
        ctx.log("Wipe cancelled; the drive is partially erased.", color="crit")
        raise
    except (OSError, ValueError, KeyError) as exc:
        ctx.status = "error"
        ctx.progress(0, "Wipe failed", text="Failed")
        ctx.log(f"{exc} The drive may be partially erased.", color="crit")
