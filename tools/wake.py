#!/usr/bin/python3 -I
"""Send a Wake-on-LAN magic packet:  wake.py 00:00:5e:00:53:02 [broadcast]"""
import socket
import sys

mac = bytes.fromhex(sys.argv[1].replace(":", ""))
bcast = sys.argv[2] if len(sys.argv) > 2 else "255.255.255.255"
pkt = b"\xff" * 6 + mac * 16
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
for port in (9, 7):
    s.sendto(pkt, (bcast, port))
print("magic packet sent to", sys.argv[1])
