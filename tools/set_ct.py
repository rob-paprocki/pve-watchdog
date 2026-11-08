#!/usr/bin/python3 -I
"""Set one key inside one CT's [[ct]] block of the shared config.

usage: set_ct.py <id> <key> <value>      e.g.  set_ct.py 103 standby container
Numbers are written as numbers, everything else as a quoted string. The block
must already exist; the result must still parse, or nothing is written."""
import re
import sys
import tomllib

P = "/etc/pve/pve-watchdog/config.toml"
vmid, key, value = int(sys.argv[1]), sys.argv[2], sys.argv[3]
assert re.fullmatch(r"[a-z_]+", key), "bad key"
val = value if re.fullmatch(r"\d+", value) else '"%s"' % value.replace('"', "")
lines = open(P).read().split("\n")
start = next(i for i in range(len(lines) - 1)
             if lines[i].strip() == "[[ct]]" and lines[i + 1].strip() == "id = %d" % vmid)
end = start + 1
while end < len(lines) and lines[end].strip() and not lines[end].startswith("["):
    end += 1
for i in range(start + 1, end):
    if lines[i].split("=")[0].strip() == key:
        lines[i] = "%s = %s" % (key, val)
        break
else:
    lines.insert(end, "%s = %s" % (key, val))
text = "\n".join(lines)
cfg = tomllib.loads(text)
ct = next(c for c in cfg["ct"] if c["id"] == vmid)
open(P, "w").write(text)
print("CT %d: %s = %r" % (vmid, key, ct[key]))
