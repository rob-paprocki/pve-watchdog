"""Offline tests of the controller's decisions, using a pretend host and network.

Run:  PYTHONPATH=controller python3 -m unittest discover -s tests -v
"""
import copy
import os
import tempfile
import time
import unittest

from pve_watchdog.config import Config
from pve_watchdog.engine import Engine, sync
from pve_watchdog.hostlink import HostError
from pve_watchdog.notify import Notifier
from pve_watchdog.peerapi import PeerClient, PeerServer, ReplayGuard
from pve_watchdog.state import State

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "fixture_config.toml")) as f:
    EXAMPLE = f.read()

LIVE_CFG = EXAMPLE.replace("dry_run = true", "dry_run = false").replace('mode = "dry-run"', 'mode = "live"')


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeHost:
    """Pretends to be node-b's host. Records every changing call."""

    def __init__(self, me="pve2", dry=False):
        self.me = me
        self.dry = dry
        self.calls = []
        self.fail = {}                  # verb -> HostError to raise
        self.alive_ips = set()
        self.st = {
            "node": me, "uptime": 99999, "gateway_ok": True,
            "quorum": {"quorate": False, "expected": 3, "total": 1, "quorum": 2,
                       "member_nodes": [me]},
            "pool": {"size": 100 * 2**30, "used": 10 * 2**30, "used_pct": 10.0, "free": 90 * 2**30},
            "maintenance": {"active": False},
            "cts": {
                "9001": {"mode": "live", "conf_on": ["pve1"], "local": False, "status": None,
                         "archive": {"created": 1_000_000.0 - 600, "used_bytes": 2**30}, "volumes": []},
                "9002": {"mode": "live", "conf_on": [me], "local": True, "status": "running",
                         "archive": {"created": 1_000_000.0 - 600, "used_bytes": 2**30},
                         "volumes": ["vm-9002-disk-0"]},
            },
        }

    def _do(self, verb, *args):
        self.calls.append((verb,) + args)
        if verb in self.fail:
            raise self.fail[verb]
        return {"dry_run": True, "would_run": [verb]} if self.dry else {"live": True}

    def status(self):
        return copy.deepcopy(self.st)

    def probe(self, ip):
        return {"ping": ip in self.alive_ips, "arp": False}

    def backup(self, vmid, mode="snapshot"): return self._do("backup", vmid, mode)
    def push(self, vmid, target): return self._do("push", vmid, target)
    def start(self, vmid): return self._do("start", vmid)
    def stop(self, vmid): return self._do("stop", vmid)
    def force_quorum(self): return self._do("force_quorum")
    def claim(self, vmid, frm): return self._do("claim", vmid, frm)
    def release(self, vmid): return self._do("release", vmid)
    def restore(self, vmid): return self._do("restore", vmid)
    def free_orphans(self, vmid): return self._do("free_orphans", vmid)
    def adopt(self, vmid): return self._do("adopt", vmid)
    def refresh(self, vmid): return self._do("refresh", vmid)
    def activate(self, base, which): return self._do("activate", base, which)

    def verbs(self):
        return [c[0] for c in self.calls]


class FakeProbes:
    def __init__(self):
        self.up = set()             # IPs that answer ping
        self.tcp_up = set()

    def ping(self, ip, timeout=2):
        return ip in self.up

    def tcp(self, ip, port, timeout=2):
        return ip in self.tcp_up


class FakePeers:
    def __init__(self):
        self.reachable = {}         # node name -> status dict
        self.actions = []
        self.action_reply = {"ok": True, "result": {"restore": {"live": True}, "start": {"live": True}}}

    def status(self, node, timeout=3):
        return self.reachable.get(node["name"])

    def action(self, node, verb, vmid, timeout=0):
        self.actions.append((node["name"], verb, vmid))
        if isinstance(self.action_reply, Exception):
            raise self.action_reply
        return self.action_reply


def make(cfg_text=LIVE_CFG, me="pve2", dry=False):
    cfg = Config(cfg_text, 'hmac_key = "k"\ndiscord_webhook = ""\n', me=me)
    clock = Clock()
    host, probes, peers = FakeHost(me, dry), FakeProbes(), FakePeers()
    d = tempfile.mkdtemp()
    state = State(os.path.join(d, "state.json"))
    sent = []
    notifier = Notifier("http://x", me, os.path.join(d, "q.jsonl"), state.data,
                        sender=lambda m: sent.append(m) or True, pause=0)
    eng = Engine(cfg, host, peers, probes, notifier, state, clock=clock)
    eng.wake = lambda mac, bcast: None      # never send real magic packets from tests
    eng.sent = sent
    return eng, host, probes, peers, clock


