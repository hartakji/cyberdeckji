"""Shared identification of USB storage and SD cards in Linux sysfs."""


def transport(disk):
    device = disk / "device"
    resolved = device.resolve(strict=True)
    if any(part.startswith("usb") and part[3:].isdigit() for part in resolved.parts):
        return "USB"
    if disk.name.startswith("mmcblk") and (device / "type").read_text(
            encoding="utf-8").strip() == "SD":
        return "SD"
    return None
