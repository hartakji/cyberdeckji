"""Ordinary Linux Wi-Fi discovery through NetworkManager; no connection changes."""

import shutil
import sys
import time

from deckfw import app, Choice, Form, Number
from apps._wifi_scan import (
    AccessPoint, Snapshot, NetworkList, ENV, OPTIONS, command, parse_networks,
    run_nmcli, scan, split_fields, wireless_interfaces, wrapped,
)


def setup():
    interfaces = wireless_interfaces()
    return Form([
        Choice("interface", "Interface",
               choices=[("All available", ""), *[(name, name) for name in interfaces]],
               default="", help="Left/Right selects a detected wireless adapter."),
        Choice("band", "Band", choices=[
            ("2.4 + 5 GHz", "both"), ("2.4 GHz", "2.4"), ("5 GHz", "5"),
        ], default="both"),
        Number("interval", "Refresh (s)", default=15, minimum=5, maximum=120,
               help="Wait after each completed scan. B cancels."),
    ], submit_label="Scan Wi-Fi")


@app(name="Wi-Fi scanner", group="Network", icon="icons/network.png", setup=setup)
def wifi_scanner(ctx):
    view = ctx.add(NetworkList())
    if sys.platform != "linux":
        view.snapshot = Snapshot(message="Cannot scan",
                                 error="Wi-Fi scanner requires Linux and NetworkManager.")
        ctx.status = "error"
        return
    nmcli = shutil.which("nmcli")
    if not nmcli:
        view.snapshot = Snapshot(message="Cannot scan",
                                 error="Install NetworkManager (nmcli) and start its service.")
        ctx.status = "error"
        return
    interface, band, interval = (ctx.params[key] for key in ("interface", "band", "interval"))
    view.snapshot = Snapshot(interfaces=(interface,) if interface else ())

    def show_interfaces(interfaces):
        current = view.snapshot
        view.snapshot = Snapshot(current.networks, current.message, current.error,
                                 current.updated, interfaces)

    while True:
        ctx.check()
        previous = view.snapshot
        ctx.note = "scanning..."
        view.snapshot = Snapshot(previous.networks,
                                 "Scanning; previous results" if previous.updated else "Scanning...",
                                 updated=previous.updated, interfaces=previous.interfaces)
        try:
            networks = scan(ctx, nmcli, interface, band, on_interfaces=show_interfaces)
        except ValueError as exc:
            view.snapshot = Snapshot(previous.networks,
                                     "STALE results - scan failed" if previous.updated else "Scan failed",
                                     error=str(exc), updated=previous.updated,
                                     interfaces=view.snapshot.interfaces)
            ctx.note = "scan failed"
            ctx.status = "error"
            return
        updated = time.strftime("%H:%M:%S")
        view.snapshot = Snapshot(networks, f"{len(networks)} APs | refresh {interval}s",
                                 updated=updated, interfaces=view.snapshot.interfaces)
        ctx.note = f"{len(networks)} access points"
        ctx.sleep(interval)
