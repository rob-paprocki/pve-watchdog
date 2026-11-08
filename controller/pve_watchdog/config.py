"""Reading the shared config (fetched from this node's host via the gatekeeper)."""
import tomllib

DEFAULTS = {
    "probe": {"interval": 10, "rounds": 6, "failover_delay": 90, "tcp_port": 8006,
              "timeout": 2},
    "policy": {"failback": "manual", "failback_stable_minutes": 10,
               "isolation_stop_after": 120, "boot_wait": 120,
               "maintenance_max_hours": 8, "stale_factor": 3,
               "wake_on_lan": False, "wake_after": 60, "wake_every": 300, "wake_tries": 6},
    "controller": {"api_port": 8787},
    "cluster": {"pool_max_used_pct": 80},
}


class Config:
    def __init__(self, text, secret_text="", me=None):
        raw = tomllib.loads(text)
        for section, values in DEFAULTS.items():
            merged = dict(values)
            merged.update(raw.get(section, {}))
            raw[section] = merged
        self.raw = raw
        self.secret = tomllib.loads(secret_text) if secret_text.strip() else {}
        self.nodes = {n["name"]: n for n in raw.get("node", [])}
        self.me = me
        if me is not None and me not in self.nodes:
            raise ValueError("this node (%s) is not listed in the config" % me)
        if raw["policy"]["failback"] not in ("manual", "auto", "never"):
            raise ValueError("policy.failback must be manual, auto or never")

    # -- convenience accessors --
    @property
    def dry_run(self):
        return self.raw.get("dry_run", True) is not False

    def probe(self, k):
        return self.raw["probe"][k]

    def policy(self, k):
        return self.raw["policy"][k]

    def cluster(self, k, default=None):
        return self.raw["cluster"].get(k, default)

    @property
    def api_port(self):
        return self.raw["controller"]["api_port"]

    def peers(self):
        return [n for name, n in self.nodes.items() if name != self.me]

    def cts(self):
        """Covered containers (mode != off), highest priority (lowest number) first."""
        out = [c for c in self.raw.get("ct", []) if c.get("mode", "off") != "off"]
        return sorted(out, key=lambda c: (c.get("priority", 99), c["id"]))

    def ct(self, vmid):
        for c in self.cts():
            if c["id"] == int(vmid):
                return c
        return None

    # -- true-duplicate standbys (standby = "container") --
    @staticmethod
    def container_mode(c):
        return c.get("standby", "file") == "container"

    @property
    def standby_offset(self):
        return int(self.raw["cluster"].get("standby_offset", 1000))

    def standby_node(self, c):
        if c.get("standby_node"):
            return c["standby_node"]
        return next(n for n in self.nodes if n != c["preferred_node"])

    def my_role(self, c, node=None):
        """'original' / 'standby' for the copy that lives on `node` (default: me)."""
        node = node or self.me
        if node == c["preferred_node"]:
            return "original"
        if node == self.standby_node(c):
            return "standby"
        return None

    def member_id(self, c, role):
        return c["id"] if role == "original" else c["id"] + self.standby_offset

    @property
    def hmac_key(self):
        k = self.secret.get("hmac_key", "")
        return k.encode() if k else None

    @property
    def discord_webhook(self):
        return self.secret.get("discord_webhook", "")
