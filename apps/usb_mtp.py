"""Expose the device through the installed USB MTP service."""

from deckfw import app
from apps import _usb_otg


SERVICE = "usb-mtp"


@app(name="USB-MTP", group="USB-OTG", icon="icons/usb-mtp.png",
     shutdown=_usb_otg.shutdown)
def usb_mtp(ctx):
    _usb_otg.check_inactive(ctx, SERVICE)
    _usb_otg.start(ctx, SERVICE)
