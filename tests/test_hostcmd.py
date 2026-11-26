"""Offline tests of the host gatekeeper's safety rules (no Proxmox needed)."""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
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
REAL_RESTORE_OPTIONS = hc.restore_options
REAL_RUN = hc.run

# `pct help restore --verbose` as Proxmox VE 9.3 prints it (shortened: the real
# list has every container option). Older versions lack --allow-identity-mismatch.
PCT_HELP_RESTORE_93 = """USAGE: pct restore <vmid> <ostemplate> [OPTIONS]

  Create or restore a container.

  <vmid>     <integer> (100 - 999999999)
\t     The (unique) ID of the VM.

  --allow-identity-mismatch <boolean>\t(default=0)
\t     Allow overwriting an existing guest with a backup of another
\t     guest, as detected by a different 'uid' in 'meta'. The guest
\t     then keeps its own uid.

\t     Requires option(s): restore

  --dev[n]   [[path=]<Path>] [,deny-write=<1|0>] [,gid=<integer>]
\t     Device to pass through to the container

  --force    <boolean>
\t     Allow to overwrite existing container.

  --onboot   <boolean>\t (default=0)
\t     Specifies whether a container will be started during system
\t     bootup.

  --protection <boolean>   (default=0)
\t     Sets the protection flag of the container.

  --storage  <storage ID>   (default=local)
\t     Default Storage.

  --tags     <string>
\t     Tags of the Container. This is only meta information.

  --unique   <boolean>
\t     Assign a unique random ethernet address and do not adopt the
\t     guest uid of the backup.
"""
PCT_HELP_RESTORE_OLD = PCT_HELP_RESTORE_93.replace(
    PCT_HELP_RESTORE_93[PCT_HELP_RESTORE_93.index("  --allow-identity"):
                        PCT_HELP_RESTORE_93.index("  --dev[n]")], "")
OPTS_93 = hc.parse_help_options(PCT_HELP_RESTORE_93)
OPTS_OLD = hc.parse_help_options(PCT_HELP_RESTORE_OLD)          # Proxmox VE 8.x, 9.0 to 9.2
STANDBY = "pve-watchdog-standby"

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
        hc.restore_options = lambda: set(OPTS_93)       # as on Proxmox VE 9.3
        hc._RESTORE_OPTIONS = None
        # dpkg's lock files and /proc/locks: fakes, never the real machine's
        hc.DPKG_LOCKS = (self.root + "/lock-frontend", self.root + "/lock")
        for p in hc.DPKG_LOCKS:
            open(p, "w").close()
        hc.PROC_LOCKS = self.root + "/proc-locks"
        open(hc.PROC_LOCKS, "w").close()

    def dpkg_running(self):
        """Make /proc/locks say that apt holds dpkg's front-end lock."""
        st = os.stat(hc.DPKG_LOCKS[0])
        with open(hc.PROC_LOCKS, "w") as f:
            f.write("1: POSIX  ADVISORY  WRITE 4242 %02x:%02x:%d 0 EOF\n"
                    % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino))

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


class WorkDirTests(unittest.TestCase):
    def test_clear_work_handles_interrupted_backup(self):
        root = tempfile.mkdtemp()
        work, outside = os.path.join(root, "work"), os.path.join(root, "outside")
        os.makedirs(os.path.join(work, "vzdump-lxc-102-2026_11_08-15_25_55.tmp", "etc"))
        os.makedirs(outside)
        for name in ("vzdump-lxc-102-2026_11_08-15_25_55.tar.dat", "vzdump-lxc-102-old.tar.zst",
                     "vzdump-lxc-1020-keep.tar.zst", "vzdump-lxc-103-keep.tar.zst"):
            open(os.path.join(work, name), "w").close()
        open(os.path.join(outside, "precious"), "w").close()
        os.symlink(outside, os.path.join(work, "vzdump-lxc-102-link"))
        hc.clear_work(work, 102)
        self.assertEqual(sorted(os.listdir(work)),
                         ["vzdump-lxc-1020-keep.tar.zst", "vzdump-lxc-103-keep.tar.zst"])
        self.assertTrue(os.path.exists(os.path.join(outside, "precious")))   # link removed, not followed


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


