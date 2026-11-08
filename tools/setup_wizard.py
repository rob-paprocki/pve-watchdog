#!/usr/bin/python3 -I
"""pve-watchdog setup wizard. Run as root on a Proxmox node (get.sh does this).

First node in a cluster:
  * finds the cluster's nodes (/etc/pve/.members) and the LAN gateway,
  * asks, per node, which container ID and free static IP its watchdog gets,
  * writes /etc/pve/pve-watchdog/config.toml in DRY-RUN mode,
  * installs this node (install.sh shows a preview first).
Every further node:
  * finds the existing shared config and installs this node from it
    (adding this node to the config first if it isn't listed yet).

Nothing is protected and nothing changes outside pve-watchdog's own files
and its watchdog container until you protect a container in the web UI and
switch it to live.
"""
import importlib.machinery
import importlib.util
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "controller"))
from pve_watchdog import tomledit  # noqa: E402

CFG = "/etc/pve/pve-watchdog/config.toml"
MEMBERS = "/etc/pve/.members"
VMLIST = "/etc/pve/.vmlist"


def gatekeeper():
    """Load the gatekeeper so configs are checked by exactly its rules."""
    path = os.path.join(HERE, "host", "pve-watchdog-hostcmd")
    loader = importlib.machinery.SourceFileLoader("pvewd_hostcmd", path)
    spec = importlib.util.spec_from_loader("pvewd_hostcmd", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def ask(question, default=None, check=None):
    while True:
        hint = " [%s]" % default if default not in (None, "") else ""
        try:
            answer = input("%s%s: " % (question, hint)).strip()
        except EOFError:
            sys.exit("\nno input; stopping")
        answer = answer or (default if default is not None else "")
        if check is None:
            return answer
        err = check(answer)
        if err is None:
            return answer
        print("  " + err)


def yes(question, default=True):
    a = ask(question + (" (Y/n)" if default else " (y/N)"), "").lower()
    return default if not a else a.startswith("y")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def me():
    return socket.gethostname().split(".")[0]


def cluster_nodes():
    """[(name, ip)] from Proxmox's own member list."""
    try:
        with open(MEMBERS) as f:
            data = json.load(f)
    except (OSError, ValueError):
        sys.exit("can't read %s: is this node part of a Proxmox cluster?" % MEMBERS)
    nodes = sorted((name, info.get("ip")) for name, info in (data.get("nodelist") or {}).items())
    if len(nodes) < 2:
        sys.exit("pve-watchdog needs a cluster of at least two nodes (found %d)" % len(nodes))
    return nodes


def used_ids():
    try:
        with open(VMLIST) as f:
            return {int(k) for k in (json.load(f).get("ids") or {})}
    except (OSError, ValueError):
        return set()


def default_gateway():
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", run(["ip", "route", "show", "default"]).stdout)
    return m.group(1) if m else ""


def bridge_mac(host=None, node=None):
    """MAC of the physical NIC under vmbr0, locally or on another node over the
    cluster's own root SSH trust (host key checked like Proxmox does)."""
    script = ("for p in /sys/class/net/vmbr0/brif/*; do n=$(basename $p); "
              "[ -e /sys/class/net/$n/device ] && cat /sys/class/net/$n/address && break; done")
    if host is None:
        out = run(["sh", "-c", script]).stdout
    else:
        out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                   "-o", "HostKeyAlias=" + node,
                   "-o", "UserKnownHostsFile=/etc/pve/nodes/%s/ssh_known_hosts" % node,
                   "root@" + host, script], timeout=20).stdout
    m = re.search(r"([0-9a-f]{2}(:[0-9a-f]{2}){5})", out.lower())
    return m.group(1) if m else ""


def ip_in_use(ip):
    return run(["ping", "-c", "1", "-W", "1", ip]).returncode == 0


def check_ip(taken):
    def check(v):
        try:
            ipaddress.IPv4Address(v)
        except ValueError:
            return "please enter an IPv4 address like 192.168.100.21"
        if v in taken:
            return "that IP is already used above"
        if ip_in_use(v):
            return "something already answers on %s; pick a free one" % v
        return None
    return check


def check_id(taken):
    def check(v):
        if not v.isdigit() or not 100 <= int(v) <= 999999999:
            return "please enter a container ID (100 or higher)"
        if int(v) in taken:
            return "ID %s is already in use in this cluster" % v
        return None
    return check


