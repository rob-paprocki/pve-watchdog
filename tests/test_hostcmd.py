"""Offline tests of the host gatekeeper's safety rules (no Proxmox needed)."""
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest
import unittest.mock
import tomllib
from contextlib import redirect_stdout

HERE = os.path.dirname(__file__)
_loader = importlib.machinery.SourceFileLoader("hostcmd", os.path.join(HERE, "..", "host", "pve-watchdog-hostcmd"))
_spec = importlib.util.spec_from_loader("hostcmd", _loader)
hc = importlib.util.module_from_spec(_spec)
_loader.exec_module(hc)
REAL_IP_ALIVE = hc.ip_alive          # kept before the sandbox stubs it out

with open(os.path.join(HERE, "fixture_config.toml")) as f:
    EXAMPLE = f.read()
LIVE = EXAMPLE.replace("dry_run = true", "dry_run = false").replace('mode = "dry-run"', 'mode = "live"')

PVECM = """Cluster information
-------------------
Name:             example-cluster
Config Version:   7

Quorum information
------------------
Quorate:          Yes

Votequorum information
----------------------
Expected votes:   3
Highest expected: 3
Total votes:      3
Quorum:           2
Flags:            Quorate

Membership information
----------------------
    Nodeid      Votes Name
0x00000001          2 192.168.100.249 (local)
0x00000002          1 192.168.100.250
"""