class PairSandbox(Sandbox):
    """True-duplicate standbys: 9001 lives on node-a, its standby 10001 on node-b."""

    def setUp(self):
        super().setUp()
        self.write_cfg(PAIR)
        open(hc.ALLOW_LIVE, "w").close()
        self.remote = "stopped"
        hc.remote_status = lambda n, v: self.remote
        self.tags = {9001: ["test"]}          # the tags line of each config
        self.backup_tags = ["test"]           # the tags inside the backup being restored
        hc.conf_tags = lambda vmid, node_name=None: list(self.tags.get(vmid, []))
        self.archive = {"created": 5, "used_bytes": 2**30, "mode": "snapshot", "size": 10}

    def fake_run(self, cmd, timeout=600, check=True, input=None):
        """Also plays Proxmox: a restore or a pct set writes the tags into the config."""
        p = super().fake_run(cmd, timeout, check, input)
        if cmd[:2] == ["pct", "restore"]:
            self.tags[int(cmd[2])] = (cmd[cmd.index("--tags") + 1].split(";") if "--tags" in cmd
                                      else list(self.backup_tags))
        elif cmd[:2] == ["pct", "set"] and "--tags" in cmd:
            self.tags[int(cmd[2])] = cmd[cmd.index("--tags") + 1].split(";")
        elif cmd[:2] == ["pct", "set"] and cmd[-2:] == ["--delete", "tags"]:
            self.tags.pop(int(cmd[2]), None)
        return p

    def pct(self):
        return [c for c in self.ran if c and c[0] == "pct"]

    def restore_cmd(self):
        return next(c for c in self.pct() if c[:2] == ["pct", "restore"])


class PairTests(PairSandbox):
    def test_standby_id_collision_refused(self):
        self.write_cfg(PAIR.replace("id = 9002", "id = 10001"))
        with self.assertRaises(hc.Refused):
            hc.load_cfg()

    def test_create_standby_first_time(self):
        """A missing standby is created by a plain restore: no --force, and nothing to
        overwrite, so no identity option either. Tags and onboot 0 come with the restore,
        so no pct set follows it."""
        hc.v_refresh(hc.load_cfg(), "10001")
        cmds = self.pct()
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][:3], ["pct", "restore", "10001"])
        self.assertNotIn("--force", cmds[0])
        self.assertNotIn("--allow-identity-mismatch", cmds[0])
        self.assertEqual(cmds[0][6:], ["--onboot", "0", "--tags", "test;pve-watchdog-standby"])
        self.assertIn(STANDBY, self.tags[10001])

    def test_onboot_set_afterwards_when_the_restore_cannot(self):
        """A pct without --onboot on restore, or a restore that still left onboot: 1."""
        self.conf("pve2", 10001)
        self.tags[10001] = ["test", STANDBY]
        hc.restore_options = lambda: set(OPTS_93) - {"onboot"}
        hc.v_refresh(hc.load_cfg(), "10001")
        self.assertNotIn("--onboot", self.restore_cmd())
        self.assertIn(["pct", "set", "10001", "--onboot", "0"], self.pct())
        hc.restore_options = lambda: set(OPTS_93)
        with open(hc.conf_path("pve2", 10001), "a") as f:
            f.write("onboot: 1\n")
        self.ran.clear()
        hc.v_refresh(hc.load_cfg(), "10001")
        self.assertIn("--onboot", self.restore_cmd())
        self.assertIn(["pct", "set", "10001", "--onboot", "0"], self.pct())

    def test_refresh_existing_standby_uses_force(self):
        self.conf("pve2", 10001)
        self.tags[10001] = ["test", STANDBY]
        hc.v_refresh(hc.load_cfg(), "10001")
        self.assertEqual(self.restore_cmd()[-4:], ["--force", "1", "--allow-identity-mismatch", "1"])

    def _protected(self, node, vmid):
        p = self.conf(node, vmid)
        with open(p, "a") as f:
            f.write("protection: 1\n")

    def test_refresh_lifts_protection_on_tagged_standby(self):
        self._protected("pve2", 10001)
        self.tags[10001] = ["test", STANDBY]
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
        self.tags[10001] = [STANDBY]
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
        self.tags[10001] = ["test", STANDBY]
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

    def test_untagged_standby_id_is_never_touched(self):
        """An unrelated container sitting on the standby ID must not be overwritten,
        started or stopped (also with 9.3's identity option available)."""
        self.conf("pve2", 10001)
        self.tags[10001] = ["test"]                      # not ours
        for fn, args in ((hc.v_refresh, ("10001",)), (hc.v_activate, ("9001", "standby")),
                         (hc.v_stop, ("10001",))):
            with self.assertRaises(hc.Refused):
                fn(hc.load_cfg(), *args)
        self.assertEqual(self.pct(), [])

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


