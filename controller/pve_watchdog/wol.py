"""Wake-on-LAN: send a "magic packet" so a powered-off node turns itself on.

The packet is a LAN broadcast (6 x 0xFF, then the target's MAC 16 times),
sent to UDP ports 9 and 7. It only works inside the same LAN, and only if
the target's BIOS keeps its network card powered while the machine is off,
with Wake-on-LAN enabled on that card (pve-watchdog-wol.service does that).
"""
import socket


def magic_packet(mac):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError("not a MAC address: %r" % mac)
    return b"\xff" * 6 + raw * 16


def send(mac, broadcast="255.255.255.255"):
    pkt = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            s.sendto(pkt, (broadcast, port))
    return True
