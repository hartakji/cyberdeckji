"""Format explicitly selected USB storage or SD card partitions on Linux."""

import json
import sys

from deckfw import app, Cancelled, Choice, Toggle
from apps import _removable_storage as storage


FORMATS = {"FAT32": ("mkfs.fat", "-F", "32"),
           "ext4": ("mkfs.ext4", "-F"),
           "NTFS": ("mkfs.ntfs", "-f")}
PARTED_TYPES = {"FAT32": "fat32", "ext4": "ext4", "NTFS": "ntfs"}


def discover():
    if sys.platform != "linux":
        raise ValueError("Formatting removable storage requires Linux.")
    return storage.discover()


class FormatForm(storage.StorageForm):
    confirm_message = "Confirm permanent data loss before formatting."


def setup():
    targets = discover()
    if not targets:
        return storage.empty_form()
    return FormatForm([
        storage.drive_field(targets, "Drive / partition",
                            "Check device name and capacity. Formatting erases data."),
        Choice("filesystem", "Format", [(name, name) for name in FORMATS], "FAT32"),
        Choice("table", "Partition table", [("MBR", "msdos"), ("GPT", "gpt")], "msdos"),
        Toggle("confirm", "Erase data?", help="Yes permanently erases the selected target."),
    ], submit_label="Format",
       visible_when={"table": lambda values: json.loads(values["target"])[0]
                     == json.loads(values["target"])[1]})


def format_target(ctx, selected, filesystem, table):
    if filesystem not in FORMATS or (selected.whole and table not in ("msdos", "gpt")):
        raise ValueError("Invalid format or partition table.")
    if selected.whole and table == "msdos" and selected.sectors > 0xFFFFFFFF:
        raise ValueError("Drive is too large for MBR; choose GPT.")
    tools = storage.require_tools(FORMATS[filesystem][0],
                                  *(["parted", "partprobe"] if selected.whole else []))
    storage.check_safe(selected)
    target_path = str(storage.DEV / selected.name)
    if selected.whole:
        ctx.progress(None, "Creating partition table")
        storage.check_safe(selected)
        storage.run_tool(ctx, [tools["parted"], "-s", target_path, "mklabel", table])
        for _ in range(50):
            storage.check_safe(selected)
            if not storage._partitions(storage.SYS_BLOCK / selected.disk):
                break
            ctx.sleep(0.1)
        else:
            raise ValueError("Old partitions are still present; disk may be partially formatted.")
        storage.check_safe(selected)
        storage.run_tool(ctx, [tools["parted"], "-s", target_path, "mkpart", "primary",
                   PARTED_TYPES[filesystem], "1MiB", "100%"])
        storage.run_tool(ctx, [tools["partprobe"], target_path])
        partition = None
        for _ in range(50):
            storage.check_safe(selected)
            names = storage._partitions(storage.SYS_BLOCK / selected.disk)
            if len(names) == 1 and storage._number(storage.SYS_BLOCK / names[0] / "partition") == 1:
                candidate = storage._target(selected.disk, names[0])
                remaining = selected.sectors - candidate.start - candidate.sectors
                if candidate.start < 2048 or not 0 <= remaining <= 32768:
                    raise ValueError("New partition does not match the requested layout.")
                partition = candidate
                break
            ctx.sleep(0.1)
        if partition is None:
            raise ValueError("New partition did not appear; disk may be partially formatted.")
        selected = partition
        target_path = str(storage.DEV / selected.name)
    ctx.progress(None, f"Formatting {selected.name} as {filesystem}")
    storage.check_safe(selected)
    storage.run_tool(ctx, [tools[FORMATS[filesystem][0]], *FORMATS[filesystem][1:], target_path])
    ctx.progress(1, "Format complete")
    ctx.log(f"Formatted {selected.name} as {filesystem}.", color="ok")


@app(name="Format drive", group="Removable Storage", icon="icons/disk-format.png", setup=setup)
def format_drive(ctx):
    try:
        if ctx.params.get("confirm") is not True:
            raise ValueError("Formatting was not confirmed.")
        selected = storage.token_target(ctx.params["target"])
        format_target(ctx, selected, ctx.params["filesystem"], ctx.params.get("table"))
    except Cancelled:
        ctx.log("Formatting cancelled; the device may be partially formatted.", color="crit")
        raise
    except (OSError, ValueError, KeyError) as exc:
        ctx.status = "error"
        ctx.progress(0, "Format failed", text="Failed")
        ctx.log(f"{exc} The device may be partially formatted.", color="crit")