class Sandbox(unittest.TestCase):
    """Builds a fake /etc/pve in a temp dir and stubs out every real command."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        hc.PVE = self.root + "/pve"
        hc.ALLOW_LIVE = self.root + "/allow-live"
        hc.LOG = self.root + "/log"
        hc.LOCK = self.root + "/lock"
        hc.VAR = self.root
        hc.FORCE_DRY = False
        for n in ("pve1", "pve2"):
            os.makedirs("%s/nodes/%s/lxc" % (hc.PVE, n))
            os.makedirs("%s/nodes/%s/qemu-server" % (hc.PVE, n))
        os.makedirs(hc.PVE + "/pve-watchdog")
        self.write_cfg(EXAMPLE)
        self.ran = []
        self.me = "pve2"
        self.q = {"quorate": True, "member_nodes": ["pve2"], "members": [],
                  "expected": 3, "total": 1, "quorum": 2}
        self.pinging = set()
        self.alive = set()
        self.archive = {"created": 0, "used_bytes": 2**30, "mode": "snapshot", "size": 10}
        self.status = "stopped"
        hc.me = lambda: self.me
        hc.run = self.fake_run
        hc.quorum = lambda cfg: dict(self.q)
        hc.ping = lambda ip, timeout=2: ip in self.pinging
        hc.ip_alive = lambda cfg, ip: ip in self.alive
        hc.archive_meta = lambda cfg, vmid: self.archive
        hc.ct_status_local = lambda vmid: self.status
        hc.pool = lambda cfg: {"size": 100 * 2**30, "used": 10 * 2**30, "used_pct": 10.0, "free": 90 * 2**30}
        hc.ct_volumes = lambda cfg, vmid: []

    def fake_run(self, cmd, timeout=600, check=True, input=None):
        self.ran.append(cmd)

        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()

    def write_cfg(self, text):
        with open(hc.CONFIG(), "w") as f:
            f.write(text)

    def conf(self, node, vmid):
        p = "%s/nodes/%s/lxc/%d.conf" % (hc.PVE, node, vmid)
        with open(p, "w") as f:
            f.write("rootfs: local-lvm:vm-%d-disk-0,size=2G\n" % vmid)
        return p

    def go_live(self):
        self.write_cfg(LIVE)
        open(hc.ALLOW_LIVE, "w").close()


class ParseTests(Sandbox):
    def test_parse_pvecm(self):
        q = hc.parse_quorum(PVECM)
        self.assertTrue(q["quorate"])
        self.assertEqual((q["expected"], q["total"], q["quorum"]), (3, 3, 2))
        self.assertEqual([m["name"] for m in q["members"]], ["192.168.100.249", "192.168.100.250"])
        self.assertEqual([m["votes"] for m in q["members"]], [2, 1])

    def test_parse_not_quorate(self):
        q = hc.parse_quorum(PVECM.replace("Quorate:          Yes", "Quorate:          No"))
        self.assertFalse(q["quorate"])

    def test_bad_vmid(self):
        for bad in ("abc", "1", "102; rm -rf /", "0102"):
            with self.assertRaises(hc.Refused):
                hc.parse_vmid(bad)

    def test_controller_cannot_be_covered(self):
        self.write_cfg(EXAMPLE.replace("id = 9001", "id = 9100"))
        with self.assertRaises(hc.Refused):
            hc.load_cfg()

    def test_vms_refused(self):
        open("%s/nodes/pve2/qemu-server/9001.conf" % hc.PVE, "w").close()
        with self.assertRaises(hc.Refused):
            hc.conf_locations(9001)


class GateTests(Sandbox):
    def test_dry_run_by_default_changes_nothing(self):
        self.conf("pve2", 9002)
        r = hc.v_start(hc.load_cfg(), "9002")
        self.assertTrue(r["dry_run"])
        self.assertEqual(self.ran, [])
        self.assertEqual(r["would_run"], ["pct start 9002"])

    def test_needs_all_three_switches(self):
        self.conf("pve2", 9002)
        # 1) config live but no allow-live file
        self.write_cfg(LIVE)
        self.assertTrue(hc.v_start(hc.load_cfg(), "9002")["dry_run"])
        # 2) + allow-live, but the CT is still mode = "dry-run"
        open(hc.ALLOW_LIVE, "w").close()
        self.write_cfg(EXAMPLE.replace("dry_run = true", "dry_run = false"))
        self.assertTrue(hc.v_start(hc.load_cfg(), "9002")["dry_run"])
        # 3) all three
        self.write_cfg(LIVE)
        r = hc.v_start(hc.load_cfg(), "9002")
        self.assertTrue(r["live"])
        self.assertEqual(self.ran, [["pct", "start", "9002"]])

    def test_force_dry_overrides_live(self):
        self.go_live()
        self.conf("pve2", 9002)
        hc.FORCE_DRY = True
        self.assertTrue(hc.v_start(hc.load_cfg(), "9002")["dry_run"])
        self.assertEqual(self.ran, [])

    def test_uncovered_ct_refused_even_in_dry_run(self):
        self.go_live()
        self.conf("pve2", 104)
        for fn in (hc.v_start, hc.v_stop, hc.v_restore, hc.v_backup, hc.v_free_orphans, hc.v_release):
            with self.assertRaises(hc.Refused):
                fn(hc.load_cfg(), "104")
        self.assertEqual(self.ran, [])

    def test_start_refused_if_ip_answers(self):
        self.go_live()
        self.conf("pve2", 9002)
        self.alive.add("192.168.100.232")
        with self.assertRaises(hc.Refused):
            hc.v_start(hc.load_cfg(), "9002")
        self.assertEqual(self.ran, [])

    def test_start_refused_if_registered_elsewhere(self):
        self.go_live()
        self.conf("pve1", 9001)
        with self.assertRaises(hc.Refused):
            hc.v_start(hc.load_cfg(), "9001")


class LivenessTests(Sandbox):
    def test_probe_that_cannot_run_counts_as_alive(self):
        hc.arp_probe = lambda ip, iface="vmbr0", timeout=1.0: None
        self.assertTrue(REAL_IP_ALIVE(hc.load_cfg(), "192.168.100.231"))

    def test_silent_ip_is_not_alive(self):
        hc.arp_probe = lambda ip, iface="vmbr0", timeout=1.0: False
        self.assertFalse(REAL_IP_ALIVE(hc.load_cfg(), "192.168.100.231"))


class ClaimTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.go_live()
        self.src = self.conf("pve1", 9001)

    def test_claim_parks_config_when_peer_is_dead(self):
        r = hc.v_claim(hc.load_cfg(), "9001", "pve1")
        self.assertTrue(r["live"])
        self.assertFalse(os.path.exists(self.src))
        self.assertTrue(os.path.exists(r["parked"]))
        self.assertEqual(hc.conf_locations(9001), [])

    def _refused(self):
        with self.assertRaises(hc.Refused):
            hc.v_claim(hc.load_cfg(), "9001", "pve1")
        self.assertTrue(os.path.exists(self.src))       # nothing moved

    def test_refused_if_peer_still_member(self):
        self.q["member_nodes"].append("pve1")
        self._refused()

    def test_refused_if_peer_pings(self):
        self.pinging.add("192.168.100.249")
        self._refused()

    def test_refused_if_ct_ip_answers(self):
        self.alive.add("192.168.100.231")
        self._refused()

    def test_refused_if_not_quorate(self):
        self.q["quorate"] = False
        self._refused()

    def test_refused_without_local_copy(self):
        self.archive = None
        self._refused()

    def test_dry_run_reports_failed_checks_without_raising(self):
        self.write_cfg(EXAMPLE)
        self.q["member_nodes"].append("pve1")
        r = hc.v_claim(hc.load_cfg(), "9001", "pve1")
        self.assertTrue(r["dry_run"])
        failed = [c["check"] for c in r["checks"] if not c["ok"]]
        self.assertEqual(failed, ["pve1 is not a cluster member"])
        self.assertTrue(os.path.exists(self.src))


class RestoreTests(Sandbox):
    def test_restore_then_onboot_0(self):
        self.go_live()
        hc.v_restore(hc.load_cfg(), "9001")
        cmds = [c for c in self.ran if c[0] == "pct"]
        self.assertEqual(cmds[0][:3], ["pct", "restore", "9001"])
        self.assertNotIn("--unique", cmds[0])            # keep the MAC address
        self.assertEqual(cmds[1], ["pct", "set", "9001", "--onboot", "0"])

    def test_restore_refused_if_id_registered(self):
        self.go_live()
        self.conf("pve1", 9001)
        with self.assertRaises(hc.Refused):
            hc.v_restore(hc.load_cfg(), "9001")

    def test_restore_refused_if_pool_would_pass_limit(self):
        self.go_live()
        self.archive = dict(self.archive, used_bytes=75 * 2**30)   # 10 + 75 = 85% > 80%
        with self.assertRaises(hc.Refused):
            hc.v_restore(hc.load_cfg(), "9001")
        self.assertEqual(self.ran, [])


class ForceQuorumTests(Sandbox):
    def test_force_quorum_refused_when_peer_pings(self):
        self.go_live()
        self.q["quorate"] = False
        self.pinging |= {"192.168.100.249", "192.168.100.1"}
        with self.assertRaises(hc.Refused):
            hc.v_force_quorum(hc.load_cfg())
        self.assertEqual(self.ran, [])

    def test_force_quorum_runs_when_peer_gone(self):
        self.go_live()
        self.q["quorate"] = False
        self.pinging.add("192.168.100.1")
        flips = iter([False, True])
        hc.quorum = lambda cfg: dict(self.q, quorate=next(flips, True))
        from unittest import mock
        with mock.patch.object(hc.time, "sleep", lambda s: None):
            r = hc.v_force_quorum(hc.load_cfg())
        self.assertTrue(r["quorate"])
        self.assertIn(["pvecm", "expected", "1"], self.ran)


class OrphanTests(Sandbox):
    def test_never_deletes_disk_in_use(self):
        self.go_live()
        self.status = "running"
        self.conf("pve2", 9002)        # config uses vm-9002-disk-0
        hc.ct_volumes = lambda cfg, vmid: [{"name": "vm-9002-disk-0"}, {"name": "vm-9002-disk-1"}]
        r = hc.v_free_orphans(hc.load_cfg(), "9002")
        self.assertEqual(r["orphans"], ["vm-9002-disk-1"])
        self.assertEqual(self.ran, [["pvesm", "free", "local-lvm:vm-9002-disk-1"]])

    def test_refused_if_ct_not_confirmed_running(self):
        self.go_live()
        self.conf("pve1", 9001)        # lives on node-a, which is NOT a member
        hc.ct_volumes = lambda cfg, vmid: [{"name": "vm-9001-disk-0"}]
        with self.assertRaises(hc.Refused):
            hc.v_free_orphans(hc.load_cfg(), "9001")
        self.assertEqual(self.ran, [])


PAIR = LIVE.replace("id = 9001\n", 'id = 9001\nstandby = "container"\n', 1)


class PairTests(Sandbox):
    """True-duplicate standbys: 9001 lives on node-a, its standby 10001 on node-b."""

    def setUp(self):
        super().setUp()
        self.write_cfg(PAIR)
        open(hc.ALLOW_LIVE, "w").close()
        self.remote = "stopped"
        hc.remote_status = lambda n, v: self.remote
        hc.conf_tags = lambda vmid: ["test"]
        self.archive = {"created": 5, "used_bytes": 2**30, "mode": "snapshot", "size": 10}

    def pct(self):
        return [c for c in self.ran if c and c[0] == "pct"]

    def test_standby_id_collision_refused(self):
        self.write_cfg(PAIR.replace("id = 9002", "id = 10001"))
        with self.assertRaises(hc.Refused):
            hc.load_cfg()

    def test_create_standby_first_time(self):
        hc.v_refresh(hc.load_cfg(), "10001")
        cmds = self.pct()
        self.assertEqual(cmds[0][:3], ["pct", "restore", "10001"])
        self.assertNotIn("--force", cmds[0])
        self.assertIn(["pct", "set", "10001", "--onboot", "0"], cmds)
        self.assertIn(["pct", "set", "10001", "--tags", "test;pve-watchdog-standby"], cmds)

    def test_refresh_existing_standby_uses_force(self):
        self.conf("pve2", 10001)
        hc.v_refresh(hc.load_cfg(), "10001")
        self.assertEqual(self.pct()[0][-2:], ["--force", "1"])

    def _protected(self, node, vmid):
        p = self.conf(node, vmid)
        with open(p, "a") as f:
            f.write("protection: 1\n")

    def test_refresh_lifts_protection_on_tagged_standby(self):
        self._protected("pve2", 10001)
        hc.conf_tags = lambda vmid: ["test", "pve-watchdog-standby"]
        hc.v_refresh(hc.load_cfg(), "10001")
        cmds = self.pct()
        self.assertEqual(cmds[0], ["pct", "set", "10001", "--protection", "0"])
        self.assertEqual(cmds[1][:3], ["pct", "restore", "10001"])

    def test_protected_untagged_standby_refused(self):
        self._protected("pve2", 10001)              # conf_tags -> ["test"] only
        with self.assertRaises(hc.Refused):
            hc.v_refresh(hc.load_cfg(), "10001")
        self.assertEqual(self.pct(), [])                  # nothing changed (only a read-only verify ran)

    def test_protection_put_back_if_restore_fails(self):
        self._protected("pve2", 10001)
        hc.conf_tags = lambda vmid: ["pve-watchdog-standby"]
        real = self.fake_run

        def run(cmd, timeout=600, check=True, input=None):
            if cmd[:2] == ["pct", "restore"]:
                raise RuntimeError("restore failed")
            return real(cmd, timeout, check, input)
        hc.run = run
        with self.assertRaises(RuntimeError):
            hc.v_refresh(hc.load_cfg(), "10001")
        self.assertEqual(self.ran[-1], ["pct", "set", "10001", "--protection", "1"])

    def test_refresh_refused_while_standby_is_active(self):
        self.conf("pve2", 10001)
        hc.role_set(9001, "standby")
        with self.assertRaises(hc.Refused):
            hc.v_refresh(hc.load_cfg(), "10001")
        self.assertEqual(self.ran, [])

    def test_refresh_refused_while_running(self):
        self.conf("pve2", 10001)
        self.status = "running"
        with self.assertRaises(hc.Refused):
            hc.v_refresh(hc.load_cfg(), "10001")

    def test_refresh_refused_on_wrong_node(self):
        self.me = "pve1"                 # the standby belongs on node-b
        with self.assertRaises(hc.Refused):
            hc.v_refresh(hc.load_cfg(), "10001")

    def test_activate_standby_when_peer_dead(self):
        self.conf("pve2", 10001)
        r = hc.v_activate(hc.load_cfg(), "9001", "standby")
        self.assertTrue(r["live"])
        self.assertEqual(hc.role_active(9001), "standby")
        self.assertEqual(self.pct(), [["pct", "start", "10001"]])

    def test_activate_refused_if_other_copy_running(self):
        self.conf("pve2", 10001)
        self.q["member_nodes"].append("pve1")
        self.remote = "running"
        with self.assertRaises(hc.Refused):
            hc.v_activate(hc.load_cfg(), "9001", "standby")
        self.assertEqual(hc.role_active(9001), "original")
        self.assertEqual(self.ran, [])

    def test_activate_refused_if_peer_pings_but_not_member(self):
        self.conf("pve2", 10001)
        self.pinging.add("192.168.100.249")
        with self.assertRaises(hc.Refused):
            hc.v_activate(hc.load_cfg(), "9001", "standby")

    def test_activate_refused_if_ip_answers(self):
        self.conf("pve2", 10001)
        self.alive.add("192.168.100.231")
        with self.assertRaises(hc.Refused):
            hc.v_activate(hc.load_cfg(), "9001", "standby")
        self.assertEqual(hc.role_active(9001), "original")

    def test_push_only_from_active_copy(self):
        self.conf("pve2", 10001)                  # passive standby here
        with self.assertRaises(hc.Refused):
            hc.v_push(hc.load_cfg(), "9001", "pve1")

    def test_plain_start_refused_for_pairs(self):
        self.conf("pve2", 10001)
        for vmid in ("9001", "10001"):
            with self.assertRaises(hc.Refused):
                hc.v_start(hc.load_cfg(), vmid)

    def test_status_reports_both_copies(self):
        self.conf("pve2", 10001)
        e = hc.status_pair(hc.load_cfg(), hc.member(hc.load_cfg(), 9001)[0])
        self.assertEqual(e["active"], "original")
        self.assertEqual(e["members"]["standby"]["id"], 10001)
        self.assertTrue(e["members"]["standby"]["local"])
        self.assertEqual(e["members"]["original"]["home"], "pve1")


class WebUiVerbTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.guests = [{"vmid": 102, "type": "lxc", "node": "pve1"},
                       {"vmid": 200, "type": "qemu", "node": "pve2"}]
        hc.cluster_guests = lambda: self.guests
        os.makedirs(hc.PVE + "/priv", exist_ok=True)
        open(hc.SECRET(), "w").write('hmac_key = "k"\ndiscord_webhook = ""\n')

    def put(self, text):
        hc.STDIN = text
        return hc.v_put_config(hc.load_cfg(), "root@pam")

    def test_bad_configs_refused_and_file_untouched(self):
        before = open(hc.CONFIG()).read()
        for bad in (EXAMPLE.replace('host_ip = "192.168.100.249"', 'host_ip = "not-an-ip"'),
                    EXAMPLE.replace('interval_min = 30', 'interval_min = 0', 1),
                    EXAMPLE.replace('failback = "auto"', 'failback = "sometimes"'),
                    EXAMPLE.replace("id = 9002", "id = 9100"),           # a controller
                    "this is [not toml"):
            with self.assertRaises(hc.Refused):
                self.put(bad)
        self.assertEqual(open(hc.CONFIG()).read(), before)

    def test_strict_schema(self):
        for bad in (EXAMPLE + "\nsurprise = 1\n",
                    EXAMPLE.replace("[probe]\n", "[probe]\nshell = \"rm -rf /\"\n"),
                    EXAMPLE.replace('archive_dir = "/var/lib/pve-watchdog/archive"',
                                    'archive_dir = "/var/lib/pve-watchdog/../../etc"'),
                    EXAMPLE.replace('archive_dir = "/var/lib/pve-watchdog/archive"',
                                    'archive_dir = "/root"'),
                    EXAMPLE.replace('gateway = "192.168.100.1"', 'gateway = "203.0.113.4; reboot"'),
                    EXAMPLE.replace('nice = 10', 'nice = 99'),
                    EXAMPLE.replace("id = 9001\n", "id = 9001\nhook = \"x\"\n", 1),
                    EXAMPLE + "\n[extra]\na = 1\n"):
            with self.assertRaises(hc.Refused):
                hc.validate_cfg(tomllib.loads(bad))
        hc.validate_cfg(tomllib.loads(EXAMPLE))                       # the example is fine

    def test_vm_cannot_be_covered(self):
        text = EXAMPLE + '\n[[ct]]\nid = 200\npreferred_node = "pve2"\nmode = "dry-run"\n'
        with self.assertRaises(hc.Refused) as e:
            self.put(text)
        self.assertIn("VMs are not supported", str(e.exception))

    def test_unknown_guest_cannot_be_covered(self):
        text = EXAMPLE + '\n[[ct]]\nid = 555\npreferred_node = "pve1"\nmode = "live"\n'
        with self.assertRaises(hc.Refused):
            self.put(text)

    def test_save_keeps_history_of_ten(self):
        import time as _t
        for i in range(12):
            with unittest.mock.patch.object(hc, "now", lambda i=i: 1000 + i):
                self.put(EXAMPLE.replace("failover_delay = 90", "failover_delay = %d" % (50 + i)))
        self.assertIn("failover_delay = 61", open(hc.CONFIG()).read())
        hist = hc.v_config_history(hc.load_cfg())
        self.assertEqual(len(hist), 10)
        self.assertEqual(hist[0]["ts"], 1011)

    def test_restore(self):
        with unittest.mock.patch.object(hc, "now", lambda: 2000):
            self.put(EXAMPLE.replace("failover_delay = 90", "failover_delay = 40"))
        hc.v_restore_config(hc.load_cfg(), "2000", "root@pam")
        self.assertIn("failover_delay = 90", open(hc.CONFIG()).read())

    def test_secret_only_discord_urls(self):
        hc.STDIN = "https://evil.example.com/x"
        with self.assertRaises(hc.Refused):
            hc.v_put_secret(hc.load_cfg(), "discord_webhook")
        with self.assertRaises(hc.Refused):
            hc.v_put_secret(hc.load_cfg(), "hmac_key")
        hc.STDIN = "https://discord.com/api/webhooks/123/abc_DEF-9"
        hc.v_put_secret(hc.load_cfg(), "discord_webhook")
        self.assertIn('discord_webhook = "https://discord.com/api/webhooks/123/abc_DEF-9"',
                      open(hc.SECRET()).read())
        self.assertIn('hmac_key = "k"', open(hc.SECRET()).read())

    def test_check_login_needs_both_fields(self):
        hc.STDIN = '{"username": "root"}'
        with self.assertRaises(hc.Refused):
            hc.v_check_login(hc.load_cfg())


class MainTests(Sandbox):
    def _main(self, cmd):
        os.environ["SSH_ORIGINAL_COMMAND"] = cmd
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = hc.main(["pve-watchdog-hostcmd"])
        finally:
            del os.environ["SSH_ORIGINAL_COMMAND"]
        return code, json.loads(out.getvalue())

    def test_unknown_verb(self):
        code, r = self._main("bash -c id")
        self.assertEqual(code, 2)
        self.assertFalse(r["ok"])

    def test_extra_args_rejected(self):
        code, r = self._main("start 9002 9003")
        self.assertEqual(code, 2)

    def test_dry_run_prefix(self):
        self.go_live()
        self.conf("pve2", 9002)
        code, r = self._main("--dry-run start 9002")
        self.assertEqual(code, 0)
        self.assertTrue(r["result"]["dry_run"])
        self.assertEqual(self.ran, [])


if __name__ == "__main__":
    unittest.main()
