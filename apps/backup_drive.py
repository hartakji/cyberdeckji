"""Save a whole USB drive or SD card as an image in ~/Backups."""

import gzip
import json
import os
import shutil

from deckfw import app, Cancelled, Choice
from apps import _removable_storage as storage

FORMATS = {"raw": ".img", "gzip": ".img.gz"}


def setup():
    targets = storage.discover(partitions=False)
    if not targets:
        return storage.empty_form()
    return storage.StorageForm([
        storage.drive_field(targets, help="Only unmounted drives are listed."),
        Choice("format", "Image", [("Raw .img", "raw"), ("Compressed .img.gz", "gzip")], "raw",
               help="Raw images can be mounted or used with Ventoy. "
                    "Compressed images are smaller but slower to create."),
    ], submit_label="Backup")


def image_path(selected, kind):
    name = f"{storage.drive_label(selected.disk)}-{storage.timestamp()}{FORMATS[kind]}"
    return storage.backup_dir() / name


def backup(ctx, selected, kind):
    if kind not in FORMATS:
        raise ValueError("Invalid image format.")
    folder = storage.backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = image_path(selected, kind)
    if path.exists():
        raise ValueError(f"{path.name} already exists.")
    if kind == "raw" and shutil.disk_usage(folder).free < selected.size:
        raise ValueError(f"Not enough free space in {folder} for "
                         f"{storage.human_size(selected.size)}.")
    partial = path.with_name(path.name + ".partial")
    fd = storage.open_device(selected, write=False)
    try:
        ctx.log(f"Backing up {selected.name} to {path}")
        with (gzip.open(partial, "wb", compresslevel=1) if kind == "gzip"
              else open(partial, "wb")) as out:
            copied = storage.copy_stream(ctx, lambda n: os.read(fd, n), out.write,
                                         selected.size, f"Reading {selected.name}")
        if copied != selected.size:
            raise ValueError("Drive ended early; image is incomplete.")
        storage.token_target(selected.token())
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)
    sidecar = path.with_name(path.name + ".json")
    sidecar.write_text(json.dumps({"size": selected.size, "source": selected.name,
                                   "drive": storage.drive_label(selected.disk),
                                   "format": kind}, indent=2), encoding="utf-8")
    ctx.progress(1, "Backup complete", text="Done")
    ctx.log(f"Saved {path.name} ({storage.human_size(path.stat().st_size)}).", color="ok")
    return path


@app(name="Backup drive", group="Removable Storage", icon="icons/disk-backup.png", setup=setup)
def backup_drive(ctx):
    try:
        selected = storage.token_target(ctx.params["target"])
        backup(ctx, selected, ctx.params["format"])
    except Cancelled:
        ctx.log("Backup cancelled; the partial image was deleted.", color="crit")
        raise
    except (OSError, ValueError, KeyError) as exc:
        ctx.status = "error"
        ctx.progress(0, "Backup failed", text="Failed")
        ctx.log(str(exc), color="crit")
