"""The web UI: a small HTTPS site served by each watchdog controller.

Both controllers serve the same page. Because the config lives in /etc/pve
(shared by the cluster), a change made on either one applies to both.

Security, in plain terms:
  * HTTPS only (a self-signed certificate made at install time).
  * Login with a Proxmox account (e.g. root@pam). The password is checked by
    this node's host gatekeeper against the host's own Proxmox API; only
    administrators get in. After 5 wrong passwords from one address, that
    address waits 10 minutes.
  * A session cookie (HttpOnly, Secure, SameSite=Strict, 8 hours) plus a
    CSRF token that every changing request must send back.
  * The UI never writes files. Every change becomes a new config text that is
    handed to the host gatekeeper, which validates it and keeps 10 old
    versions. Secrets (the Discord webhook) can be set but are never shown.
"""
import json
import logging
import os
import secrets
import ssl
import subprocess
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__, tomledit, wol
from .hostlink import HostError

log = logging.getLogger("pve-watchdog.webui")

STATIC = os.path.join(os.path.dirname(__file__), "webui_static")
STATIC_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "style.css": "text/css; charset=utf-8"}
COOKIE = "pvewd_session"
SESSION_SECONDS = 8 * 3600
MAX_BODY = 256 * 1024

CT_FIELDS = {"name": str, "type": str, "preferred_node": str, "standby_node": str,
             "liveness_ip": str, "interval_min": int, "priority": int, "standby": str,
             "mode": str, "start_at_boot": bool}
