"""Discord alerts, with a local queue so nothing is lost while offline.

alert(key, text)   -> sends once when a problem starts (not every 10 s)
resolve(key, text) -> sends once when that problem clears
info(text)         -> one-off message (e.g. "took over CT 102")

If Discord (or the internet) is unreachable, messages wait in a queue file
and are retried on every tick, oldest first.
"""
import json
import logging
import os
import time
import urllib.request

log = logging.getLogger("pve-watchdog.notify")


class Notifier:
    def __init__(self, webhook, node, queue_path, state, dry_run_prefix=False, sender=None, pause=0.5):
        self.webhook = webhook
        self.node = node
        self.queue_path = queue_path
        self.state = state                      # dict persisted by State
        self.prefix = "[DRY-RUN] " if dry_run_prefix else ""
        self.sender = sender or self._post
        self.pause = pause

    def _post(self, text):
        body = json.dumps({"username": "pve-watchdog", "content": text[:1900]}).encode()
        req = urllib.request.Request(self.webhook, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "pve-watchdog/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300

    def _enqueue(self, level, text):
        icon = {"alert": "\U0001F6A8", "resolved": "✅", "info": "ℹ️"}[level]
        msg = "%s %s**%s** %s" % (icon, self.prefix, self.node, text)
        log.warning("NOTIFY %s: %s", level, text)
        try:
            with open(self.queue_path, "a") as f:
                f.write(json.dumps({"t": time.time(), "msg": msg}) + "\n")
        except OSError as e:
            log.error("cannot queue notification: %s", e)

    def alert(self, key, text):
        alerts = self.state.setdefault("alerts", {})
        if key in alerts:
            return
        alerts[key] = {"since": time.time(), "text": text}
        self._enqueue("alert", text)

    def resolve(self, key, text=None):
        alerts = self.state.setdefault("alerts", {})
        if key not in alerts:
            return
        del alerts[key]
        self._enqueue("resolved", text or ("resolved: " + key))

    def info(self, text):
        self._enqueue("info", text)

    def flush(self):
        if not self.webhook or not os.path.exists(self.queue_path):
            return
        with open(self.queue_path) as f:
            items = [json.loads(line) for line in f if line.strip()]
        remaining = []
        for i, item in enumerate(items):
            try:
                ok = self.sender(item["msg"])
            except Exception as e:           # network down, Discord down, ...
                log.info("notification not sent yet (%s); will retry", e)
                ok = False
            if not ok:
                remaining = items[i:]
                break
            time.sleep(self.pause)           # be gentle with Discord rate limits
        tmp = self.queue_path + ".tmp"
        with open(tmp, "w") as f:
            for item in remaining:
                f.write(json.dumps(item) + "\n")
        os.replace(tmp, self.queue_path)
