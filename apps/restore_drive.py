"""Write an image from ~/Backups back to a USB drive or SD card."""

import gzip
import json
import os
from pathlib import Path
import shutil

from deckfw import app, Cancelled, Choice, Toggle
from apps import _removable_storage as storage

SUFFIXES = (".img", ".img.gz")


class RestoreForm(storage.StorageForm):
    confirm_message = "Confirm that the drive will be overwritten."


def list_images():
    folder = storage.backup_dir()
    try:
        files = [path for path in folder.iterdir()
                 if path.is_file() and not path.is_symlink() and path.name.endswith(SUFFIXES)]
    except OSError:
        return []
    return sorted(files, key=lambda path: (-path.stat().st_mtime, path.name))


def image_size(path):
    """Uncompressed size in bytes, or None when it cannot be known up front."""
    sidecar = path.with_name(path.name + ".json")
    try:
        size = json.loads(sidecar.read_text(encoding="utf-8")).get("size")
        if isinstance(size, int) and size > 0:
            return size
    except (OSError, ValueError, AttributeError):
        pass
    if path.name.endswith(".img"):
        return path.stat().st_size
    return None


def _image_label(path):
    size = image_size(path)
    return f"{path.name} {storage.human_size(size) if size else '?'}"


def setup():
    targets = storage.discover(partitions=False)
    images = list_images()
    if not images:
        image = Choice("image", "Image", [("No images in ~/Backups", "")], "",
                       help="Create one with Backup drive first.")
    else:
        image = Choice("image", "Image", [(_image_label(path), path.name) for path in images],
                       images[0].name)
    if not targets:
        return RestoreForm([image, storage.drive_field([])], submit_label="No drives available")
    return RestoreForm([
        image,
        storage.drive_field(targets, help="The whole drive is overwritten."),
        Toggle("confirm", "Overwrite drive?", help="Yes erases everything on the drive."),
    ], submit_label="Restore")


def resolve_image(name):
    folder = storage.backup_dir().resolve()
    path = folder / name
    if Path(name).name != name or not name.endswith(SUFFIXES):
        raise ValueError("Invalid image name.")
    if path.is_symlink() or not path.is_file() or path.resolve().parent != folder:
        raise ValueError("Image is no longer available.")
    return path


def restore(ctx, path, selected):
    size = image_size(path)
    if size is not None and size > selected.size:
        raise ValueError(f"Image needs {storage.human_size(size)}; drive has "
                         f"{storage.human_size(selected.size)}.")
    partprobe = shutil.which("partprobe")
    fd = storage.open_device(selected, write=True)
    try:
        ctx.log(f"Restoring {path.name} to {selected.name}")
        with (gzip.open(path, "rb") if path.name.endswith(".gz") else open(path, "rb")) as src:
            storage.copy_stream(ctx, src.read, lambda data: storage.write_all(fd, data),
                                size, f"Writing {selected.name}", limit=selected.size)
        storage.sync_device(ctx, fd, selected.name)
    finally:
        os.close(fd)
    if partprobe:
        storage.run_tool(ctx, [partprobe, str(storage.DEV / selected.name)], check=False)
    ctx.progress(1, "Restore complete", text="Done")
    ctx.log(f"Restored {path.name} to {selected.name}.", color="ok")


@app(name="Restore drive", group="Removable Storage", icon="icons/disk-restore.png", setup=setup)
def restore_drive(ctx):
    try:
        if ctx.params.get("confirm") is not True:
            raise ValueError("Restore was not confirmed.")
        path = resolve_image(ctx.params["image"])
        selected = storage.token_target(ctx.params["target"])
        restore(ctx, path, selected)
    except Cancelled:
        ctx.log("Restore cancelled; the drive is only partially written.", color="crit")
        raise
    except (OSError, ValueError, KeyError, EOFError, gzip.BadGzipFile) as exc:
        ctx.status = "error"
        ctx.progress(0, "Restore failed", text="Failed")
        ctx.log(f"{exc} The drive may be partially written.", color="crit")
