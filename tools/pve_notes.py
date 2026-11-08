#!/usr/bin/python3 -I
"""Put a "pve-watchdog" block (with a clickable link to the web UI) into Proxmox Notes.

  pve_notes.py node NODE WATCHDOG_IP [--apply]   # Datacenter > node > Summary > Notes
  pve_notes.py ct   CTID NODE WATCHDOG_IP [--apply]   # the watchdog container's Notes

Only the block between the two marker lines is ever written or replaced;
anything else in the Notes stays as it is. Without --apply it only prints
what it would set.
"""
import json
import re
import subprocess
import sys

START, END = "<!-- pve-watchdog -->", "<!-- /pve-watchdog -->"
# The unmarked note written by earlier installs, replaced by the marked block.
LEGACY = re.compile(r"\*\*pve-watchdog\*\* protects selected containers.*?`pve-watchdog status`",
                    re.S)


def node_block(ip):
    return "\n".join([
        START,
        "### 🛡️ pve-watchdog",
        "**[➜ Open the pve-watchdog web UI](https://%s:8443)**  " % ip,
        "(sign in with a Proxmox admin, e.g. `root@pam`; accept the self-signed certificate once)",
        "",
        "- A normal **Reboot / Shutdown** turns on maintenance mode for 20 minutes, so it "
        "does not trigger a failover.",
        "- Longer planned work: in this node's **Shell** run `pve-watchdog maintenance on 2h` "
        "(end it early with `pve-watchdog maintenance off`).",
        "- Status: `pve-watchdog status`",
        END])


def ct_block(node, ip):
    return "\n".join([
        START,
        "### 🛡️ pve-watchdog watchdog for %s" % node,
        "**[➜ Open the pve-watchdog web UI](https://%s:8443)**" % ip,
        "",
        "This container is pinned to %s and is never failed over. Don't protect it, "
        "move it or set it to start by hand: the watchdog manages it." % node,
        END])


LEGACY_CT = re.compile(r"pve-watchdog controller for \S+\. Pinned to this node; never failed over\.\n*")


def merge(old, block):
    old = LEGACY_CT.sub("", old or "")
    if START in old and END in old:
        before, rest = old.split(START, 1)
        after = rest.split(END, 1)[1]
        return (before + block + after).strip("\n")
    if LEGACY.search(old):
        return LEGACY.sub(lambda _m: block, old, count=1).strip("\n")
    return (old.rstrip("\n") + "\n\n" + block if old.strip() else block)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def main(argv):
    apply = "--apply" in argv
    args = [a for a in argv[1:] if a != "--apply"]
    if args[:1] == ["node"] and len(args) == 3:
        node, ip = args[1], args[2]
        cur = json.loads(run(["pvesh", "get", "/nodes/%s/config" % node, "--output-format", "json"]))
        new = merge(cur.get("description", ""), node_block(ip))
        cmd = ["pvesh", "set", "/nodes/%s/config" % node, "--description", new]
    elif args[:1] == ["ct"] and len(args) == 4:
        ctid, node, ip = args[1], args[2], args[3]
        cur = json.loads(run(["pvesh", "get", "/nodes/%s/lxc/%s/config" % (node, ctid),
                              "--output-format", "json"]))
        new = merge(cur.get("description", ""), ct_block(node, ip))
        cmd = ["pct", "set", ctid, "--description", new]
    else:
        print(__doc__)
        return 2
    if apply:
        run(cmd)
        print("  Notes updated (%s %s)" % (args[0], args[1]))
    else:
        print("  would set Notes for %s %s to include the pve-watchdog block with a link to "
              "https://%s:8443" % (args[0], args[1], args[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