def ticks(eng, clock, n, step=10):
    for _ in range(n):
        eng.tick()
        clock.t += step


def queued(eng):
    eng.notify.flush()
    return "\n".join(eng.sent)


class TakeoverTests(unittest.TestCase):
    def test_peer_down_full_takeover_with_force_quorum(self):
        eng, host, probes, peers, clock = make()
        ticks(eng, clock, 5)                      # 5 failed rounds: not yet down
        self.assertIsNone(eng.state.peer("pve1")["down_since"])
        ticks(eng, clock, 1)                      # 6th round: declared down
        self.assertIsNotNone(eng.state.peer("pve1")["down_since"])
        self.assertEqual(host.verbs(), [])        # but nothing happens before the 90 s delay
        ticks(eng, clock, 8)                      # 80 s after "down": still waiting
        self.assertEqual(host.verbs(), [])
        ticks(eng, clock, 1)                      # 90 s after "down": take over
        self.assertEqual(host.verbs(), ["force_quorum", "claim", "restore", "start"])
        self.assertEqual(host.calls[1], ("claim", 9001, "pve1"))
        self.assertIn("ENTERING FORCE MODE", queued(eng))
        ticks(eng, clock, 5)                      # does not repeat in the same episode
        self.assertEqual(host.verbs().count("claim"), 1)

    def test_no_force_quorum_when_already_quorate(self):
        eng, host, probes, peers, clock = make(me="pve2")
        host.st["quorum"]["quorate"] = True       # e.g. node-a taking over node-b
        ticks(eng, clock, 17)
        self.assertEqual(host.verbs(), ["claim", "restore", "start"])

    def test_any_single_signal_keeps_peer_alive(self):
        eng, host, probes, peers, clock = make()
        probes.tcp_up.add("192.168.100.249")        # only the web port answers
        ticks(eng, clock, 30)
        self.assertIsNone(eng.state.peer("pve1")["down_since"])
        self.assertEqual(host.verbs(), [])

    def test_ct_liveness_guard_aborts(self):
        """Cluster link cut, but the CT itself still answers: never start a copy."""
        eng, host, probes, peers, clock = make()
        host.alive_ips.add("192.168.100.231")
        ticks(eng, clock, 40)
        self.assertNotIn("claim", host.verbs())
        self.assertIn("ALIVE", queued(eng))
        host.alive_ips.clear()                    # it really dies later -> takeover
        ticks(eng, clock, 1)
        self.assertEqual(host.verbs()[-3:], ["claim", "restore", "start"])

    def test_liveness_probe_error_counts_as_alive(self):
        eng, host, probes, peers, clock = make()
        host.probe = lambda ip: (_ for _ in ()).throw(HostError("boom"))
        ticks(eng, clock, 40)
        self.assertNotIn("claim", host.verbs())

    def test_isolated_node_does_not_take_over(self):
        eng, host, probes, peers, clock = make()
        host.st["gateway_ok"] = False
        ticks(eng, clock, 40)
        self.assertNotIn("claim", host.verbs())
        self.assertIn("NOT taking over", queued(eng))

    def test_maintenance_suppresses_and_expires(self):
        eng, host, probes, peers, clock = make()
        host.st["maintenance"] = {"active": True, "until": clock.t + 300}
        ticks(eng, clock, 40)
        self.assertNotIn("claim", host.verbs())
        host.st["maintenance"] = {"active": False}
        ticks(eng, clock, 1)
        self.assertIn("claim", host.verbs())
        self.assertIn("maintenance mode ended", queued(eng))

    def test_peer_returns_during_delay(self):
        eng, host, probes, peers, clock = make()
        ticks(eng, clock, 10)                     # declared down, waiting the delay
        probes.up.add("192.168.100.249")            # comes back
        ticks(eng, clock, 20)
        self.assertEqual(host.verbs(), [])
        self.assertIn("reachable again", queued(eng))

    def test_peer_returns_at_the_last_moment(self):
        eng, host, probes, peers, clock = make()
        ticks(eng, clock, 14)                     # down, delay almost over
        calls = []

        def ping(ip, timeout=2):                  # this tick: observe() sees it dead,
            calls.append(ip)                      # the re-check right before claiming
            return len(calls) > 1                 # sees it answering again
        probes.ping = ping
        ticks(eng, clock, 1)
        self.assertNotIn("claim", host.verbs())

    def test_stale_copy_still_taken_over_with_alert(self):
        eng, host, probes, peers, clock = make()
        host.st["cts"]["9001"]["archive"]["created"] = clock.t - 5 * 3600
        ticks(eng, clock, 16)
        self.assertIn("claim", host.verbs())
        self.assertIn("STALE", queued(eng))

    def test_no_copy_no_takeover(self):
        eng, host, probes, peers, clock = make()
        host.st["cts"]["9001"]["archive"] = None
        ticks(eng, clock, 20)
        self.assertNotIn("claim", host.verbs())
        self.assertIn("no copy", queued(eng))

    def test_failed_step_retries_three_times_then_stops(self):
        eng, host, probes, peers, clock = make()
        host.fail["restore"] = HostError("pool full", refused=True)
        ticks(eng, clock, 30)
        self.assertEqual(host.verbs().count("restore"), 3)

    def test_dry_run_plans_once(self):
        eng, host, probes, peers, clock = make(cfg_text=EXAMPLE, dry=True)
        ticks(eng, clock, 30)
        self.assertEqual(host.verbs().count("claim"), 1)
        self.assertIn("DRY-RUN: would have taken over CT 9001", queued(eng))

    def test_conflict_alert(self):
        eng, host, probes, peers, clock = make()
        probes.up.add("192.168.100.249")
        host.st["cts"]["9001"]["conf_on"] = ["pve1", "pve2"]
        ticks(eng, clock, 2)
        self.assertIn("MORE THAN ONE node", queued(eng))


