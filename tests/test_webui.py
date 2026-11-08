"""Offline tests of the web UI: the config editor, login/sessions/CSRF and the API."""
import http.client
import json
import os
import tomllib
import unittest

from pve_watchdog import tomledit
from pve_watchdog.config import Config
from pve_watchdog.hostlink import HostError
from pve_watchdog.webui import WebApp, WebServer

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "fixture_config.toml")) as f:
    EXAMPLE = f.read()


class TomlEditTests(unittest.TestCase):
    def test_keeps_comments_and_changes_one_value(self):
        t = tomledit.set_in_table(EXAMPLE, "probe", "failover_delay", 30)
        self.assertEqual(tomllib.loads(t)["probe"]["failover_delay"], 30)
        self.assertIn("# extra seconds to wait", t)                    # inline comment kept
        self.assertEqual(t.count("\n"), EXAMPLE.count("\n"))           # nothing else moved

    def test_set_top_level(self):
        t = tomledit.set_top(EXAMPLE, "dry_run", False)
        self.assertFalse(tomllib.loads(t)["dry_run"])

    def test_upsert_new_ct_then_update_it(self):
        t = tomledit.upsert_item(EXAMPLE, "ct", "id", 555, {"name": "x", "preferred_node": "pve1",
                                                             "mode": "dry-run"})
        cts = {c["id"]: c for c in tomllib.loads(t)["ct"]}
        self.assertEqual(cts[555]["mode"], "dry-run")
        t2 = tomledit.upsert_item(t, "ct", "id", 555, {"mode": "live", "standby": "container"})
        c = {c["id"]: c for c in tomllib.loads(t2)["ct"]}[555]
        self.assertEqual((c["mode"], c["standby"], c["name"]), ("live", "container", "x"))
        self.assertEqual(len(tomllib.loads(t2)["ct"]), len(tomllib.loads(EXAMPLE)["ct"]) + 1)

    def test_update_does_not_touch_other_items(self):
        t = tomledit.upsert_item(EXAMPLE, "ct", "id", 9001, {"mode": "live"})
        cts = {c["id"]: c for c in tomllib.loads(t)["ct"]}
        self.assertEqual(cts[9001]["mode"], "live")
        self.assertEqual(cts[9002]["mode"], "dry-run")

    def test_delete_item(self):
        t = tomledit.delete_item(EXAMPLE, "ct", "id", 9001)
        self.assertEqual([c["id"] for c in tomllib.loads(t)["ct"]], [9002])
        self.assertIn("# --- Real containers", t)                     # comments around it stay

    def test_remove_key(self):
        t = tomledit.upsert_item(EXAMPLE, "ct", "id", 9001, {}, remove_keys=["liveness_ip"])
        c = {c["id"]: c for c in tomllib.loads(t)["ct"]}[9001]
        self.assertNotIn("liveness_ip", c)

    def test_strings_are_escaped(self):
        t = tomledit.upsert_item(EXAMPLE, "ct", "id", 9001, {"name": 'evil"\nmode = "live'})
        c = {c["id"]: c for c in tomllib.loads(t)["ct"]}[9001]
        self.assertEqual(c["mode"], "dry-run")                         # injection did not work


class FakeHost:
    def __init__(self):
        self.text = EXAMPLE
        self.saved = []
        self.logins = {"root@pam": ("secret", True), "viewer@pve": ("pw", False)}
        self.secret = ""
        self.maint = []

    def get_config(self):
        return {"config": self.text, "secret": 'hmac_key = "k"\ndiscord_webhook = "%s"\n' % self.secret,
                "node": "pve1"}

    def put_config(self, text, who):
        if "id = 104" in text:
            raise HostError("CT 104 is not a container in this cluster", refused=True)
        self.saved.append((text, who))
        self.text = text
        return {"saved": True}

    def check_login(self, user, password):
        if "@" not in user:
            user += "@pam"
        good = self.logins.get(user)
        if not good or good[0] != password:
            return {"ok": False}
        return {"ok": True, "user": user, "admin": good[1]}

    def list_guests(self):
        return [{"id": 102, "type": "lxc", "node": "pve1", "name": "pihole", "status": "running",
                 "ip": "192.168.100.254", "role": None}]

    def config_history(self):
        return [{"ts": 1, "size": 10}]

    def restore_config(self, ts, who):
        return {"restored": ts}

    def put_secret(self, key, value):
        self.secret = value
        return {"set": bool(value)}

    def maintenance(self, what, seconds=None):
        self.maint.append((what, seconds))
        return {"active": what == "on"}


class FakeEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.local = {"quorum": {"quorate": True}, "cts": {}, "maintenance": {"active": False}}
        self.peer_status = {}

        class S:
            data = {"alerts": {}}

            def peer(self, name):
                return {"last_signals": {"ping": True}}
        self.state = S()


class Ctx:
    def __init__(self):
        self.host = FakeHost()
        self.engine = FakeEngine(Config(EXAMPLE, 'hmac_key = "k"\n', me="pve1"))
        self.reloads = 0

        class N:
            sent = []

            def info(self, t):
                self.sent.append(t)

            def flush(self):
                pass
        self.notifier = N()

    def cfg(self):
        return self.engine.cfg

    def reload(self):
        self.reloads += 1

    def sync_alerts(self):
        return {}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Ctx()
        self.app = WebApp(self.ctx)
        self.srv = WebServer(self.app, "127.0.0.1", 0, None, None)    # plain HTTP for the test
        self.srv.start()
        self.port = self.srv.httpd.server_address[1]
        self.cookie, self.csrf = None, None

    def tearDown(self):
        self.srv.httpd.shutdown()

    def req(self, method, path, body=None, csrf=True, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie and self.cookie:
            headers["Cookie"] = self.cookie
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        set_cookie = r.getheader("Set-Cookie")
        return r.status, (json.loads(data) if data and r.getheader("Content-Type") == "application/json" else data), set_cookie, r

    def login(self, user="root@pam", pw="secret"):
        st, data, ck, _ = self.req("POST", "/api/login", {"username": user, "password": pw})
        if st == 200:
            self.cookie = ck.split(";")[0]
            self.csrf = data["csrf"]
        return st, data, ck

    def test_page_and_security_headers(self):
        st, data, _, r = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"pve-watchdog", data)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(self.req("GET", "/static/../webui.py")[0], 404)

    def test_api_needs_login(self):
        self.assertEqual(self.req("GET", "/api/overview")[0], 401)

    def test_login_ok_cookie_flags(self):
        st, data, ck = self.login()
        self.assertEqual(st, 200)
        for flag in ("HttpOnly", "Secure", "SameSite=Strict"):
            self.assertIn(flag, ck)
        self.assertEqual(self.req("GET", "/api/overview")[0], 200)

    def test_wrong_password_and_throttle(self):
        for _ in range(5):
            self.assertEqual(self.login(pw="nope")[0], 401)
        self.assertEqual(self.login()[0], 429)                         # even the right one now waits

    def test_parallel_logins_cannot_beat_the_limit(self):
        import threading
        from pve_watchdog.webui import Throttle
        t = Throttle(limit=5)
        results = []
        threads = [threading.Thread(target=lambda: results.append(t.reserve("192.168.100.9")))
                   for _ in range(50)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(results.count(True), 5)

    def test_non_admin_refused(self):
        self.assertEqual(self.login("viewer@pve", "pw")[0], 403)

    def test_changes_need_csrf(self):
        self.login()
        st, *_ = self.req("POST", "/api/settings", {"probe.failover_delay": 30}, csrf=False)
        self.assertEqual(st, 403)
        self.assertEqual(self.ctx.host.saved, [])

    def test_save_setting_goes_through_gatekeeper(self):
        self.login()
        st, data, *_ = self.req("POST", "/api/settings", {"probe.failover_delay": 30})
        self.assertEqual(st, 200)
        text, who = self.ctx.host.saved[-1]
        self.assertEqual(tomllib.loads(text)["probe"]["failover_delay"], 30)
        self.assertEqual(who, "root@pam")
        self.assertEqual(self.ctx.reloads, 1)

    def test_unknown_setting_refused(self):
        self.login()
        self.assertEqual(self.req("POST", "/api/settings", {"cluster.standby_offset": 5})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"probe.rounds": "six"})[0], 400)

    def test_protect_edit_remove_guest(self):
        self.login()
        st, *_ = self.req("POST", "/api/ct", {"id": 102, "name": "pihole", "preferred_node": "pve1",
                                              "standby": "container", "mode": "dry-run",
                                              "interval_min": 30, "priority": 1,
                                              "liveness_ip": "192.168.100.254"})
        self.assertEqual(st, 200)
        c = {c["id"]: c for c in tomllib.loads(self.ctx.host.text)["ct"]}[102]
        self.assertEqual((c["mode"], c["standby"]), ("dry-run", "container"))
        self.assertEqual(self.req("POST", "/api/ct", {"id": 102, "mode": "live"})[0], 200)
        self.assertEqual({c["id"]: c for c in tomllib.loads(self.ctx.host.text)["ct"]}[102]["mode"], "live")
        self.assertEqual(self.req("DELETE", "/api/ct/102")[0], 200)
        self.assertNotIn(102, [c["id"] for c in tomllib.loads(self.ctx.host.text)["ct"]])

    def test_vm_and_bad_values_refused(self):
        self.login()
        self.assertEqual(self.req("POST", "/api/ct", {"id": 200, "type": "qemu"})[0], 400)
        self.assertEqual(self.req("POST", "/api/ct", {"id": 102, "mode": "yolo"})[0], 400)
        st, data, *_ = self.req("POST", "/api/ct", {"id": 104, "mode": "live"})   # gatekeeper refuses
        self.assertEqual(st, 400)
        self.assertIn("not a container", data["error"])

    def test_add_and_remove_node(self):
        self.login()
        st, *_ = self.req("POST", "/api/node", {"name": "pve3", "host_ip": "192.168.100.251",
                                                "controller_id": 9102, "controller_ip": "192.168.100.245"})
        self.assertEqual(st, 200)
        self.assertIn("pve3", [n["name"] for n in tomllib.loads(self.ctx.host.text)["node"]])
        self.assertEqual(self.req("DELETE", "/api/node/pve1")[0], 400)   # can't remove myself
        self.assertEqual(self.req("DELETE", "/api/node/pve3")[0], 200)

    def test_config_never_exposes_webhook(self):
        self.ctx.host.secret = "https://discord.com/api/webhooks/1/abc"
        self.login()
        st, data, *_ = self.req("GET", "/api/config")
        self.assertTrue(data["webhook_set"])
        self.assertNotIn("discord.com/api/webhooks", json.dumps(data))

    def test_actions(self):
        self.login()
        self.assertEqual(self.req("POST", "/api/action", {"action": "maintenance_on", "minutes": 30})[0], 200)
        self.assertEqual(self.ctx.host.maint[-1], ("on", 1800))
        self.assertEqual(self.req("POST", "/api/action", {"action": "rm -rf"})[0], 400)
        self.assertEqual(self.req("POST", "/api/action", {"action": "test_notify"})[0], 200)

    def test_logout(self):
        self.login()
        self.assertEqual(self.req("POST", "/api/logout")[0], 200)
        self.assertEqual(self.req("GET", "/api/overview")[0], 401)

    def test_oversized_body_refused(self):
        self.login()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/api/settings")
        c.putheader("Content-Length", str(10 * 1024 * 1024))
        c.putheader("Cookie", self.cookie)
        c.putheader("X-CSRF-Token", self.csrf)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)


if __name__ == "__main__":
    unittest.main()