class IdentityTests(PairSandbox):
    """Proxmox VE 9.3 refuses to restore a backup over a guest with another uid
    unless --allow-identity-mismatch is given; older versions have no such option."""

    def test_parse_help(self):
        self.assertIn("allow-identity-mismatch", OPTS_93)
        self.assertNotIn("allow-identity-mismatch", OPTS_OLD)
        for opt in ("force", "storage", "tags", "unique", "dev"):
            self.assertIn(opt, OPTS_93)
            self.assertIn(opt, OPTS_OLD)
        self.assertNotIn("uid", OPTS_93)             # words inside descriptions don't count

    def test_older_pct_gets_no_identity_option(self):
        hc.restore_options = lambda: set(OPTS_OLD)
        self.conf("pve2", 10001)
        self.tags[10001] = ["test", STANDBY]
        hc.v_refresh(hc.load_cfg(), "10001")
        cmd = self.restore_cmd()
        self.assertEqual(cmd[-2:], ["--force", "1"])
        self.assertNotIn("--allow-identity-mismatch", cmd)

    def test_failback_overwrites_the_original_with_the_option(self):
        """During failback the original (9001, node-a) is rewritten from the standby's
        backup: same option, the standby's own tags, and no standby tag."""
        self.me = "pve1"
        self.conf("pve1", 9001)
        hc.role_set(9001, "standby")                     # the standby is the active copy
        self.tags[10001] = ["test", "extra", STANDBY]   # the standby's config, on node-b
        hc.v_refresh(hc.load_cfg(), "9001")
        cmd = self.restore_cmd()
        self.assertEqual(cmd[2], "9001")
        self.assertEqual(cmd[-6:], ["--tags", "test;extra", "--force", "1",
                                    "--allow-identity-mismatch", "1"])
        self.assertNotIn(STANDBY, self.tags[9001])

    def test_failback_original_without_tags_loses_the_standby_tag(self):
        self.me = "pve1"
        self.conf("pve1", 9001)
        hc.role_set(9001, "standby")
        self.tags[10001] = [STANDBY]                     # nothing but our tag
        self.backup_tags = [STANDBY]                     # so the backup brings only that
        hc.v_refresh(hc.load_cfg(), "9001")
        self.assertNotIn("--tags", self.restore_cmd())
        self.assertIn(["pct", "set", "9001", "--delete", "tags"], self.pct())
        self.assertNotIn(9001, self.tags)

    def test_identity_option_lifts_no_guard(self):
        """With the option available, every refusal still holds and nothing runs."""
        self.conf("pve2", 10001)
        cases = [("running", lambda: setattr(self, "status", "running")),
                 ("standby active", lambda: hc.role_set(9001, "standby")),
                 ("untagged", lambda: self.tags.__setitem__(10001, ["test"])),
                 ("wrong node", lambda: setattr(self, "me", "pve1"))]
        for name, make_it_unsafe in cases:
            with self.subTest(name):
                self.status, self.me = "stopped", "pve2"           # safe again ...
                hc.role_set(9001, "original")
                self.tags[10001] = ["test", STANDBY]
                make_it_unsafe()                                     # ... but for one thing
                with self.assertRaises(hc.Refused):
                    hc.v_refresh(hc.load_cfg(), "10001")
                self.assertEqual(self.pct(), [])

    def test_options_read_once_per_call(self):
        hc.restore_options = REAL_RESTORE_OPTIONS
        helps = []

        def run(cmd, timeout=600, check=True, input=None):
            if cmd[:3] == ["pct", "help", "restore"]:
                helps.append(cmd)

                class P:
                    returncode, stdout, stderr = 0, PCT_HELP_RESTORE_93, ""
                return P()
            return self.fake_run(cmd, timeout, check, input)
        hc.run = run
        self.assertIn("allow-identity-mismatch", hc.restore_options())
        self.assertIn("tags", hc.restore_options())
        self.assertEqual(len(helps), 1)

    def test_unreadable_options_stop_before_any_change(self):
        """If pct cannot even print its help, nothing is touched (not even protection)."""
        hc.restore_options = REAL_RESTORE_OPTIONS
        p = self.conf("pve2", 10001)
        with open(p, "a") as f:
            f.write("protection: 1\n")
        self.tags[10001] = [STANDBY]

        def run(cmd, timeout=600, check=True, input=None):
            self.ran.append(cmd)

            class P:
                returncode, stdout = (255, "") if cmd[:2] == ["pct", "help"] else (0, "")
                stderr = "Compilation failed in require"
            return P()
        hc.run = run
        with self.assertRaises(RuntimeError):
            hc.v_refresh(hc.load_cfg(), "10001")
        self.assertEqual(self.pct(), [["pct", "help", "restore", "--verbose"]])