class AdoptTests(unittest.TestCase):
    def test_live_ct_with_onboot_gets_adopted_once_status_changes(self):
        eng, host, probes, peers, clock = make()
        probes.up.add("192.168.100.249")
        host.st["cts"]["9002"]["onboot"] = True
        ticks(eng, clock, 1)
        self.assertEqual(host.calls, [("adopt", 9002)])

    def test_dry_run_ct_not_adopted(self):
        eng, host, probes, peers, clock = make(cfg_text=EXAMPLE, dry=True)
        probes.up.add("192.168.100.249")
        host.st["cts"]["9002"]["onboot"] = True
        ticks(eng, clock, 1)
        self.assertEqual(host.calls, [])


class IsolationTests(unittest.TestCase):
    def test_isolated_node_stops_its_cts_then_recovers(self):
        eng, host, probes, peers, clock = make()
        host.st["gateway_ok"] = False
        ticks(eng, clock, 12)                     # 120 s
        self.assertNotIn("stop", host.verbs())
        ticks(eng, clock, 1)
        self.assertEqual(host.verbs().count("stop"), 1)
        self.assertEqual(host.calls[-1], ("stop", 9002))
        host.st["cts"]["9002"]["status"] = "stopped"
        host.st["gateway_ok"] = True              # network back, peer did not take it
        probes.up.add("192.168.100.249")
        ticks(eng, clock, 1)
        self.assertEqual(host.calls[-1], ("start", 9002))

    def test_isolation_recovery_leaves_ct_stopped_if_peer_runs_it(self):
        eng, host, probes, peers, clock = make()
        host.st["gateway_ok"] = False
        ticks(eng, clock, 14)
        host.st["cts"]["9002"]["status"] = "stopped"
        host.st["gateway_ok"] = True
        probes.up.add("192.168.100.249")
        host.alive_ips.add("192.168.100.232")       # peer started its copy
        ticks(eng, clock, 1)
        self.assertNotEqual(host.calls[-1], ("start", 9002))


