"""Small local memory for the controller (/var/lib/pve-watchdog/state.json).

Holds things like "how many probe rounds has the peer failed", "which
problems have I already alerted about". Written atomically so a crash
mid-write can never leave a half-written file.
"""
import json
import os


class State:
    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def peer(self, name):
        return self.data.setdefault("peers", {}).setdefault(name, {
            "fail_rounds": 0, "down_since": None, "healthy_since": None})

    def ct(self, vmid):
        return self.data.setdefault("cts", {}).setdefault(str(vmid), {})
