"""NetworkManager hotspot ownership, local-only isolation and restoration."""

import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid


ENV = {"LC_ALL": "C", "LANG": "C"}
SUBNETS = ("192.168.1.0/24", "192.168.2.0/24", "192.168.10.0/24", "10.0.0.0/24")
INTERFACE = re.compile(r"[A-Za-z0-9_.:-]{1,15}")
MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")


def interfaces():
    if sys.platform != "linux":
        raise ValueError("QuickShare requires Linux and NetworkManager")
    return tuple(sorted(device.name for device in Path("/sys/class/net").iterdir()
                        if INTERFACE.fullmatch(device.name)
                        and ((device / "wireless").is_dir()
                             or (device / "phy80211").exists())))


def check_overlap(subnet, addresses, routes, interface):
    network = ipaddress.IPv4Network(subnet)
    for device in addresses:
        if device["ifname"] == interface:
            continue
        for address in device.get("addr_info", []):
            if address.get("family") == "inet":
                other = ipaddress.IPv4Network(
                    "{}/{}".format(address["local"], address["prefixlen"]), strict=False)
                if network.overlaps(other):
                    raise ValueError("Subnet overlaps {} on {}".format(other, device["ifname"]))
    for route in routes:
        destination = route.get("dst", "default")
        if destination == "default" or route.get("dev") == interface:
            continue
        other = ipaddress.IPv4Network(destination, strict=False)
        if network.overlaps(other):
            raise ValueError("Subnet overlaps route " + str(other))


def stations(lines, neighbors):
    connected = set()
    for line in lines:
        if line.startswith("Station "):
            parts = line.split()
            if len(parts) < 2 or not MAC.fullmatch(parts[1]):
                raise ValueError("Invalid iw station record")
            connected.add(parts[1].lower())
    addresses = {}
    for neighbor in neighbors:
        mac, address = neighbor.get("lladdr", "").lower(), neighbor.get("dst", "")
        if mac in connected and ipaddress.ip_address(address).version == 4:
            addresses.setdefault(mac, set()).add(address)
    return tuple((mac, ", ".join(sorted(addresses.get(mac, ()))) or "IP unknown")
                 for mac in sorted(connected))


