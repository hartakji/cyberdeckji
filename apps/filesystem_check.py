"""Check, and optionally repair, a FAT, ext or NTFS partition."""

import json

from deckfw import app, Cancelled, Choice, Toggle
from apps import _removable_storage as storage

TOOLS = {"vfat": "fsck.fat", "ext2": "e2fsck", "ext3": "e2fsck", "ext4": "e2fsck",
         "ntfs": "ntfsfix"}
ARGS = {("fsck.fat", "check"): ["-n"], ("fsck.fat", "repair"): ["-a", "-w"],
        ("e2fsck", "check"): ["-f", "-n"], ("e2fsck", "repair"): ["-f", "-y"],
        ("ntfsfix", "check"): ["-n"], ("ntfsfix", "repair"): []}


class CheckForm(storage.StorageForm):
    confirm_message = "Confirm before repairing the filesystem."


def discover():
    found = []
    for target in storage.discover():
        if target.whole:
            continue
        kind = storage.fs_type(target.name)
        if kind in TOOLS:
            found.append((target, kind))
    return found


def setup():
    partitions = discover()
    if not partitions:
        return storage.empty_form()
    choices = [(f"{storage.label(target)} {storage.FS_NAMES[kind]}",
                json.dumps([target.token(), kind])) for target, kind in partitions]
    return CheckForm([
        Choice("target", "Partition", choices, choices[0][1],
               help="Unmounted FAT, ext2/3/4 and NTFS partitions."),
        Choice("mode", "Mode", [("Check only", "check"), ("Check and repair", "repair")],
               "check", help="Check only never changes the partition."),
        Toggle("confirm", "Repair?", help="Repairs can move or delete damaged files."),
    ], submit_label="Check", visible_when={"confirm": lambda values: values["mode"] == "repair"})


def verdict(tool, mode, code):
    """Return (message, color) for a checker exit code."""
    if code == 0:
        return "Filesystem is clean.", "ok"
    if tool == "e2fsck":
        if code & 8:
            return f"e2fsck could not run (exit {code}).", "crit"
        if code & 4:
            return f"Errors remain (exit {code}).", "crit"
        if mode == "repair" and code & 3:
            return "Errors were fixed.", "ok"
        return f"Errors found (exit {code}); run Check and repair.", "crit"
    if mode == "repair" and tool == "fsck.fat" and code == 1:
        return "Errors were found and fixed.", "ok"
    if mode == "check":
        return f"Errors found (exit {code}); run Check and repair.", "crit"
    return f"Repair failed or errors remain (exit {code}).", "crit"


def check(ctx, selected, kind, mode):
    if kind not in TOOLS or mode not in ("check", "repair"):
        raise ValueError("Unsupported filesystem or mode.")
    tool = TOOLS[kind]
    path = storage.require_tools(tool)[tool]
    storage.check_safe(selected, write=mode == "repair")
    ctx.progress(None, f"{'Repairing' if mode == 'repair' else 'Checking'} {selected.name}")
    result = storage.run_tool(ctx, [path, *ARGS[(tool, mode)], str(storage.DEV / selected.name)],
                              check=False, log=True)
    message, color = verdict(tool, mode, result.returncode)
    ctx.progress(1 if color == "ok" else 0, message, text="Done" if color == "ok" else "Problems")
    ctx.log(message, color=color)
    if color != "ok":
        ctx.status = "error"
    return message


@app(name="Filesystem check", group="Removable Storage", icon="icons/disk-check.png", setup=setup)
def filesystem_check(ctx):
    try:
        token, kind = json.loads(ctx.params["target"])
        mode = ctx.params["mode"]
        if mode == "repair" and ctx.params.get("confirm") is not True:
            raise ValueError("Repair was not confirmed.")
        selected = storage.token_target(token)
        if selected.whole or storage.fs_type(selected.name) != kind:
            raise ValueError("Partition changed; return to setup and select it again.")
        check(ctx, selected, kind, mode)
    except Cancelled:
        ctx.log("Check cancelled.", color="crit")
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        ctx.status = "error"
        ctx.progress(0, "Check failed", text="Failed")
        ctx.log(str(exc), color="crit")
