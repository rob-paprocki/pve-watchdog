"""How controllers talk to each other.

Each controller runs a tiny web endpoint on its own IP (port 8787):
  GET  /status   -> "here is what my node looks like right now"
  POST /action   -> a request from another controller, e.g. during failback
                    "please restore CT 102 on your node and start it".

Every request is signed with a shared secret key (HMAC-SHA256) over:
  timestamp, a random nonce, the TARGET node's name, method, path, body.
So a request meant for node-a cannot be replayed to node-b, an old request
cannot be replayed later, and the same request cannot be used twice.

Every answer is signed over the request's own signature plus the answer,
so a recorded answer cannot be passed off as the answer to a new request.
Requests that fail the check get a plain unsigned 403.

An action request is never blindly trusted either: the receiving controller
hands it to its own host's gatekeeper, which re-checks every safety rule.
"""
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("pve-watchdog.peer")
MAX_SKEW = 60          # seconds; clocks on the nodes must be roughly in sync (NTP)
MAX_BODY = 65536
ALLOWED_ACTIONS = ("restore_start", "free_orphans", "refresh", "refresh_activate", "reload")


def _msg(*parts):
    return b"\n".join(p if isinstance(p, bytes) else str(p).encode() for p in parts)


def sign_request(key, ts, nonce, target, method, path, body):
    return hmac.new(key, _msg("REQ", ts, nonce, target, method, path, body),
                    hashlib.sha256).hexdigest()


def sign_reply(key, request_sig, body):
    return hmac.new(key, _msg("REPLY", request_sig, body), hashlib.sha256).hexdigest()


class ReplayGuard:
    """Remembers recent request signatures (thread-safe)."""

    def __init__(self, started=None):
        self.started = started if started is not None else time.time()
        self.seen = {}
        self.lock = threading.Lock()

    def fresh(self, ts, sig):
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return False
        t = time.time()
        if abs(t - ts) > MAX_SKEW or ts < self.started - 1:
            return False                  # too old, or from before this server started
        with self.lock:
            if sig in self.seen:
                return False
            self.seen[sig] = t
            for s, seen_at in list(self.seen.items()):
                if t - seen_at > 2 * MAX_SKEW:
                    del self.seen[s]
        return True


def verify_request(key, me, headers, method, path, body, guard):
    if not key:
        return None
    ts, nonce = headers.get("X-WD-TS"), headers.get("X-WD-Nonce")
    target, sig = headers.get("X-WD-Target"), headers.get("X-WD-Sig")
    if not (ts and nonce and target and sig) or target != me:
        return None
    if not hmac.compare_digest(sign_request(key, ts, nonce, target, method, path, body), sig):
        return None
    if not guard.fresh(ts, sig):
        return None
    return sig


class PeerServer:
    """Runs in a background thread inside the controller daemon."""

    def __init__(self, me, bind_ip, port, key, status_fn, action_fn):
        self.me, self.key = me, key
        self.status_fn = status_fn          # () -> dict
        self.action_fn = action_fn          # (from_node, verb, vmid) -> dict
        self.guard = ReplayGuard()
        server = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10                    # drop clients that send slowly

            def log_message(self, format, *args):
                log.debug("api: " + format, *args)

            def _forbidden(self, code=403):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _reply(self, request_sig, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-WD-Sig", sign_reply(server.key, request_sig, body))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                sig = (self.path == "/status" and
                       verify_request(server.key, server.me, self.headers, "GET", self.path,
                                      b"", server.guard))
                if not sig:
                    return self._forbidden()
                self._reply(sig, server.status_fn())

            def do_POST(self):
                raw = self.headers.get("Content-Length", "")
                if not raw.isdigit() or int(raw) > MAX_BODY:
                    return self._forbidden(413)
                body = self.rfile.read(int(raw))
                sig = (self.path == "/action" and
                       verify_request(server.key, server.me, self.headers, "POST", self.path,
                                      body, server.guard))
                if not sig:
                    return self._forbidden()
                try:
                    req = json.loads(body)
                    if req.get("verb") not in ALLOWED_ACTIONS:
                        raise ValueError("verb not allowed")
                    res = server.action_fn(str(req["from"]), req["verb"], int(req["vmid"]))
                    self._reply(sig, {"ok": True, "result": res})
                except Exception as e:
                    log.warning("peer action failed: %s", e)
                    self._reply(sig, {"ok": False, "error": str(e)})

        self.httpd = ThreadingHTTPServer((bind_ip, port), Handler)

    def start(self):
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="peer-api")
        t.start()
        return t


class PeerClient:
    def __init__(self, key, port, me):
        self.key = key
        self.port = port
        self.me = me

    def _request(self, node, method, path, payload=None, timeout=3):
        body = json.dumps(payload).encode() if payload is not None else b""
        ts, nonce = str(time.time()), secrets.token_hex(16)
        sig = sign_request(self.key, ts, nonce, node["name"], method, path, body)
        req = urllib.request.Request(
            "http://%s:%d%s" % (node["controller_ip"], self.port, path),
            data=body if method == "POST" else None, method=method,
            headers={"Content-Type": "application/json", "X-WD-TS": ts, "X-WD-Nonce": nonce,
                     "X-WD-Target": node["name"], "X-WD-Sig": sig})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(4 * 1024 * 1024)
            got = r.headers.get("X-WD-Sig") or ""
            if not hmac.compare_digest(sign_reply(self.key, sig, data), got):
                raise ValueError("reply from %s was not correctly signed" % node["name"])
            return json.loads(data)

    def status(self, node, timeout=3):
        """Peer controller's status, or None if it can't be reached/trusted."""
        if not self.key:
            return None
        try:
            return self._request(node, "GET", "/status", timeout=timeout)
        except Exception as e:
            log.debug("peer %s status unavailable: %s", node["name"], e)
            return None

    def action(self, node, verb, vmid, timeout=6 * 3600):
        return self._request(node, "POST", "/action",
                             {"from": self.me, "verb": verb, "vmid": vmid}, timeout=timeout)