class BackupOriginTests(PairSandbox):
    """For a pair, Proxmox's uid check is upside down (every right refresh is a
    mismatch), so --allow-identity-mismatch turns it off. The gatekeeper checks the
    right way round instead: the backup must be of the OTHER copy."""
    ORIGINAL, STANDBY_UID, STRANGER = "a" * 20, "b" * 20, "x" * 20

    def setUp(self):
        super().setUp()
        for node, vmid, uid in (("pve1", 9001, self.ORIGINAL), ("pve2", 10001, self.STANDBY_UID)):
            with open(self.conf(node, vmid), "a") as f:
                f.write("meta: rtime=1764000000,uid=%s\n" % uid)
        self.tags[10001] = ["test", STANDBY]
        self.backup_uid, self.tar_rc = self.ORIGINAL, 0       # the archive's own pct.conf
        sandbox_run = self.fake_run

        def run(cmd, timeout=600, check=True, input=None):
            if cmd[0] != "tar":
                return sandbox_run(cmd, timeout, check, input)
            self.ran.append(cmd)

            class P:
                returncode, stderr = self.tar_rc, ""
                stdout = "arch: amd64\n" + ("meta: rtime=1,uid=%s\n" % self.backup_uid
                                            if self.backup_uid else "")
            return P()
        hc.run = run

    def _protect_standby(self):
        with open(hc.conf_path("pve2", 10001), "a") as f:
            f.write("protection: 1\n")

    def test_backup_of_the_other_copy_is_restored(self):
        hc.v_refresh(hc.load_cfg(), "10001")
        self.assertIn(["tar", "-xOf", "/var/lib/pve-watchdog/archive/ct-9001.tar.zst",
                       "--occurrence=1", "--", "./etc/vzdump/pct.conf"], self.ran)
        self.assertEqual(self.restore_cmd()[-2:], ["--allow-identity-mismatch", "1"])

    def test_wrong_backup_refused_before_anything_changes(self):
        """Another guest's backup, or an old one of the standby itself: nothing runs, not
        even the protection lift."""
        self._protect_standby()
        for uid in (self.STRANGER, self.STANDBY_UID):
            with self.subTest(uid=uid):
                self.backup_uid = uid
                with self.assertRaises(hc.Refused) as e:
                    hc.v_refresh(hc.load_cfg(), "10001")
                self.assertIn("holds another guest's backup", str(e.exception))
                self.assertEqual(self.pct(), [])

    def test_failback_needs_the_standbys_backup(self):
        """During failback the original takes the standby's data: an old backup of the
        original itself (the push of the new one never came) is refused."""
        self.me = "pve1"
        hc.role_set(9001, "standby")
        self.backup_uid = self.ORIGINAL
        with self.assertRaises(hc.Refused):
            hc.v_refresh(hc.load_cfg(), "9001")
        self.assertEqual(self.pct(), [])
        self.backup_uid = self.STANDBY_UID
        hc.v_refresh(hc.load_cfg(), "9001")
        self.assertEqual(self.restore_cmd()[-2:], ["--allow-identity-mismatch", "1"])

    def test_unknown_uid_goes_on_as_before(self):
        """A backup from before 9.3 (no uid), an unreadable archive, or a copy without a
        uid: no check is possible, and refusing would bring the 9.3 outage back."""
        cases = {"backup without uid": lambda: setattr(self, "backup_uid", None),
                 "tar fails": lambda: setattr(self, "tar_rc", 2),
                 "original without uid": lambda: self.conf("pve1", 9001)}
        for name, make_it_unknown in cases.items():
            with self.subTest(name):
                self.backup_uid, self.tar_rc = self.STRANGER, 0      # would be refused ...
                make_it_unknown()                                    # ... if it were known
                self.ran.clear()
                open(hc.LOG, "w").close()
                hc.v_refresh(hc.load_cfg(), "10001")
                self.assertEqual(self.restore_cmd()[-2:], ["--allow-identity-mismatch", "1"])
                with open(hc.LOG) as f:
                    self.assertIn("backup origin not checked", f.read())

    def test_meta_parsing(self):
        uid = "abcdefghijklmnopqrs2"
        self.assertEqual(hc.meta_uid(["arch: amd64\n", "meta: rtime=5,uid=%s\n" % uid]), uid)
        self.assertEqual(hc.meta_uid(["meta: foo=1,uid=%s,rtime=5" % uid]), uid)
        self.assertIsNone(hc.meta_uid(["arch: amd64\n", "[pve:pending]\n",
                                       "meta: uid=%s\n" % uid]))        # pending: not its uid
        self.assertIsNone(hc.meta_uid(["meta: uid=%s,uid=%s\n" % (uid, uid)]))   # twice
        self.assertIsNone(hc.meta_uid(["meta: uid=NOT-A-UID\n"]))
        self.assertIsNone(hc.meta_uid(["meta: rtime=5\n"]))

    def test_reads_a_real_archive(self):
        """The real tar on a real .tar.zst laid out like vzdump's (pct.conf first)."""
        src = os.path.join(self.root, "ct")
        os.makedirs(os.path.join(src, "etc", "vzdump"))
        with open(os.path.join(src, "etc", "vzdump", "pct.conf"), "w") as f:
            f.write("arch: amd64\nmeta: rtime=1764000000,uid=%s\n[pve:pending]\n"
                    "meta: uid=%s\n" % (self.ORIGINAL, self.STRANGER))
        with open(os.path.join(src, "data"), "wb") as f:
            f.write(os.urandom(1 << 20))
        arc = os.path.join(self.root, "ct-9001.tar.zst")
        made = subprocess.run("tar -cf - -C %s ./etc/vzdump/pct.conf ./data | zstd -q -o %s"
                              % (src, arc), shell=True, capture_output=True)
        if made.returncode != 0:
            self.skipTest("no tar with zstd here")
        hc.run = REAL_RUN
        self.assertEqual(hc.archive_uid(arc), self.ORIGINAL)
        self.assertIsNone(hc.archive_uid(arc + ".missing"))