NODE_FIELDS = {"host_ip": str, "controller_id": int, "controller_ip": str, "wol_mac": str}
SETTINGS = {  # "section.key" -> type ; "" section = top level
    ".dry_run": bool,
    "probe.interval": int, "probe.rounds": int, "probe.failover_delay": int, "probe.timeout": int,
    "policy.failback": str, "policy.failback_stable_minutes": int,
    "policy.isolation_stop_after": int, "policy.boot_wait": int,
    "policy.maintenance_max_hours": int, "policy.stale_factor": int,
    "policy.wake_on_lan": bool, "policy.wake_after": int, "policy.wake_every": int,
    "policy.wake_tries": int,
    "cluster.gateway": str, "cluster.pool_max_used_pct": int,
    "replication.bwlimit_kbps": int, "replication.nice": int,
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _typed(value, kind, what):
    if kind is bool and isinstance(value, bool):
        return value
    if kind is int and isinstance(value, int) and not isinstance(value, bool):
        return value
    if kind is str and isinstance(value, str) and len(value) <= 200:
        return value.strip()
    raise ApiError(400, "%s must be %s" % (what, {bool: "true/false", int: "a whole number",
                                                  str: "text"}[kind]))


class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def new(self, user):
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            self.data[sid] = {"user": user, "csrf": csrf, "expires": time.time() + SESSION_SECONDS}
        return sid, csrf

    def get(self, sid):
        with self.lock:
            s = self.data.get(sid or "")
            if s and s["expires"] < time.time():
                del self.data[sid]
                s = None
            return dict(s) if s else None

    def drop(self, sid):
        with self.lock:
            self.data.pop(sid or "", None)


class Throttle:
    """At most 5 failed logins per client address per 10 minutes."""

    def __init__(self, limit=5, window=600):
        self.limit, self.window = limit, window
        self.lock = threading.Lock()
        self.fails = {}

    def allowed(self, ip):
        with self.lock:
            t = time.time()
            self.fails[ip] = [x for x in self.fails.get(ip, []) if t - x < self.window]
            return len(self.fails[ip]) < self.limit

    def failed(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


class WebApp:
    """The JSON API. `ctx` gives access to the running daemon:
    ctx.cfg() (current Config), ctx.host, ctx.engine, ctx.notifier,
    ctx.sync_alerts() and ctx.reload()."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.sessions = Sessions()
        self.throttle = Throttle()
        self._guests = (0, None)

    # ---- read ----
    def overview(self):
        cfg, eng = self.ctx.cfg(), self.ctx.engine
        st = eng.local or {}
        nodes = []
        for name, n in cfg.nodes.items():
            item = {"name": name, "host_ip": n["host_ip"], "controller_ip": n["controller_ip"],
                    "controller_id": n["controller_id"], "wol_mac": n.get("wol_mac", ""),
                    "me": name == cfg.me}
            if name != cfg.me:
                ps = eng.state.peer(name)
                item.update(signals=ps.get("last_signals", {}), down_since=ps.get("down_since"),
                            healthy_since=ps.get("healthy_since"))
            nodes.append(item)
        peer_cts = {}
        for name, ps in (eng.peer_status or {}).items():
            peer_cts[name] = ((ps or {}).get("host") or {}).get("cts") or {}
        cts = []
        for c in cfg.raw.get("ct", []):
            e = (st.get("cts") or {}).get(str(c["id"])) or {}
            item = {k: c.get(k) for k in ("id", "name", "preferred_node", "liveness_ip",
                                          "interval_min", "priority", "mode")}
            item["standby"] = c.get("standby", "file")
            item["here"] = e
            item["peers"] = {n: pc.get(str(c["id"])) for n, pc in peer_cts.items()}
            if item["standby"] == "container":
                item["standby_id"] = c["id"] + cfg.standby_offset
                item["standby_node"] = cfg.standby_node(c)
            cts.append(item)
        alerts = dict(eng.state.data.get("alerts", {}))
        alerts.update(self.ctx.sync_alerts())
        return {"me": cfg.me, "version": __version__, "dry_run": cfg.dry_run,
                "maintenance": st.get("maintenance"), "quorum": st.get("quorum"),
                "gateway_ok": st.get("gateway_ok"), "pool": st.get("pool"),
                "nodes": nodes, "cts": cts,
                "alerts": sorted(({"key": k, **v} for k, v in alerts.items()),
                                 key=lambda a: a.get("since", 0))}

    def config(self):
        got = self.ctx.host.get_config()
        secret = tomllib.loads(got["secret"]) if got.get("secret", "").strip() else {}
        return {"config": tomllib.loads(got["config"]), "text": got["config"],
                "webhook_set": bool(secret.get("discord_webhook"))}

    def guests(self):
        t, data = self._guests
        if data is None or time.time() - t > 20:
            data = self.ctx.host.list_guests()
            self._guests = (time.time(), data)
        return data

    # ---- change ----
    def _save(self, text, user):
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ApiError(400, "internal: produced invalid TOML (%s)" % e)
        try:
            self.ctx.host.put_config(text, user)
        except HostError as e:
            raise ApiError(400 if e.refused else 502, str(e))
        self._guests = (0, None)
        self.ctx.reload()
        return {"saved": True}

    def _text(self):
        return self.ctx.host.get_config()["config"]

    def put_ct(self, body, user):
        vmid = _typed(body.get("id"), int, "id")
        fields = {}
        for k, kind in CT_FIELDS.items():
            if k in body and body[k] not in (None, ""):
                fields[k] = _typed(body[k], kind, k)
        if fields.get("standby") not in (None, "file", "container"):
            raise ApiError(400, "standby must be file or container")
        if fields.get("mode") not in (None, "off", "dry-run", "live"):
            raise ApiError(400, "mode must be off, dry-run or live")
        fields.setdefault("type", "lxc")
        if fields["type"] != "lxc":
            raise ApiError(400, "only containers (lxc) are supported so far")
        remove = [k for k in ("standby_node", "liveness_ip") if k in body and not body[k]]
        text = tomledit.upsert_item(self._text(), "ct", "id", vmid, fields, remove_keys=remove)
        return self._save(text, user)

    def delete_ct(self, vmid, user):
        text = tomledit.delete_item(self._text(), "ct", "id", int(vmid))
        return self._save(text, user)

    def put_node(self, body, user):
        name = _typed(body.get("name"), str, "name")
        fields = {k: _typed(body[k], kind, k) for k, kind in NODE_FIELDS.items()
                  if k in body and body[k] not in (None, "")}
        text = tomledit.upsert_item(self._text(), "node", "name", name, fields)
        return self._save(text, user)

    def delete_node(self, name, user):
        cfg = self.ctx.cfg()
        if name == cfg.me:
            raise ApiError(400, "a node cannot remove itself; do it from another node")
        for c in cfg.raw.get("ct", []):
            if c.get("preferred_node") == name or (cfg.container_mode(c) and cfg.standby_node(c) == name):
                raise ApiError(400, "CT %d still uses %s; change or remove it first" % (c["id"], name))
        text = tomledit.delete_item(self._text(), "node", "name", name)
        return self._save(text, user)

    def put_settings(self, body, user):
        text = self._text()
        for key, value in body.items():
            if key not in SETTINGS:
                raise ApiError(400, "unknown setting %s" % key)
            v = _typed(value, SETTINGS[key], key)
            section, k = key.split(".", 1)
            text = tomledit.set_top(text, k, v) if section == "" else \
                tomledit.set_in_table(text, section, k, v)
        return self._save(text, user)

    def put_secret(self, body, user):
        url = body.get("discord_webhook", "")
        if not isinstance(url, str):
            raise ApiError(400, "discord_webhook must be text")
        try:
            r = self.ctx.host.put_secret("discord_webhook", url.strip())
        except HostError as e:
            raise ApiError(400, str(e))
        self.ctx.reload()
        return r

    def history(self):
        return self.ctx.host.config_history()

    def restore(self, body, user):
        try:
            r = self.ctx.host.restore_config(int(body.get("ts")), user)
        except (HostError, TypeError, ValueError) as e:
            raise ApiError(400, str(e))
        self.ctx.reload()
        return r

    def action(self, body, user):
        cfg, act = self.ctx.cfg(), body.get("action")
        if act == "maintenance_on":
            minutes = _typed(body.get("minutes", 60), int, "minutes")
            return self.ctx.host.maintenance("on", max(1, minutes) * 60)
        if act == "maintenance_off":
            return self.ctx.host.maintenance("off")
        if act in ("sync", "failback"):
            vmid = _typed(body.get("id"), int, "id")
            if not cfg.ct(vmid):
                raise ApiError(400, "CT %d is not covered" % vmid)
            argv = ["/usr/local/bin/pve-watchdog", act] + (["--now"] if act == "sync" else []) + [str(vmid)]
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("%s started %s %d from the web UI", user, act, vmid)
            return {"started": True}
        if act == "wake":
            node = cfg.nodes.get(body.get("node"))
            if not node or not node.get("wol_mac"):
                raise ApiError(400, "unknown node, or it has no wol_mac")
            wol.send(node["wol_mac"], cfg.cluster("broadcast", "255.255.255.255"))
            return {"sent": True}
        if act == "test_notify":
            self.ctx.notifier.info("test message from the web UI (%s)" % user)
            self.ctx.notifier.flush()
            return {"sent": True}
        raise ApiError(400, "unknown action")

    # ---- login ----
    def login(self, body, client_ip):
        if not self.throttle.allowed(client_ip):
            raise ApiError(429, "too many failed logins; wait 10 minutes")
        user, password = body.get("username"), body.get("password")
        if not isinstance(user, str) or not isinstance(password, str) or not user or not password:
            raise ApiError(400, "username and password required")
        try:
            r = self.ctx.host.check_login(user, password)
        except HostError as e:
            raise ApiError(502, "could not check the login: %s" % e)
        if not r.get("ok"):
            # Same answer for every failure (wrong password, two-factor
            # account, ...), so the page can't be used to test passwords.
            self.throttle.failed(client_ip)
            raise ApiError(401, "wrong username or password")
        if not r.get("admin"):
            raise ApiError(403, "%s is not a Proxmox administrator" % r.get("user"))
        self.throttle.ok(client_ip)
        sid, csrf = self.sessions.new(r["user"])
        log.info("web UI login: %s from %s", r["user"], client_ip)
        return sid, {"user": r["user"], "csrf": csrf}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pve-watchdog"
        sys_version = ""

        def log_message(self, format, *args):
            log.debug("web: " + format, *args)

        # -- helpers --
        def _headers(self, code, ctype, length, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                             "form-action 'self'")
            self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _json(self, code, obj, extra=None):
            body = json.dumps(obj).encode()
            self._headers(code, "application/json", len(body), extra)
            self.wfile.write(body)

        def _session(self):
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v, app.sessions.get(v)
            return None, None

        def _body(self):
            n = self.headers.get("Content-Length", "0")
            if not n.isdigit() or int(n) > MAX_BODY:
                raise ApiError(413, "request too large")
            raw = self.rfile.read(int(n)) if int(n) else b"{}"
            try:
                obj = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError(400, "body must be JSON")
            if not isinstance(obj, dict):
                raise ApiError(400, "body must be a JSON object")
            return obj

        # -- routing --
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/favicon.ico":
                self._headers(204, "image/x-icon", 0)
                return
            self._api("GET", path)

        def do_POST(self):
            self._api("POST", urlparse(self.path).path)

        def do_DELETE(self):
            self._api("DELETE", urlparse(self.path).path)

        def _static(self, name):
            if name not in STATIC_FILES:
                return self._json(404, {"error": "not found"})
            with open(os.path.join(STATIC, name), "rb") as f:
                data = f.read()
            self._headers(200, STATIC_FILES[name], len(data))
            self.wfile.write(data)

        def _api(self, method, path):
            try:
                if method == "POST" and path == "/api/login":
                    sid, out = app.login(self._body(), self.client_address[0])
                    cookie = "%s=%s; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=%d" % (
                        COOKIE, sid, SESSION_SECONDS)
                    return self._json(200, out, {"Set-Cookie": cookie})
                sid, sess = self._session()
                if not sess:
                    raise ApiError(401, "not logged in")
                if method != "GET" and self.headers.get("X-CSRF-Token") != sess["csrf"]:
                    raise ApiError(403, "missing or wrong CSRF token")
                user = sess["user"]
                if method == "GET" and path == "/api/session":
                    return self._json(200, {"user": user, "csrf": sess["csrf"]})
                if method == "POST" and path == "/api/logout":
                    app.sessions.drop(sid)
                    return self._json(200, {"ok": True}, {
                        "Set-Cookie": "%s=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0" % COOKIE})
                routes = {
                    ("GET", "/api/overview"): lambda: app.overview(),
                    ("GET", "/api/config"): lambda: app.config(),
                    ("GET", "/api/guests"): lambda: app.guests(),
                    ("GET", "/api/history"): lambda: app.history(),
                    ("POST", "/api/ct"): lambda: app.put_ct(self._body(), user),
                    ("POST", "/api/node"): lambda: app.put_node(self._body(), user),
                    ("POST", "/api/settings"): lambda: app.put_settings(self._body(), user),
                    ("POST", "/api/secret"): lambda: app.put_secret(self._body(), user),
                    ("POST", "/api/history/restore"): lambda: app.restore(self._body(), user),
                    ("POST", "/api/action"): lambda: app.action(self._body(), user),
                }
                if (method, path) in routes:
                    return self._json(200, routes[(method, path)]())
                if method == "DELETE" and path.startswith("/api/ct/") and path[8:].isdigit():
                    return self._json(200, app.delete_ct(path[8:], user))
                if method == "DELETE" and path.startswith("/api/node/"):
                    return self._json(200, app.delete_node(path[10:], user))
                raise ApiError(404, "not found")
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except (HostError, KeyError, ValueError) as e:
                self._json(400, {"error": str(e)})
            except Exception:
                log.exception("web UI error on %s %s", method, path)
                self._json(500, {"error": "internal error (see the controller log)"})

    return Handler


class WebServer:
    def __init__(self, app, bind_ip, port, certfile, keyfile):
        handler = make_handler(app)
        tls = None
        if certfile:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(certfile, keyfile)

        class TLSHandler(handler):
            # The TLS handshake happens here, in this connection's own thread,
            # with a time limit, so a slow or silent client can't stall the
            # server for everyone else.
            def setup(self):
                self.request.settimeout(10)
                if tls is not None:
                    self.request = tls.wrap_socket(self.request, server_side=True)
                super().setup()

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                log.debug("web: connection from %s dropped", client_address[0])

        self.httpd = Server((bind_ip, port), TLSHandler)

    def start(self):
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="web-ui")
        t.start()
        return t
