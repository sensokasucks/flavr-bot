"""
Wake-on-LAN for /wake: one "magic packet" (6 x 0xFF, then the stream PC's MAC address 16 times)
sent as a UDP broadcast on the home network (ports 9 and 7). The stream PC must allow it once:
BIOS "Wake on LAN" / "Power on by PCI-E", and in Windows the network card's
"Allow this device to wake the computer" + "Only allow a magic packet". docs/WAKE_ON_LAN.md.

The container needs the host's network (docker-compose.yml network_mode: host), or the
broadcast never leaves Docker's own network.
"""

from __future__ import annotations

import re
import socket

MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}$")


def magic_packet(mac: str) -> bytes:
    """The packet for ``mac`` ("AA:BB:CC:DD:EE:FF", dashes or none also work). ValueError if not a MAC."""
    mac = (mac or "").strip()
    if not MAC.match(mac):
        raise ValueError(f"{mac!r} is not a MAC address (like AA:BB:CC:DD:EE:FF)")
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    return b"\xff" * 6 + raw * 16


def wake(mac: str, broadcast: str = "255.255.255.255") -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            s.sendto(packet, (broadcast, port))
