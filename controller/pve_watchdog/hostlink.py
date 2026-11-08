"""Talking to this node's own Proxmox host through the gatekeeper.

Each method is one request over SSH. The SSH key used here can only run
pve-watchdog-hostcmd on the host, and only from this controller's IP address.
The host's answer is JSON: {"ok": true, "result": ...} or an error/refusal.
"""
import json
import logging
import subprocess

log = logging.getLogger("pve-watchdog.host")

KEY = "/etc/pve-watchdog/id_ed25519"
KNOWN_HOSTS = "/etc/pve-watchdog/known_hosts"


class HostError(Exception):
    def __init__(self, msg, refused=False):
        super().__init__(msg)
        self.refused = refused


class Host:
    def __init__(self, host_ip, key=KEY, known_hosts=KNOWN_HOSTS, force_dry=False):
        self.host_ip = host_ip
        self.key = key
        self.known_hosts = known_hosts
        self.force_dry = force_dry

    def call(self, *words, timeout=120, stdin=None):
        words = [str(w) for w in words]
        if self.force_dry:
            words = ["--dry-run"] + words
        cmd = ["ssh", "-i", self.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + self.known_hosts,
               "--", "root@" + self.host_ip, " ".join(words)]   # "--": nothing after is an ssh option
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               input=stdin if stdin is not None else "")
        except subprocess.TimeoutExpired:
            raise HostError("host did not answer %r within %ss" % (words, timeout))
        lines = p.stdout.strip().splitlines()
        try:
            reply = json.loads(lines[-1])
        except (IndexError, ValueError):
            raise HostError("bad reply from host for %r: %s" % (words, (p.stderr or p.stdout)[-300:]))
        if not reply.get("ok"):
            raise HostError(reply.get("error", "unknown error"), refused=reply.get("refused", False))
        res = reply["result"]
        if isinstance(res, dict) and res.get("dry_run"):
            for w in res.get("would_run", []):
                log.info("DRY-RUN %s would run: %s", words[0] if words[0] != "--dry-run" else words[1], w)
            for c in res.get("checks", []):
                if not c["ok"]:
                    log.info("DRY-RUN check would FAIL: %s", c["check"])
        return res

    # Read-only
    def status(self):
        return self.call("status", timeout=60)

    def get_config(self):
        return self.call("get-config", timeout=30)

    def probe(self, ip):
        return self.call("probe", ip, timeout=30)

    # Changing (gatekeeper decides whether it is real or dry-run)
    def backup(self, vmid, mode="snapshot"):
        return self.call("backup", vmid, mode, timeout=6 * 3600 + 600)

    def push(self, vmid, target):
        return self.call("push", vmid, target, timeout=6 * 3600 + 600)

    def start(self, vmid):
        return self.call("start", vmid, timeout=400)

    def stop(self, vmid):
        return self.call("stop", vmid, timeout=300)

    def refresh(self, vmid):
        return self.call("refresh", vmid, timeout=6 * 3600 + 600)

    def activate(self, base, which):
        return self.call("activate", base, which, timeout=400)

    # Web UI
    def list_guests(self):
        return self.call("list-guests", timeout=120)

    def put_config(self, text, who):
        return self.call("put-config", who, timeout=120, stdin=text)

    def config_history(self):
        return self.call("config-history", timeout=30)

    def restore_config(self, ts, who):
        return self.call("restore-config", ts, who, timeout=60)

    def put_secret(self, key, value):
        return self.call("put-secret", key, timeout=30, stdin=value)

    def check_login(self, username, password):
        import json as _json
        return self.call("check-login", timeout=30,
                         stdin=_json.dumps({"username": username, "password": password}))

    def adopt(self, vmid):
        return self.call("adopt", vmid, timeout=120)

    def force_quorum(self):
        return self.call("force-quorum", timeout=120)

    def claim(self, vmid, from_node):
        return self.call("claim", vmid, from_node, timeout=120)

    def release(self, vmid):
        return self.call("release", vmid, timeout=120)

    def restore(self, vmid):
        return self.call("restore", vmid, timeout=6 * 3600 + 600)

    def free_orphans(self, vmid):
        return self.call("free-orphans", vmid, timeout=900)

    def maintenance(self, what, seconds=None):
        args = ["maintenance", what] + ([seconds] if seconds is not None else [])
        return self.call(*args, timeout=30)