class FailbackTests(unittest.TestCase):
    def _healthy_peer(self, eng, probes, peers, host):
        probes.up.add("192.168.100.249")
        peers.reachable["pve1"] = {"node": "pve1", "host": {}}
        host.st["quorum"]["member_nodes"] = ["pve1", "pve2"]
        host.st["quorum"].update({"quorate": True, "expected": 3, "total": 3, "quorum": 2})
        # CT 9001 now lives here after an earlier failover
        host.st["cts"]["9001"].update({"conf_on": ["pve2"], "local": True, "status": "running"})

    def test_auto_failback_after_10_minutes(self):
        eng, host, probes, peers, clock = make()
        self._healthy_peer(eng, probes, peers, host)
        ticks(eng, clock, 59)                     # 9 min 50 s
        self.assertNotIn("release", host.verbs())
        ticks(eng, clock, 2)
        self.assertEqual(host.verbs(), ["stop", "backup", "push", "release"])
        self.assertEqual(host.calls[1], ("backup", 9001, "stop"))
        self.assertEqual(peers.actions, [("pve1", "free_orphans", 9001),
                                         ("pve1", "restore_start", 9001)])
        self.assertIn("back on its preferred node", queued(eng))

    def test_failback_rollback_when_peer_cannot_restore(self):
        eng, host, probes, peers, clock = make()
        self._healthy_peer(eng, probes, peers, host)
        peers.action_reply = {"ok": False, "error": "pool full"}
        ticks(eng, clock, 62)
        self.assertEqual(host.verbs(), ["stop", "backup", "push", "release", "restore", "start"])
        self.assertIn("failback of CT 9001 failed", queued(eng))

    def test_lost_reply_but_peer_took_it_no_rollback(self):
        eng, host, probes, peers, clock = make()
        self._healthy_peer(eng, probes, peers, host)
        peers.action_reply = TimeoutError("connection reset")

        def sleep(s):                       # meanwhile the peer finishes its restore
            clock.t += s
            host.st["cts"]["9001"].update({"conf_on": ["pve1"], "local": False})
        eng.sleep = sleep
        ticks(eng, clock, 62)
        self.assertEqual(host.verbs(), ["stop", "backup", "push", "release"])   # no restore/start here
        self.assertIn("not rolling back", queued(eng))

    def test_lost_reply_and_nothing_registered_rolls_back_after_wait(self):
        eng, host, probes, peers, clock = make()
        self._healthy_peer(eng, probes, peers, host)
        peers.action_reply = TimeoutError("connection reset")
        waited = []
        eng.sleep = lambda s: (waited.append(s), setattr(clock, "t", clock.t + s))
        orig_release = host.release

        def release_and_vanish(vmid):           # after release: registered nowhere
            r = orig_release(vmid)
            host.st["cts"]["9001"].update({"conf_on": [], "local": False})
            return r
        host.release = release_and_vanish
        ticks(eng, clock, 61)
        self.assertGreaterEqual(sum(waited), 15 * 60)
        self.assertEqual(host.verbs()[-2:], ["restore", "start"])

    def test_manual_failback_only_alerts(self):
        eng, host, probes, peers, clock = make(cfg_text=LIVE_CFG.replace('failback = "auto"', 'failback = "manual"'))
        self._healthy_peer(eng, probes, peers, host)
        ticks(eng, clock, 70)
        self.assertEqual(host.verbs(), [])
        self.assertIn("pve-watchdog failback 9001", queued(eng))

    def test_never_failback(self):
        eng, host, probes, peers, clock = make(cfg_text=LIVE_CFG.replace('failback = "auto"', 'failback = "never"'))
        self._healthy_peer(eng, probes, peers, host)
        ticks(eng, clock, 70)
        self.assertEqual(host.verbs(), [])

    def test_votes_alert_when_wrong_after_return(self):
        eng, host, probes, peers, clock = make()
        self._healthy_peer(eng, probes, peers, host)
        host.st["cts"]["9001"].update({"conf_on": ["pve1"], "local": False, "status": None})
        host.st["quorum"].update({"expected": 1, "total": 3, "quorum": 1})
        ticks(eng, clock, 1)
        self.assertIn("votes look wrong", queued(eng))


class BootTests(unittest.TestCase):
    def test_boot_starts_local_stopped_ct(self):
        eng, host, probes, peers, clock = make()
        host.st["uptime"] = 60
        host.st["cts"]["9002"]["status"] = "stopped"
        eng.boot_arbitration(sleep=lambda s: setattr(clock, "t", clock.t + s))
        self.assertEqual(host.calls, [("start", 9002)])

    def test_boot_does_not_start_if_ip_answers(self):
        eng, host, probes, peers, clock = make()
        host.st["uptime"] = 60
        host.st["cts"]["9002"]["status"] = "stopped"
        host.alive_ips.add("192.168.100.232")
        eng.boot_arbitration(sleep=lambda s: setattr(clock, "t", clock.t + s))
        self.assertEqual(host.calls, [])

    def test_controller_restart_is_not_a_boot(self):
        eng, host, probes, peers, clock = make()
        host.st["cts"]["9002"]["status"] = "stopped"     # e.g. Rob stopped it on purpose
        eng.boot_arbitration()
        self.assertEqual(host.calls, [])