def first_node_config():
    nodes = cluster_nodes()
    print("\nCluster nodes found: %s" % ", ".join("%s (%s)" % n for n in nodes))
    gw = ask("LAN gateway (used to tell whether a node is the one that's cut off)",
             default_gateway(), check=lambda v: None if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", v) else "an IPv4 address please")
    taken_ids, taken_ips = used_ids(), set()
    entries = []
    suggest = 9100
    print("\nEach node gets a small watchdog container with a fixed IP outside your DHCP range.")
    for name, host_ip in nodes:
        while suggest in taken_ids:
            suggest += 1
        print("\n-- %s (%s)" % (name, host_ip))
        cid = int(ask("  watchdog container ID", str(suggest), check_id(taken_ids)))
        taken_ids.add(cid)
        cip = ask("  watchdog IP", None, check_ip(taken_ips))
        taken_ips.add(cip)
        mac = bridge_mac() if name == me() else bridge_mac(host_ip, name)
        mac = ask("  Wake-on-LAN MAC of its wired network card (blank = none)", mac,
                  check=lambda v: None if not v or re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", v) else "a MAC like 00:00:5e:00:53:01 please")
        entries.append((name, host_ip, cid, cip, mac))
    with open(os.path.join(HERE, "config.example.toml")) as f:
        text = f.read()
    # Replace the example nodes with the real ones; keep everything else (dry-run on).
    for n in [n["name"] for n in tomledit.tomllib.loads(text).get("node", [])]:
        text = tomledit.delete_item(text, "node", "name", n)
    for name, host_ip, cid, cip, mac in entries:
        fields = {"host_ip": host_ip, "controller_id": cid, "controller_ip": cip}
        if mac:
            fields["wol_mac"] = mac
        text = tomledit.upsert_item(text, "node", "name", name, fields)
    text = tomledit.set_in_table(text, "cluster", "gateway", gw)
    text = tomledit.set_top(text, "dry_run", True)
    return text


def add_this_node(text):
    nodes = dict(cluster_nodes())
    name = me()
    print("\nThis node (%s) isn't in the shared config yet." % name)
    cfg = tomledit.tomllib.loads(text)
    taken_ids = used_ids() | {n["controller_id"] for n in cfg.get("node", [])}
    taken_ips = {n["controller_ip"] for n in cfg.get("node", [])}
    cid = int(ask("  watchdog container ID", str(max(taken_ids | {9099}) + 1), check_id(taken_ids)))
    cip = ask("  watchdog IP", None, check_ip(taken_ips))
    mac = ask("  Wake-on-LAN MAC (blank = none)", bridge_mac())
    fields = {"host_ip": nodes.get(name, ""), "controller_id": cid, "controller_ip": cip}
    if mac:
        fields["wol_mac"] = mac
    return tomledit.upsert_item(text, "node", "name", name, fields)


def main():
    if os.geteuid() != 0:
        sys.exit("please run as root")
    if not os.path.isdir("/etc/pve/nodes/%s" % me()):
        sys.exit("this doesn't look like a Proxmox VE node (no /etc/pve/nodes/%s)" % me())
    print(__doc__)
    gk = gatekeeper()
    if os.path.exists(CFG):
        text = open(CFG).read()
        names = [n["name"] for n in tomledit.tomllib.loads(text).get("node", [])]
        new = text if me() in names else add_this_node(text)
        print("\nUsing the existing shared config (%s)." % CFG)
    else:
        new = first_node_config()
    gk.validate_cfg(tomledit.tomllib.loads(new))            # the gatekeeper's own rules
    if new != (open(CFG).read() if os.path.exists(CFG) else None):
        print("\n----- %s -----" % CFG)
        print("\n".join(l for l in new.splitlines() if l.strip() and not l.lstrip().startswith("#")))
        print("-----")
        if not yes("Write this config (dry-run: nothing will be changed yet)?"):
            sys.exit("stopped; nothing written")
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        with open(CFG + ".new", "w") as f:
            f.write(new)
        os.replace(CFG + ".new", CFG)
    installer = os.path.join(HERE, "install.sh")
    print("\nPreview of what will be installed on %s:\n" % me())
    subprocess.run([installer, me()], check=True)
    if not yes("\nInstall pve-watchdog on %s now?" % me()):
        sys.exit("stopped before installing; the config is saved. Re-run to continue.")
    subprocess.run([installer, me(), "--yes"], check=True)
    cip = next(n["controller_ip"] for n in tomledit.tomllib.loads(open(CFG).read())["node"]
               if n["name"] == me())
    others = [n for n, _ in cluster_nodes() if n != me()]
    print("\nDone. Web UI: https://%s:8443 (sign in with a Proxmox admin such as root@pam)." % cip)
    print("Next: run the same install command on %s, then protect a container in the web UI."
          % ", ".join(others))


if __name__ == "__main__":
    main()
