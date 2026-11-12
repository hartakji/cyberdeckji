"""Detect counterfeit USB drives and SD cards with f3probe."""

import re

from deckfw import app, Cancelled
from apps import _removable_storage as storage

USABLE = re.compile(r"\*Usable\* size:\s*([\d.]+ \w+)")
ANNOUNCED = re.compile(r"Announced size:\s*([\d.]+ \w+)")


def setup():
    targets = storage.discover(partitions=False)
    if not targets:
        return storage.empty_form()
    return storage.StorageForm([
        storage.drive_field(targets, help="f3probe restores every block it changes. "
                                          "Keep the drive connected until it finishes."),
    ], submit_label="Check")


def verdict(lines, code):
    text = "\n".join(lines)
    if "is the real thing" in text:
        return "Genuine: full capacity is usable.", "ok"
    if "counterfeit" in text or "damaged" in text:
        usable, announced = USABLE.search(text), ANNOUNCED.search(text)
        if usable and announced:
            return (f"FAKE: usable {usable.group(1)} of {announced.group(1)}. "
                    "f3fix can shrink the partition to the real size."), "crit"
        return "FAKE or damaged drive; see the log above.", "crit"
    return f"f3probe gave no verdict (exit {code}).", "crit"


@app(name="Fake capacity check", group="Removable Storage", icon="icons/fake-capacity.png", setup=setup)
def fake_capacity(ctx):
    try:
        selected = storage.token_target(ctx.params["target"])
        try:
            tool = storage.require_tools("f3probe")["f3probe"]
        except ValueError:
            raise ValueError("f3probe not found; install it with: apt install f3") from None
        storage.check_safe(selected)
        ctx.progress(None, f"Probing {selected.name}")
        result = storage.run_tool(ctx, [tool, "--time-ops", str(storage.DEV / selected.name)],
                                  check=False, log=True)
        message, color = verdict(result.lines, result.returncode)
        ctx.progress(1 if color == "ok" else 0, message.split(":")[0],
                     text="Done" if color == "ok" else "Problem")
        ctx.log(message, color=color)
        if color != "ok":
            ctx.status = "error"
    except Cancelled:
        ctx.log("Check cancelled; f3probe may not have restored every block.", color="crit")
        raise
    except (OSError, ValueError, KeyError) as exc:
        ctx.status = "error"
        ctx.progress(0, "Check failed", text="Failed")
        ctx.log(str(exc), color="crit")