PAIR_CFG = LIVE_CFG.replace("id = 9001\n", 'id = 9001\nstandby = "container"\n', 1)


def make_pair(me="pve2", active="original", my_status="stopped"):
    """9001 (original, node-a) + 10001 (standby, node-b), seen from `me`."""
    eng, host, probes, peers, clock = make(cfg_text=PAIR_CFG, me=me)
    mine = "standby" if me == "pve2" else "original"
    other = "original" if mine == "standby" else "standby"
    ids = {"original": 9001, "standby": 10001}
    homes = {"original": "pve1", "standby": "pve2"}
    members = {
        mine: {"id": ids[mine], "home": me, "conf_on": [me], "local": True,
               "status": my_status, "onboot": False, "tagged": mine == "standby",
               "refreshed": {"archive_created": clock.t - 600}},
        other: {"id": ids[other], "home": homes[other], "conf_on": [homes[other]],
                "local": False, "status": None},
    }
    host.st["cts"]["9001"] = {"mode": "live", "standby": "container", "active": active,
                              "members": members,
                              "archive": {"created": clock.t - 600, "used_bytes": 2**30,
                                          "source": ids[active]}}
    return eng, host, probes, peers, clock


class PairTests(unittest.TestCase):
    def test_takeover_starts_standby_with_force_mode(self):
        eng, host, probes, peers, clock = make_pair()
        ticks(eng, clock, 16)
        self.assertEqual(host.calls, [("force_quorum",), ("activate", 9001, "standby")])
        self.assertIn("ENTERING FORCE MODE", queued(eng))
        ticks(eng, clock, 5)
        self.assertEqual(host.verbs().count("activate"), 1)      # once per episode

    def test_takeover_aborted_if_original_ip_answers(self):
        eng, host, probes, peers, clock = make_pair()
        host.alive_ips.add("192.168.100.231")
        ticks(eng, clock, 30)
        self.assertNotIn("activate", host.verbs())
        self.assertIn("ALIVE", queued(eng))

    def test_no_takeover_when_peer_alive(self):
        eng, host, probes, peers, clock = make_pair()
        probes.up.add("192.168.100.249")
        ticks(eng, clock, 30)
        self.assertEqual(host.calls, [])

    def _healthy_a(self, probes, peers, host):
        probes.up.add("192.168.100.249")
        peers.reachable["pve1"] = {"node": "pve1", "host": {}}
        host.st["quorum"].update({"quorate": True, "expected": 3, "total": 3, "quorum": 2,
                                  "member_nodes": ["pve1", "pve2"]})

    def test_failback_stops_standby_first_then_starts_original(self):
        eng, host, probes, peers, clock = make_pair(active="standby", my_status="running")
        self._healthy_a(probes, peers, host)
        ticks(eng, clock, 59)
        self.assertEqual(host.calls, [])
        ticks(eng, clock, 2)
        self.assertEqual(host.calls, [("stop", 10001), ("backup", 10001, "stop"),
                                      ("push", 9001, "pve1")])
        self.assertEqual(peers.actions, [("pve1", "refresh_activate", 9001)])
        self.assertIn("CT 9001 is back on pve1", queued(eng))

    def test_second_failback_soon_after_a_successful_one(self):
        """A successful failback must not block the next one for an hour."""
        eng, host, probes, peers, clock = make_pair(active="standby", my_status="running")
        self._healthy_a(probes, peers, host)
        e = host.st["cts"]["9001"]
        ticks(eng, clock, 61)
        self.assertEqual(host.verbs().count("stop"), 1)
        e["active"], e["members"]["standby"]["status"] = "original", "stopped"   # it worked
        ticks(eng, clock, 10)
        self.assertEqual(host.verbs().count("stop"), 1)
        # 20 minutes later the standby is active again (another outage) ...
        clock.t += 20 * 60
        e["active"], e["members"]["standby"]["status"] = "standby", "running"
        ticks(eng, clock, 1)
        self.assertEqual(host.verbs().count("stop"), 2)       # not blocked for an hour

    def test_failback_failure_restarts_standby(self):
        eng, host, probes, peers, clock = make_pair(active="standby", my_status="running")
        self._healthy_a(probes, peers, host)
        peers.action_reply = {"ok": False, "error": "pool full"}
        ticks(eng, clock, 61)
        self.assertEqual(host.calls[-1], ("activate", 9001, "standby"))
        self.assertIn("failback of CT 9001 failed", queued(eng))

    def test_failback_lost_reply_but_original_active_no_rollback(self):
        eng, host, probes, peers, clock = make_pair(active="standby", my_status="running")
        self._healthy_a(probes, peers, host)
        peers.action_reply = TimeoutError("reset")

        def sleep(s):
            clock.t += s
            host.st["cts"]["9001"]["active"] = "original"
        eng.sleep = sleep
        ticks(eng, clock, 61)
        self.assertNotIn("activate", host.verbs())

    def test_manual_failback_only_alerts(self):
        eng, host, probes, peers, clock = make_pair(active="standby", my_status="running")
        eng.cfg.raw["policy"]["failback"] = "manual"
        self._healthy_a(probes, peers, host)
        ticks(eng, clock, 70)
        self.assertEqual(host.calls, [])
        self.assertIn("pve-watchdog failback 9001", queued(eng))

    def test_sync_backs_up_active_and_refreshes_standby(self):
        eng, host, probes, peers, clock = make_pair(me="pve1", my_status="running")
        host.st["cts"]["9001"]["archive"]["created"] = clock.t - 31 * 60
        peers.reachable["pve2"] = {"host": {"pool": {"size": 100 * 2**30, "used": 10 * 2**30}}}
        sync(eng.cfg, host, peers, eng.notify, eng.state, only=9001, clock=clock)
        self.assertEqual(host.calls, [("backup", 9001, "snapshot"), ("push", 9001, "pve2")])
        self.assertEqual(peers.actions, [("pve2", "refresh", 9001)])

    def test_sync_never_from_the_passive_copy(self):
        eng, host, probes, peers, clock = make_pair(me="pve2", my_status="stopped")
        sync(eng.cfg, host, peers, eng.notify, eng.state, clock=clock)
        self.assertEqual(peers.actions, [])
        self.assertNotIn("backup", host.verbs())

    def test_sync_after_failback_takes_new_backup_of_original(self):
        # archive still from the standby's final backup -> original must be backed up anew
        eng, host, probes, peers, clock = make_pair(me="pve1", my_status="running")
        host.st["cts"]["9001"]["archive"].update({"source": 10001, "created": clock.t - 60})
        peers.reachable["pve2"] = {"host": {}}
        sync(eng.cfg, host, peers, eng.notify, eng.state, only=9001, clock=clock)
        self.assertEqual(host.calls[0], ("backup", 9001, "snapshot"))

    def test_boot_starts_only_the_active_copy(self):
        eng, host, probes, peers, clock = make_pair(me="pve1", active="standby")
        host.st["uptime"] = 60
        eng.boot_arbitration(sleep=lambda s: setattr(clock, "t", clock.t + s))
        self.assertEqual(host.calls, [])                        # standby is active: stay off
        eng2, host2, _, _, clock2 = make_pair(me="pve1", active="original")
        host2.st["uptime"] = 60
        eng2.boot_arbitration(sleep=lambda s: setattr(clock2, "t", clock2.t + s))
        self.assertEqual(host2.calls, [("activate", 9001, "original")])

    def test_both_copies_running_alarm(self):
        eng, host, probes, peers, clock = make_pair(me="pve2", active="standby",
                                                    my_status="running")
        probes.up.add("192.168.100.249")
        peers.reachable["pve1"] = {"host": {"cts": {"9001": {"members": {
            "original": {"status": "running"}}}}}}
        ticks(eng, clock, 1)
        self.assertIn("BOTH copies of CT 9001 are running", queued(eng))

    def test_file_mode_logic_ignores_pairs(self):
        eng, host, probes, peers, clock = make_pair(active="standby", my_status="running")
        probes.up.add("192.168.100.249")
        ticks(eng, clock, 3)
        self.assertNotIn("claim", host.verbs())
        self.assertNotIn("free_orphans", host.verbs())


