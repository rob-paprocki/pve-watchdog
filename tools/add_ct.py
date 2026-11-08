#!/usr/bin/python3 -I
"""Add a CT to the shared config.

usage: add_ct.py <id> <name> <preferred-node> <liveness-ip> <interval-min> <priority>
                 <standby: file|container> <mode: off|dry-run|live>
Refuses if the CT is already listed; checks the result parses before saving."""
import sys
import tomllib

P = "/etc/pve/pve-watchdog/config.toml"
vmid, name, pref, ip, interval, prio, standby, mode = sys.argv[1:9]
assert standby in ("file", "container") and mode in ("off", "dry-run", "live")
text = open(P).read()
cfg = tomllib.loads(text)
assert all(c["id"] != int(vmid) for c in cfg.get("ct", [])), "CT %s already listed" % vmid
block = ('\n[[ct]]\nid = %s\nname = "%s"\ntype = "lxc"\npreferred_node = "%s"\n'
         'liveness_ip = "%s"\ninterval_min = %s\npriority = %s\nstandby = "%s"\nmode = "%s"\n'
         % (vmid, name, pref, ip, interval, prio, standby, mode))
new = text.rstrip("\n") + "\n" + block
tomllib.loads(new)
open(P, "w").write(new)
print("added CT %s (%s, standby=%s, mode=%s)" % (vmid, name, standby, mode))
