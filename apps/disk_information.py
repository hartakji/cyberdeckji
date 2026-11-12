"""Read-only information about USB storage and SD cards."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import sys

import pygame

from deckfw import app, Choice, Form, Widget
from apps._removable_storage import transport


SYS_BLOCK = Path("/sys/class/block")
DEV = Path("/dev")
ENV = {"LC_ALL": "C", "LANG": "C"}


@dataclass(frozen=True)
class Disk:
    name: str
    device: str
    sequence: int
    size: int
    transport: str

    def token(self):
        return json.dumps([self.name, self.device, self.sequence, self.size, self.transport],
                          separators=(",", ":"))


@dataclass(frozen=True)
class Partition:
    name: str
    size: int
    filesystem: str


def disk_info(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("Invalid disk name.")
    disk = SYS_BLOCK / name
    if not disk.exists() or (disk / "partition").exists():
        raise ValueError("Selected disk is no longer available.")
    kind = transport(disk)
    if kind is None:
        raise ValueError("Selected disk is not USB storage or an SD card.")
    number = (disk / "dev").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+:\d+", number):
        raise ValueError("Invalid disk device number.")
    seq = (disk / "diskseq").read_text(encoding="utf-8").strip()
    sectors = (disk / "size").read_text(encoding="utf-8").strip()
    if not seq.isdecimal() or not sectors.isdecimal() or int(sectors) <= 0:
        raise ValueError("Invalid disk size or identity.")
    return Disk(name, number, int(seq), int(sectors) * 512, kind)


def discover_disks():
    if sys.platform != "linux":
        raise ValueError("Disk information requires Linux.")
    disks = []
    try:
        entries = sorted(SYS_BLOCK.iterdir())
    except OSError as exc:
        raise ValueError(f"Cannot discover disks: {exc}") from exc
    for disk in entries:
        if (disk / "partition").exists():
            continue
        try:
            disks.append(disk_info(disk.name))
        except (OSError, ValueError):
            continue
    if not disks:
        raise ValueError("No identifiable USB drives or SD cards found.")
    return disks


def human_size(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024


def setup():
    disks = discover_disks()
    choices = [(f"{disk.name} {human_size(disk.size)} {disk.transport}", disk.token())
               for disk in disks]
    return Form([Choice("disk", "Drive", choices, choices[0][1])],
                submit_label="Show information")


def selected_disk(token):
    try:
        data = json.loads(token)
        if not isinstance(data, list) or len(data) != 5:
            raise ValueError("Invalid selection.")
        expected = Disk(*data)
        current = disk_info(expected.name)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot verify selected disk: {exc}") from exc
    if expected != current:
        raise ValueError("Selected disk changed; return to setup and select it again.")
    return current


def read_partitions(ctx, disk):
    lsblk = shutil.which("lsblk")
    if not lsblk:
        raise ValueError("Install util-linux (lsblk) to read disk information.")
    result = ctx.run([lsblk, "--json", "--bytes", "--output",
                      "NAME,TYPE,SIZE,FSTYPE,MAJ:MIN", str(DEV / disk.name)],
                     log=False, env=ENV)
    if result.returncode:
        detail = " ".join(line.strip() for line in result.lines if line.strip())
        raise ValueError(f"lsblk failed ({result.returncode}): {detail or 'no diagnostic output'}")
    try:
        data = json.loads("\n".join(result.lines))
        roots = data["blockdevices"]
        if not isinstance(roots, list) or len(roots) != 1:
            raise ValueError("Expected one disk.")
        root = roots[0]
        if (root["name"] != disk.name or root["type"] != "disk"
                or root["maj:min"] != disk.device or int(root["size"]) != disk.size):
            raise ValueError("Disk identity or size does not match.")
        children = root.get("children", [])
        if not isinstance(children, list):
            raise ValueError("Invalid partition list.")
        partitions = []
        for item in children:
            if not isinstance(item, dict):
                raise ValueError("Invalid partition record.")
            if item.get("type") != "part":
                continue
            name, size, fs = item["name"], item["size"], item.get("fstype")
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name)
                    or isinstance(size, bool) or int(size) <= 0
                    or (fs is not None and not isinstance(fs, str))):
                raise ValueError("Invalid partition information.")
            partitions.append(Partition(name, int(size), "FAT" if fs == "vfat" else fs or "Unknown"))
        selected_disk(disk.token())
        return tuple(partitions)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read disk information: {exc}") from exc


class PartitionTable(Widget):
    focusable = True

    def __init__(self, disk, partitions):
        super().__init__(flex=1)
        self.disk, self.partitions = disk, partitions
        self.top = 0
        self.rows = 1
        self.horizontal = 0
        self.max_horizontal = 0

    def action(self, name):
        if name in ("up", "down"):
            self.top = max(0, min(max(0, len(self.partitions) - self.rows),
                                  self.top + (1 if name == "down" else -1)))
            return True
        if name in ("left", "right"):
            self.horizontal = max(0, min(self.max_horizontal,
                                        self.horizontal + (80 if name == "right" else -80)))
            return True
        return False

    def draw(self, surf, rect, theme):
        font, pad = theme.f_small, theme.pad
        line_h = font.get_linesize() + pad
        width = max(1, rect.w - 2 * pad - 5)

        def text(value, y, color):
            surf.blit(theme.render(font, theme.fit(font, value, width), color), (rect.x + pad, y))

        text(f"Drive: {self.disk.name} | {self.disk.transport}", rect.y, theme.accent)
        text(f"Disk size: {human_size(self.disk.size)}", rect.y + line_h, theme.text)
        row_h = font.get_linesize() + pad * 2
        footer_h = line_h + pad
        table = pygame.Rect(rect.x + pad, rect.y + 2 * line_h + pad, width,
                            max(0, rect.h - 2 * line_h - pad - footer_h))
        self.rows = max(1, (table.h - row_h) // row_h)
        self.top = min(self.top, max(0, len(self.partitions) - self.rows))
        columns = [("Partition", max(font.size("Partition")[0], font.size("mmcblk999p999")[0]) + pad * 2),
                   ("Size", font.size("9999.9 GiB")[0] + pad * 2),
                   ("Filesystem", font.size("Filesystem")[0] + pad * 2)]
        self.max_horizontal = max(0, sum(cell for _, cell in columns) - table.w)
        self.horizontal = min(self.horizontal, self.max_horizontal)
        old_clip = surf.get_clip()
        surf.set_clip(table.clip(old_clip))

        def row(values, y, bg, header=False):
            pygame.draw.rect(surf, bg, (table.x, y, table.w, row_h))
            x = table.x - self.horizontal
            for (_, cell), value in zip(columns, values):
                color = theme.accent if header else theme.text
                image = theme.render(font, theme.fit(font, value, cell - 2 * pad), color)
                surf.blit(image, (x + pad, y + (row_h - image.get_height()) // 2))
                pygame.draw.line(surf, theme.line, (x + cell - 1, y), (x + cell - 1, y + row_h))
                x += cell
            pygame.draw.line(surf, theme.line, (table.x, y + row_h - 1),
                             (table.right, y + row_h - 1))

        row([name for name, _ in columns], table.y, theme.track, True)
        for index, partition in enumerate(self.partitions[self.top:self.top + self.rows]):
            row([partition.name, human_size(partition.size), partition.filesystem],
                table.y + (index + 1) * row_h, theme.panel if index % 2 == 0 else theme.bg)
        if not self.partitions:
            text("No partitions found", table.y + row_h + pad, theme.dim)
        surf.set_clip(old_clip)
        text(f"{self.top + 1 if self.partitions else 0}/{len(self.partitions)}"
             " | Up/Down rows | Left/Right columns", rect.bottom - footer_h, theme.dim)


@app(name="Disk information", group="Removable Storage", setup=setup)
def disk_information(ctx):
    try:
        disk = selected_disk(ctx.params["disk"])
        partitions = read_partitions(ctx, disk)
        ctx.add(PartitionTable(disk, partitions))
    except (KeyError, OSError, ValueError) as exc:
        ctx.status = "error"
        ctx.log(str(exc), color="crit")