class TaggingTests(PairSandbox):
    """A standby must never be left without its tag: every later refresh would refuse it."""

    def test_tags_come_with_the_restore(self):
        self.conf("pve2", 10001)
        self.tags[10001] = [STANDBY]
        self.tags[9001] = ["web", "test"]                # the original's current tags
        hc.v_refresh(hc.load_cfg(), "10001")
        self.assertIn("--tags", self.restore_cmd())
        self.assertEqual(self.tags[10001], ["web", "test", STANDBY])
        self.assertEqual([c for c in self.pct() if c[1] == "set" and "--tags" in c], [])

    def test_a_failure_after_the_restore_cannot_strand_the_standby(self):
        """What happened on 9.3 upgrade night: the restore worked, then every pct set
        failed because Proxmox's Perl modules were half replaced. On 9.3 no pct set
        follows the restore any more; with a pct that can't take --onboot on restore,
        the one that does still fails, but the standby keeps its tag."""
        self.conf("pve2", 10001)
        self.tags[10001] = [STANDBY]
        real = self.fake_run

        def broken(cmd, timeout=600, check=True, input=None):
            if cmd[:2] == ["pct", "set"]:
                self.ran.append(cmd)
                raise RuntimeError("Compilation failed in require")
            return real(cmd, timeout, check, input)
        hc.run = broken
        hc.v_refresh(hc.load_cfg(), "10001")             # 9.3: nothing after the restore
        self.assertEqual([c for c in self.pct() if c[1] == "set"], [])
        hc.restore_options = lambda: set(OPTS_93) - {"onboot"}
        with self.assertRaises(RuntimeError):
            hc.v_refresh(hc.load_cfg(), "10001")
        self.assertIn(STANDBY, self.tags[10001])         # the restore itself tagged it
        hc.run = real                                    # packages fixed: next refresh
        self.ran.clear()
        hc.v_refresh(hc.load_cfg(), "10001")             # not refused
        self.assertTrue(self.restore_cmd())

    def test_pct_without_tags_option_tags_afterwards_with_retries(self):
        hc.restore_options = lambda: set(OPTS_OLD) - {"tags"}
        self.conf("pve2", 10001)
        self.tags[10001] = [STANDBY]
        real, fails = self.fake_run, [1, 2]

        def flaky(cmd, timeout=600, check=True, input=None):
            if cmd[:2] == ["pct", "set"] and "--tags" in cmd and fails:
                fails.pop()
                self.ran.append(cmd)
                raise RuntimeError("pct set failed")
            return real(cmd, timeout, check, input)
        hc.run = flaky
        with unittest.mock.patch.object(hc.time, "sleep", lambda s: None):
            hc.v_refresh(hc.load_cfg(), "10001")
        self.assertNotIn("--tags", self.restore_cmd())
        tag_sets = [c for c in self.pct() if c[1] == "set" and "--tags" in c]
        self.assertEqual(len(tag_sets), 3)
        self.assertEqual(tag_sets[-1], ["pct", "set", "10001", "--tags", "test;" + STANDBY])
        self.assertIn(STANDBY, self.tags[10001])

    def test_tagging_that_keeps_failing_fails_the_refresh(self):
        hc.restore_options = lambda: set(OPTS_OLD) - {"tags"}
        self.conf("pve2", 10001)
        self.tags[10001] = [STANDBY]
        real = self.fake_run

        def broken(cmd, timeout=600, check=True, input=None):
            if cmd[:2] == ["pct", "set"] and "--tags" in cmd:
                raise RuntimeError("pct set failed")
            return real(cmd, timeout, check, input)
        hc.run = broken
        with unittest.mock.patch.object(hc.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                hc.v_refresh(hc.load_cfg(), "10001")
        self.assertFalse(os.path.exists(hc.refreshed_path(10001)))

    def test_refusal_says_how_to_recover(self):
        """A leftover of a restore that stopped part-way is locked (Proxmox then refuses
        pct set and pct destroy), and a standby copied from a protected original is
        protected (pct destroy refuses it): the hint covers both, in the order to run."""
        self.conf("pve2", 10001)
        self.tags[10001] = ["test"]
        self.tags[9001] = ["web", "test"]                # the original's tags
        with self.assertRaises(hc.Refused) as e:
            hc.v_refresh(hc.load_cfg(), "10001")
        msg = str(e.exception)
        steps = ["pct unlock 10001", "pct set 10001 --tags 'web;test;%s'" % STANDBY,
                 "pct set 10001 --protection 0", "pct destroy 10001"]
        for step in steps:
            self.assertIn(step, msg)
        self.assertEqual([msg.index(s) for s in steps], sorted(msg.index(s) for s in steps))
        self.assertEqual(self.pct(), [])


class PackageUpgradeTests(PairSandbox):
    """No backup or refresh starts while dpkg/apt holds its lock (packages half installed)."""

    def test_backup_and_refresh_wait(self):
        self.dpkg_running()
        self.conf("pve2", 10001)
        self.tags[10001] = [STANDBY]
        for fn in (hc.v_backup, hc.v_refresh):
            with self.subTest(fn.__name__):
                with self.assertRaises(hc.Deferred) as e:
                    fn(hc.load_cfg(), "10001")
                self.assertIn("dpkg/apt", str(e.exception))
        self.assertEqual(self.ran, [])

    def test_deferral_is_its_own_reply(self):
        self.dpkg_running()
        self.conf("pve2", 10001)
        self.tags[10001] = [STANDBY]
        os.environ["SSH_ORIGINAL_COMMAND"] = "refresh 10001"
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = hc.main(["pve-watchdog-hostcmd"])
        finally:
            del os.environ["SSH_ORIGINAL_COMMAND"]
        r = json.loads(out.getvalue())
        self.assertEqual(code, 4)
        self.assertTrue(r["deferred"])
        self.assertNotIn("refused", r)
        self.assertIn("DEFERRED refresh 10001", open(hc.LOG).read())

    def test_no_wait_once_dpkg_is_done(self):
        self.conf("pve2", 10001)
        self.tags[10001] = [STANDBY]
        self.dpkg_running()
        open(hc.PROC_LOCKS, "w").close()                 # apt finished
        hc.v_refresh(hc.load_cfg(), "10001")
        self.assertTrue(self.restore_cmd())

    def test_restore_of_a_down_container_does_not_wait(self):
        """A file-mode restore only runs while the CT is down; waiting would extend that."""
        self.dpkg_running()
        self.write_cfg(LIVE)
        hc.v_restore(hc.load_cfg(), "9001")
        self.assertEqual(self.pct()[0][:3], ["pct", "restore", "9001"])

    def test_dry_run_is_not_held_back(self):
        self.dpkg_running()
        self.write_cfg(EXAMPLE.replace("id = 9001\n", 'id = 9001\nstandby = "container"\n', 1))
        r = hc.v_refresh(hc.load_cfg(), "10001")
        self.assertTrue(r["dry_run"])

    def test_status_reports_it(self):
        self.assertIsNone(hc.v_status(hc.load_cfg())["packages_busy"])
        self.dpkg_running()
        self.assertIn("pid 4242", hc.v_status(hc.load_cfg())["packages_busy"])

    def test_lock_seen_in_the_real_proc_locks_without_taking_it(self):
        if not os.path.exists("/proc/locks"):
            self.skipTest("no /proc/locks here")
        hc.PROC_LOCKS = "/proc/locks"
        holder = subprocess.Popen(
            [sys.executable, "-I", "-c",
             "import fcntl, sys, time; f = open(sys.argv[1], 'w'); fcntl.lockf(f, fcntl.LOCK_EX);"
             " print('locked', flush=True); time.sleep(60)", hc.DPKG_LOCKS[0]],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            self.assertIn("pid %d" % holder.pid, hc.packages_busy())
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertIsNone(hc.packages_busy())

    def test_waiters_and_other_files_do_not_count(self):
        def key(path):
            st = os.stat(path)
            return "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        with open(hc.PROC_LOCKS, "w") as f:
            f.write("1: -> POSIX  ADVISORY  WRITE 7 %s 0 EOF\n" % key(hc.DPKG_LOCKS[0]))
            f.write("2: POSIX  ADVISORY  WRITE 8 %s 0 EOF\n" % key(hc.CONFIG()))   # not dpkg's
        self.assertIsNone(hc.packages_busy())


class FailoverNeverWaitsTests(PairSandbox):
    """No failover step waits for dpkg/apt. An upgrade can sit at a prompt for hours,
    and the containers are down meanwhile. Only backups and refreshes wait."""

    def setUp(self):
        super().setUp()
        self.dpkg_running()
        self.assertIsNotNone(hc.packages_busy())         # the fake lock really is held

    def test_pair_takeover_starts_the_standby(self):
        self.conf("pve2", 10001)
        self.tags[10001] = ["test", STANDBY]
        hc.v_activate(hc.load_cfg(), "9001", "standby")
        self.assertEqual(self.pct(), [["pct", "start", "10001"]])
        self.assertEqual(hc.role_active(9001), "standby")

    def test_pair_standby_stops(self):
        self.conf("pve2", 10001)
        self.tags[10001] = ["test", STANDBY]
        self.status = "running"
        hc.v_stop(hc.load_cfg(), "10001")
        self.assertEqual(self.pct(), [["pct", "shutdown", "10001", "--timeout", "60"]])

    def test_file_mode_takeover_runs_every_step(self):
        """force-quorum, claim, restore and start: the whole file-mode takeover."""
        self.write_cfg(LIVE)
        src = self.conf("pve1", 9001)                    # registered on the dead node
        self.q["quorate"] = False
        self.pinging.add("192.168.100.1")                # the gateway answers, node-a doesn't
        flips = iter([False, True])
        hc.quorum = lambda cfg: dict(self.q, quorate=next(flips, True))
        with unittest.mock.patch.object(hc.time, "sleep", lambda s: None):
            self.assertTrue(hc.v_force_quorum(hc.load_cfg())["quorate"])
        self.assertIn(["pvecm", "expected", "1"], self.ran)
        hc.v_claim(hc.load_cfg(), "9001", "pve1")
        self.assertFalse(os.path.exists(src))           # parked
        hc.v_restore(hc.load_cfg(), "9001")
        self.assertEqual(self.pct()[0][:3], ["pct", "restore", "9001"])
        self.conf("pve2", 9001)                          # what the restore registered
        hc.v_start(hc.load_cfg(), "9001")
        self.assertEqual(self.pct()[-1], ["pct", "start", "9001"])

    def test_file_mode_stop(self):
        self.write_cfg(LIVE)
        self.conf("pve2", 9002)
        self.status = "running"
        hc.v_stop(hc.load_cfg(), "9002")
        self.assertEqual(self.pct(), [["pct", "shutdown", "9002", "--timeout", "60"]])


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

    def test_standby_id_taken_by_another_guest_refused(self):
        self.guests.append({"vmid": 10102, "type": "lxc", "node": "pve2", "tags": "mine"})
        self.guests.append({"vmid": 102, "type": "lxc", "node": "pve1"})
        text = EXAMPLE + ('\n[[ct]]\nid = 102\npreferred_node = "pve1"\nstandby = "container"\n'
                          'mode = "live"\n')
        text = text.replace("[cluster]\n", "[cluster]\nstandby_offset = 10000\n", 1)
        with self.assertRaises(hc.Refused) as e:
            self.put(text)
        self.assertIn("already belongs to another guest", str(e.exception))
        self.guests[-2]["tags"] = "mine;pve-watchdog-standby"     # it IS our standby: fine
        self.put(text)

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


class VolumeTests(Sandbox):
    def test_snapshot_referenced_volume_is_not_an_orphan(self):
        p = self.conf("pve2", 9002)
        with open(p, "a") as f:
            f.write("\n[before-upgrade]\nrootfs: local-lvm:vm-9002-disk-1,size=2G\n")
        self.assertEqual(hc.referenced_volumes(9002), {"vm-9002-disk-0", "vm-9002-disk-1"})


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