WOL_CFG = LIVE_CFG                     # the example config has wol_mac + wake_on_lan = true
NO_WOL_CFG = LIVE_CFG.replace("wake_on_lan = true", "wake_on_lan = false")


class WakeTests(unittest.TestCase):
    def _make(self, cfg=WOL_CFG):
        eng, host, probes, peers, clock = make(cfg_text=cfg)
        eng.sent_wol = []
        eng.wake = lambda mac, bcast: eng.sent_wol.append(mac)
        host.st["quorum"]["quorate"] = True          # keep takeover out of the way
        host.st["cts"] = {}
        return eng, host, probes, clock

    def test_wakes_dead_peer_then_repeats_then_gives_up(self):
        eng, host, probes, clock = self._make()
        ticks(eng, clock, 6)                          # declared down
        self.assertEqual(eng.sent_wol, [])
        ticks(eng, clock, 6)                          # 60 s later
        self.assertEqual(eng.sent_wol, ["00:00:5e:00:53:01"])
        ticks(eng, clock, 25)                         # < 5 min: no repeat yet
        self.assertEqual(len(eng.sent_wol), 1)
        ticks(eng, clock, 6 * 30 * 2)                 # an hour
        self.assertEqual(len(eng.sent_wol), 6)        # wake_tries
        self.assertIn("sent Wake-on-LAN to pve1", queued(eng))

    def test_count_resets_when_peer_returns(self):
        eng, host, probes, clock = self._make()
        ticks(eng, clock, 13)
        probes.up.add("192.168.100.249")
        ticks(eng, clock, 1)
        self.assertEqual(eng.state.peer("pve1")["wake_sent"], 0)

    def test_no_wake_during_maintenance(self):
        eng, host, probes, clock = self._make()
        host.st["maintenance"] = {"active": True, "until": clock.t + 9999}
        ticks(eng, clock, 40)
        self.assertEqual(eng.sent_wol, [])

    def test_no_wake_when_i_am_cut_off(self):
        eng, host, probes, clock = self._make()
        host.st["gateway_ok"] = False
        ticks(eng, clock, 40)
        self.assertEqual(eng.sent_wol, [])

    def test_off_by_default_and_in_dry_run(self):
        eng, host, probes, clock = self._make(cfg=NO_WOL_CFG)     # switched off
        ticks(eng, clock, 40)
        self.assertEqual(eng.sent_wol, [])
        eng, host, probes, clock = self._make(cfg=WOL_CFG.replace("dry_run = false", "dry_run = true"))
        ticks(eng, clock, 40)
        self.assertEqual(eng.sent_wol, [])

    def test_magic_packet_format(self):
        from pve_watchdog.wol import magic_packet
        pkt = magic_packet("00:00:5e:00:53:01")
        self.assertEqual(len(pkt), 102)
        self.assertEqual(pkt[:6], b"\xff" * 6)
        self.assertEqual(pkt[6:12], bytes.fromhex("00005e005301"))


