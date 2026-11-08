#!/usr/bin/env python3
"""Run the real web UI against a made-up two-node cluster, for screenshots.

  PYTHONPATH=controller python3 scripts/screenshots/demo_webui.py [PORT]

Listens on 127.0.0.1 only (plain HTTP, default port 8999). Log in with
root@pam / demo. Nothing here talks to Proxmox: the host and the engine are
stand-ins that return fixed example data (documentation IP and MAC ranges).
Saving in the UI changes only this process's copy of the config.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "controller"))

from pve_watchdog.config import Config          # noqa: E402
from pve_watchdog.webui import WebApp, WebServer  # noqa: E402

with open(os.path.join(ROOT, "config.example.toml")) as f:
    BASE = f.read()
BASE = BASE[:BASE.index("\n[[ct]]\n") + 1] if "\n[[ct]]\n" in BASE else BASE
BASE = BASE.replace("dry_run = true", "dry_run = false")

CTS = [(110, "pihole", 254, 30, 100), (120, "unifi", 253, 30, 90), (130, "nginx", 252, 60, 80)]
CONFIG = BASE + "".join('''
[[ct]]
id = %d
name = "%s"
preferred_node = "pve1"
standby = "container"
liveness_ip = "192.168.100.%d"
interval_min = %d
priority = %d
mode = "live"
''' % c for c in CTS)

NOW = time.time()
AGES = {110: 12 * 60, 120: 26 * 60, 130: 41 * 60}


class DemoHost:
    def __init__(self):
        self.text = CONFIG
        self.history = [{"ts": int(NOW - d), "size": len(CONFIG) - i * 37}
                        for i, d in enumerate((300, 86400, 3 * 86400, 9 * 86400))]

    def get_config(self):
        return {"config": self.text, "node": "pve1",
                "secret": 'hmac_key = "demo"\ndiscord_webhook = "set"\n'}

    def put_config(self, text, who):
        self.text = text
        return {"saved": True}

    def check_login(self, user, password):
        user = user if "@" in user else user + "@pam"
        return {"ok": password == "demo", "user": user, "admin": True}

    def list_guests(self):
        g = [
            {"id": 9100, "type": "lxc", "node": "pve1", "name": "pve-watchdog", "status": "running",
             "ip": "192.168.100.243", "role": "controller"},
            {"id": 9101, "type": "lxc", "node": "pve2", "name": "pve-watchdog", "status": "running",
             "ip": "192.168.100.244", "role": "controller"},
            {"id": 105, "type": "lxc", "node": "pve1", "name": "jellyfin", "status": "running",
             "ip": "192.168.100.105", "role": None},
            {"id": 106, "type": "lxc", "node": "pve2", "name": "paperless", "status": "running",
             "ip": "192.168.100.106", "role": None},
            {"id": 200, "type": "qemu", "node": "pve1", "name": "homeassistant", "status": "running",
             "ip": "192.168.100.200", "role": None},
        ]
        for vmid, name, ip, _, _ in CTS:
            g.append({"id": vmid, "type": "lxc", "node": "pve1", "name": name, "status": "running",
                      "ip": "192.168.100.%d" % ip, "role": "original"})
            g.append({"id": vmid + 1000, "type": "lxc", "node": "pve2", "name": name, "status": "stopped",
                      "ip": "192.168.100.%d" % ip, "role": "standby", "standby_of": vmid})
        return sorted(g, key=lambda x: x["id"])

    def config_history(self):
        return self.history

    def restore_config(self, ts, who):
        return {"restored": ts}

    def put_secret(self, key, value):
        return {"set": bool(value)}

    def maintenance(self, what, seconds=None):
        return {"active": what == "on"}


class DemoState:
    data = {"alerts": {}}

    def peer(self, name):
        return {"last_signals": {"ping": True, "tcp": True, "member": True, "controller": True},
                "healthy_since": NOW - 3 * 86400}


class DemoEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = DemoState()
        members = lambda vmid: {"original": {"home": "pve1", "id": vmid, "status": "running"},  # noqa: E731
                                "standby": {"home": "pve2", "id": vmid + 1000, "status": "stopped"}}
        self.local = {
            "quorum": {"quorate": True, "expected": 3, "total": 3, "quorum": 2},
            "gateway_ok": True, "pool": {"used_pct": 31.4},
            "maintenance": {"active": False},
            "cts": {str(v): {"active": "original", "members": members(v)} for v, *_ in CTS},
        }
        peer_cts = {}
        for v, *_ in CTS:
            m = members(v)
            m["standby"]["refreshed"] = {"archive_created": NOW - AGES[v]}
            peer_cts[str(v)] = {"active": "original", "members": m}
        self.peer_status = {"pve2": {"host": {"cts": peer_cts}}}


class DemoCtx:
    def __init__(self):
        self.host = DemoHost()
        self.engine = DemoEngine(Config(self.host.text, 'hmac_key = "demo"\n', me="pve1"))

        class Quiet:
            def info(self, text):
                pass

            def flush(self):
                pass
        self.notifier = Quiet()

    def cfg(self):
        return self.engine.cfg

    def reload(self):
        self.engine.cfg = Config(self.host.text, 'hmac_key = "demo"\n', me="pve1")

    def sync_alerts(self):
        return {}


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8999
    srv = WebServer(WebApp(DemoCtx()), "127.0.0.1", port, None, None)
    srv.start()
    print("demo web UI on http://localhost:%d  (root@pam / demo)" % port, flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
