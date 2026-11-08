"""Uncomment one CT's [[ct]] block in config.toml and set its mode.

usage: enable_ct.py <id> <mode>     (mode: dry-run | live | off)
If the block is already active, only its mode line is changed."""
import re
import sys

P = "/etc/pve/pve-watchdog/config.toml"
vmid, mode = sys.argv[1], sys.argv[2]
assert mode in ("dry-run", "live", "off")
s = open(P).read()

commented = re.compile(r"(?m)^# \[\[ct\]\]\n# id = %s\n(?:# .*\n)*?# mode = \"[^\"]*\"\n" % vmid)
m = commented.search(s)
if m:
    block = "\n".join(line[2:] for line in m.group(0).splitlines()) + "\n"
    block = re.sub(r'mode = "[^"]*"', 'mode = "%s"' % mode, block)
    s = s[:m.start()] + block + s[m.end():]
else:
    active = re.compile(r"(?m)(^\[\[ct\]\]\nid = %s\n(?:(?!\[\[).*\n)*?)mode = \"[^\"]*\"" % vmid)
    m = active.search(s)
    assert m, "CT %s not found in config" % vmid
    s = s[:m.start()] + m.group(1) + 'mode = "%s"' % mode + s[m.end():]

import tomllib
cfg = tomllib.loads(s)                      # refuse to write a broken config
ct = [c for c in cfg["ct"] if c["id"] == int(vmid)]
assert len(ct) == 1 and ct[0]["mode"] == mode
open(P, "w").write(s)
print("CT %s -> mode = %s" % (vmid, mode))
print(ct[0])