class Hotspot:
    def __init__(self, ctx, interface, subnet, *, log=None):
        if not INTERFACE.fullmatch(interface) or subnet not in SUBNETS:
            raise ValueError("Invalid interface or subnet")
        self.ctx, self.interface, self.subnet = ctx, interface, subnet
        self.address = str(ipaddress.IPv4Network(subnet).network_address + 1)
        self.uuid = str(uuid.uuid4())
        self.table = "quickshare_" + self.uuid.replace("-", "")
        self.previous = ""
        self.profile_attempted = False
        self.guard_attempted = False
        self.activation_attempted = False
        self.claim = None
        self.tools = {}
        self.closed = False
        self.log = log or ctx.log
        self.recorded = False

    def command(self, tool, *args, seconds=6):
        result = self.ctx.run([self.tools["timeout"], "--kill-after=1s", str(seconds) + "s",
                               self.tools[tool], *args], log=False, env=ENV)
        if result.returncode:
            detail = " ".join(result.lines)[-1500:]
            raise RuntimeError("{} failed ({}): {}".format(tool, result.returncode, detail))
        return result.lines

    def nm(self, *args, wait=4):
        return self.command("nmcli", "--colors", "no", "--wait", str(wait), *args,
                            seconds=wait + 2)

    def property(self, field):
        return "\n".join(self.nm("--get-values", field, "device", "show", self.interface))

    def preflight(self, consent):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise PermissionError("QuickShare network control requires an already-root launcher")
        path = os.pathsep.join([os.environ.get("PATH", os.defpath), "/usr/sbin", "/sbin"])
        for tool in ("nmcli", "nft", "ip", "iw", "timeout"):
            found = shutil.which(tool, path=path)
            if not found:
                raise ValueError("Missing required command: " + tool)
            self.tools[tool] = found
        fd = os.open("/run/quickshare-{}.lock".format(self.interface),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.claim = os.fdopen(fd, "r+")
        info = os.fstat(fd)
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Unsafe QuickShare interface lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("QuickShare already owns this interface") from exc
        if self.nm("--get-values", "WIFI", "radio") != ["enabled"]:
            raise ValueError("Wi-Fi is disabled or blocked")
        if self.property("GENERAL.NM-MANAGED") != "yes":
            raise ValueError("Selected adapter is not managed by NetworkManager")
        if self.property("WIFI-PROPERTIES.AP") != "yes":
            raise ValueError("Selected adapter does not support AP mode")
        previous = self.property("GENERAL.CON-UUID")
        if previous not in ("", "--"):
            self.previous = str(uuid.UUID(previous))
            if not consent:
                raise ValueError("Adapter is connected; enable the disconnect consent first")
        addresses = json.loads("\n".join(self.command("ip", "-j", "-4", "address", "show")))
        routes = json.loads("\n".join(self.command("ip", "-j", "-4", "route", "show",
                                                   "table", "all")))
        check_overlap(self.subnet, addresses, routes, self.interface)
        tables = json.loads("\n".join(self.command("nft", "-j", "list", "tables")))
        previous_record = self.claim.read(4096)
        if previous_record:
            record = json.loads(previous_record)
            profiles = self.nm("-g", "UUID", "connection", "show")
            if (record["profile"] in profiles or any(
                    item.get("table", {}).get("name") == record["table"]
                    and item["table"].get("family") == "inet"
                    for item in tables["nftables"])):
                raise ValueError("Earlier QuickShare resources remain; inspect "
                                 "/run/quickshare-{}.lock before recovery".format(self.interface))
        record = {"profile": self.uuid, "table": self.table, "previous": self.previous,
                  "interface": self.interface}
        self.claim.seek(0)
        self.claim.truncate()
        self.claim.write(json.dumps(record) + "\n")
        self.claim.flush()
        os.fsync(self.claim.fileno())
        self.recorded = True
        self.log("Recovery profile: " + self.uuid)
        self.log("Recovery firewall table: inet " + self.table)

    def guard(self):
        iface = json.dumps(self.interface)
        rules = """table inet TABLE {
 chain forward {
  type filter hook forward priority -200; policy accept;
  iifname IFACE drop
  oifname IFACE drop
 }
 chain input {
  type filter hook input priority -200; policy accept;
  iifname != IFACE ip daddr ADDRESS tcp dport 8080 drop
  iifname IFACE meta nfproto ipv4 udp sport 68 udp dport 67 accept
  iifname IFACE ip daddr ADDRESS tcp dport 8080 accept
  iifname IFACE drop
 }
}
""".replace("TABLE", self.table).replace("IFACE", iface).replace("ADDRESS", self.address)
        with tempfile.TemporaryDirectory(prefix="quickshare-firewall-") as directory:
            path = Path(directory) / "rules.nft"
            path.write_text(rules, encoding="ascii")
            self.guard_attempted = True
            self.command("nft", "-f", str(path))

    def start(self, ssid, security, password):
        if security not in ("open", "wpa2", "wpa3"):
            raise ValueError("Unknown security mode")
        if not ssid or len(ssid.encode("utf-8")) > 32:
            raise ValueError("SSID must contain 1-32 UTF-8 bytes")
        self.guard()
        args = ["connection", "add", "save", "no", "type", "wifi", "ifname",
                self.interface, "con-name", self.table, "connection.uuid", self.uuid,
                "connection.autoconnect", "no", "802-11-wireless.ssid", ssid,
                "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                "802-11-wireless.ap-isolation", "1",
                "ipv4.method", "shared", "ipv4.addresses", self.address + "/24",
                "ipv4.never-default", "yes", "ipv6.method", "disabled"]
        if security != "open":
            args += ["802-11-wireless-security.key-mgmt",
                     "sae" if security == "wpa3" else "wpa-psk",
                     "802-11-wireless-security.proto", "rsn",
                     "802-11-wireless-security.pairwise", "ccmp",
                     "802-11-wireless-security.group", "ccmp",
                     "802-11-wireless-security.pmf", "3" if security == "wpa3" else "1",
                     "802-11-wireless-security.psk-flags", "2",
                     "802-11-wireless-security.wps-method", "1"]
        self.profile_attempted = True
        self.nm(*args)
        with tempfile.TemporaryDirectory(prefix="quickshare-credentials-") as directory:
            path = Path(directory) / "password"
            path.touch(mode=0o600)
            if security != "open":
                path.write_text("802-11-wireless-security.psk:" + password + "\n",
                                encoding="ascii")
            self.activation_attempted = True
            activation = ["connection", "up", "uuid", self.uuid, "ifname", self.interface]
            if security != "open":
                activation += ["passwd-file", str(path)]
            self.nm(*activation, wait=20)
        self.verify()
        mode = self.command("iw", "dev", self.interface, "info")
        if not any(line.strip() == "type AP" for line in mode):
            raise ValueError("Adapter did not enter AP mode")
        expected = "sae" if security == "wpa3" else "wpa-psk"
        if security != "open":
            actual = self.nm("-g", "802-11-wireless-security.key-mgmt",
                             "connection", "show", "uuid", self.uuid)
            if actual != [expected]:
                raise ValueError("Hotspot security does not match the requested mode")

    def verify(self):
        if self.property("GENERAL.CON-UUID") != self.uuid:
            raise ValueError("QuickShare no longer owns the active connection")
        addresses = self.property("IP4.ADDRESS").splitlines()
        if self.address + "/24" not in addresses:
            raise ValueError("Hotspot IPv4 address is missing")
        self.command("nft", "list", "table", "inet", self.table)

    def clients(self):
        lines = self.command("iw", "dev", self.interface, "station", "dump")
        neighbors = json.loads("\n".join(self.command("ip", "-j", "-4", "neighbor", "show",
                                                      "dev", self.interface)))
        return stations(lines, neighbors)

    def close(self):
        if self.closed:
            return
        errors = []
        safe = True
        current = None
        try:
            if self.profile_attempted:
                current = self.property("GENERAL.CON-UUID")
                if current == self.uuid:
                    self.nm("connection", "down", "uuid", self.uuid)
                profiles = self.nm("-g", "UUID", "connection", "show")
                if self.uuid in profiles:
                    self.nm("connection", "delete", "uuid", self.uuid)
                if self.property("GENERAL.CON-UUID") == self.uuid:
                    raise RuntimeError("Hotspot remains active")
        except Exception as exc:
            safe = False
            errors.append(str(exc))
        try:
            if self.guard_attempted and safe:
                tables = json.loads("\n".join(self.command("nft", "-j", "list", "tables")))
                if any(item.get("table", {}).get("name") == self.table
                       and item["table"].get("family") == "inet"
                       for item in tables["nftables"]):
                    self.command("nft", "delete", "table", "inet", self.table)
        except Exception as exc:
            errors.append(str(exc))
        try:
            if self.activation_attempted and self.previous and safe and not errors:
                now = self.property("GENERAL.CON-UUID")
                if now in ("", "--"):
                    self.nm("connection", "up", "uuid", self.previous, "ifname", self.interface,
                            wait=8)
                elif now != self.previous:
                    self.log("Adapter changed elsewhere; leaving its current connection untouched")
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if self.claim is not None:
                try:
                    if self.recorded and not errors:
                        self.claim.seek(0)
                        self.claim.truncate()
                        self.claim.flush()
                        os.fsync(self.claim.fileno())
                except OSError as exc:
                    errors.append("Cannot clear recovery record: " + str(exc))
                finally:
                    self.claim.close()
                    self.claim = None
        self.closed = True
        if errors:
            raise RuntimeError("QuickShare cleanup incomplete; profile {}; table inet {}: {}".format(
                self.uuid, self.table, "; ".join(errors)))
