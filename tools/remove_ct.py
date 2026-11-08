#!/usr/bin/python3 -I
"""Remove a CT's [[ct]] block from the shared config completely.

usage: remove_ct.py <id>
The CT should be set to mode = "off" first (tools/enable_ct.py <id> off)."""
import sys
import tomllib

P = "/etc/pve/pve-watchdog/config.toml"
vmid = int(sys.argv[1])
lines = open(P).read().split("\n")
out, i, removed = [], 0, False
while i < len(lines):
    if lines[i].strip() == "[[ct]]" and i + 1 < len(lines) and lines[i + 1].strip() == "id = %d" % vmid:
        while i < len(lines) and lines[i].strip() != "":    # block ends at the next blank line
            i += 1
        while i < len(lines) and lines[i].strip() == "":    # and its trailing blank lines
            i += 1
        removed = True
        continue
    out.append(lines[i])
    i += 1
assert removed, "CT %d has no active [[ct]] block" % vmid
text = "\n".join(out)
cfg = tomllib.loads(text)                                  # refuse to write a broken config
assert all(c["id"] != vmid for c in cfg.get("ct", []))
open(P, "w").write(text)
print("removed CT %d; covered now: %s" % (vmid, [c["id"] for c in cfg.get("ct", [])]))