class SyncTests(unittest.TestCase):
    def test_sync_due_ct_backs_up_and_pushes(self):
        eng, host, probes, peers, clock = make()
        host.st["cts"]["9002"]["archive"]["created"] = clock.t - 31 * 60
        peers.reachable["pve1"] = {"host": {"pool": {"size": 100 * 2**30, "used": 10 * 2**30}}}
        sync(eng.cfg, host, peers, eng.notify, eng.state, clock=clock)
        self.assertEqual(host.verbs(), ["backup", "push"])
        self.assertEqual(host.calls[1], ("push", 9002, "pve1"))

    def test_missed_peer_gets_latest_copy_without_new_backup(self):
        eng, host, probes, peers, clock = make()
        host.st["cts"]["9002"]["archive"]["created"] = clock.t - 31 * 60
        sync(eng.cfg, host, peers, eng.notify, eng.state, clock=clock)   # peer unreachable
        self.assertEqual(host.verbs(), ["backup"])
        host.st["cts"]["9002"]["archive"]["created"] = clock.t           # fresh local backup
        clock.t += 60
        peers.reachable["pve1"] = {"host": {"pool": {"size": 100 * 2**30, "used": 10 * 2**30}}}
        sync(eng.cfg, host, peers, eng.notify, eng.state, clock=clock)
        self.assertEqual(host.verbs(), ["backup", "push"])               # resent, no new backup
        clock.t += 60
        sync(eng.cfg, host, peers, eng.notify, eng.state, clock=clock)
        self.assertEqual(host.verbs(), ["backup", "push"])               # delivered: nothing more

    def test_sync_not_due(self):
        eng, host, probes, peers, clock = make()
        sync(eng.cfg, host, peers, eng.notify, eng.state, clock=clock)
        self.assertEqual(host.verbs(), [])

    def test_sync_refuses_to_fill_peer_pool(self):
        eng, host, probes, peers, clock = make()
        host.st["cts"]["9002"]["archive"]["created"] = clock.t - 31 * 60
        peers.reachable["pve1"] = {"host": {"pool": {"size": 100 * 2**30, "used": 80 * 2**30}}}
        sync(eng.cfg, host, peers, eng.notify, eng.state, clock=clock)
        self.assertEqual(host.verbs(), ["backup"])
        self.assertIn("Not sending the copy", queued(eng))

    def test_failed_sync_alerts(self):
        eng, host, probes, peers, clock = make()
        host.st["cts"]["9002"]["archive"] = None
        host.fail["backup"] = HostError("vzdump failed")
        sync(eng.cfg, host, peers, eng.notify, eng.state, clock=clock)
        self.assertIn("replication of CT 9002 failed", queued(eng))


class SigningTests(unittest.TestCase):
    """Real round trips between a client and a server on this machine."""

    def setUp(self):
        self.actions = []
        self.srv = PeerServer("pve1", "127.0.0.1", 0, b"k", lambda: {"hello": 1},
                              lambda frm, verb, vmid: self.actions.append((frm, verb, vmid)) or "ok")
        self.srv.start()
        self.port = self.srv.httpd.server_address[1]
        self.node = {"name": "pve1", "controller_ip": "127.0.0.1"}

    def tearDown(self):
        self.srv.httpd.shutdown()

    def test_round_trip(self):
        c = PeerClient(b"k", self.port, "pve2")
        self.assertEqual(c.status(self.node), {"hello": 1})
        self.assertEqual(c.action(self.node, "restore_start", 9001)["result"], "ok")
        self.assertEqual(self.actions, [("pve2", "restore_start", 9001)])

    def test_wrong_key_rejected(self):
        self.assertIsNone(PeerClient(b"wrong", self.port, "x").status(self.node))

    def test_request_for_other_node_rejected(self):
        c = PeerClient(b"k", self.port, "pve2")
        self.assertIsNone(c.status({"name": "pve2", "controller_ip": "127.0.0.1"}))

    def test_reload_is_an_allowed_action(self):
        c = PeerClient(b"k", self.port, "pve2")
        self.assertEqual(c.action(self.node, "reload", 0)["result"], "ok")
        self.assertEqual(self.actions[-1], ("pve2", "reload", 0))

    def test_disallowed_verb(self):
        c = PeerClient(b"k", self.port, "pve2")
        self.assertFalse(c.action(self.node, "rm_rf", 9001)["ok"])
        self.assertEqual(self.actions, [])

    def test_replay_and_staleness(self):
        g = ReplayGuard(started=time.time() - 5)
        ts = str(time.time())
        self.assertTrue(g.fresh(ts, "sig1"))
        self.assertFalse(g.fresh(ts, "sig1"))                       # same request twice
        self.assertFalse(g.fresh(str(time.time() - 600), "sig2"))   # too old
        self.assertFalse(g.fresh(str(time.time() - 30), "sig3"))    # before server start

    def test_forged_reply_rejected(self):
        import urllib.request as ur
        c = PeerClient(b"k", self.port, "pve2")
        real = ur.urlopen

        class Fake:
            headers = {"X-WD-Sig": "0" * 64}
            def read(self, n=-1): return b'{"hello": 2}'
            def __enter__(self): return self
            def __exit__(self, *a): pass
        ur.urlopen = lambda req, timeout=0: Fake()
        try:
            self.assertIsNone(c.status(self.node))
        finally:
            ur.urlopen = real

    def test_bad_content_length(self):
        import http.client
        h = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h.putrequest("POST", "/action")
        h.putheader("Content-Length", "-5")
        h.endheaders()
        self.assertEqual(h.getresponse().status, 413)


class NotifyTests(unittest.TestCase):
    def test_alert_once_and_queue_when_offline(self):
        d = tempfile.mkdtemp()
        st = {}
        down = {"v": True}
        sent = []

        def sender(m):
            if down["v"]:
                raise OSError("no internet")
            sent.append(m)
            return True
        n = Notifier("http://x", "pve2", os.path.join(d, "q"), st, sender=sender, pause=0)
        n.alert("k", "problem")
        n.alert("k", "problem")                   # deduplicated
        n.flush()
        self.assertEqual(sent, [])
        down["v"] = False
        n.resolve("k", "fixed")
        n.flush()
        self.assertEqual(len(sent), 2)
        self.assertIn("problem", sent[0])
        self.assertIn("fixed", sent[1])


if __name__ == "__main__":
    unittest.main()
