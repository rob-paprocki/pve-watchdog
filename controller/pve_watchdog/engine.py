"""The decision-making: when to take over, when to stand down, when to fail back.

Read top to bottom, one "tick" (every 10 seconds) does this:

  1. LOOK      Ask my host for its status (quorum, which CTs are registered
               where, running/stopped, latest backup files). Probe each peer
               four ways: ping, Proxmox web port, corosync membership, and
               the peer's controller.
  2. COUNT     A peer is "down" only after 6 rounds in a row where ALL FOUR
               signals failed. Any single answer resets the count.
  3. ISOLATED? If I can reach neither the gateway nor any peer for 2 minutes,
               I am probably the one cut off: I stop my covered CTs so the
               other node can safely take over. When the network comes back I
               start them again, unless the other node now runs them.
  4. TAKEOVER  If a peer has been down for 90 more seconds, I can reach the
               gateway, and maintenance mode is off: for each CT that was
               registered on the dead peer, check the CT's OWN IP. If it still
               answers, the CT is alive somewhere, so abort and alert. Otherwise
               (force quorum if needed) claim it, restore it from my latest
               copy, and start it.
  5. FAILBACK  If I run a CT whose preferred node is another node, and that
               node has been healthy for 10 minutes: stop it, take a final
               backup, send it over, hand it over, and have that node start it.
  6. HOUSEKEEP Check votes are back to normal, flag config conflicts, clean up
               leftover disks of CTs that now live elsewhere, send alerts.

Every changing step goes through the host gatekeeper, which re-checks the
safety rules itself and, in dry-run, only reports what it would do.
"""
import fcntl
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from . import wol
from .hostlink import HostError

log = logging.getLogger("pve-watchdog.engine")


def _dry(*results):
    return any(isinstance(r, dict) and r.get("dry_run") for r in results)


class Engine:
    def __init__(self, cfg, host, peers, probes, notifier, state, clock=time.time,
                 failback_lock=None):
        self.failback_lock = failback_lock
        self.wake = wol.send                 # replaced in tests
        self.unclear_wait = 15 * 60          # how long to watch after a lost reply
        self.sleep = time.sleep
        self.cfg = cfg
        self.host = host
        self.peers = peers
        self.probes = probes
        self.notify = notifier
        self.state = state
        self.clock = clock
        self.peer_status = {}      # name -> last signed status from that controller
        self.local = None          # last host status

    # ------------------------------------------------------------------ helpers
    def _maintenance(self, st):
        m = st.get("maintenance") or {}
        active = bool(m.get("active"))
        was = self.state.data.get("maintenance_active", False)
        if was and not active:
            self.notify.info("maintenance mode ended (failover is armed again)")
        if active and not was:
            self.notify.info("maintenance mode is ON until %s (failover suppressed)"
                             % time.strftime("%H:%M", time.localtime(m.get("until", 0))))
        self.state.data["maintenance_active"] = active
        return active

    def _ct_entry(self, st, vmid, pair=False):
        """Status entry for a CT. File-mode code (pair=False) never sees
        standby-container CTs, so the two designs can't both act on one."""
        e = (st.get("cts") or {}).get(str(vmid))
        if not e or e.get("error"):
            return None
        if (e.get("standby") == "container") != pair:
            return None
        return e

    def _liveness(self, c):
        """True if the CT's own IP answers (ping or ARP) as seen by my host."""
        ip = c.get("liveness_ip")
        if not ip:
            return False
        try:
            r = self.host.probe(ip)
            return bool(r.get("ping") or r.get("arp"))
        except HostError as e:
            log.warning("liveness probe of %s failed to run (%s); treating as ALIVE", ip, e)
            return True        # unsure -> be safe -> do not start a second copy

    def _peer_reachable_now(self, p):
        t = self.cfg.probe("timeout")
        return (self.probes.ping(p["host_ip"], t) or
                self.peers.status(p) is not None)

    # --------------------------------------------------------------- 1+2: look
    def observe(self):
        st = self.host.status()
        self.local = st
        t = self.clock()
        rounds = self.cfg.probe("rounds")
        timeout = self.cfg.probe("timeout")
        members = st.get("quorum", {}).get("member_nodes", [])
        for p in self.cfg.peers():
            name = p["name"]
            ps = self.state.peer(name)
            # The three network checks run at the same time, so a dead peer
            # costs one timeout per round instead of three in a row.
            with ThreadPoolExecutor(max_workers=3) as pool:
                f_api = pool.submit(self.peers.status, p)
                f_ping = pool.submit(self.probes.ping, p["host_ip"], timeout)
                f_tcp = pool.submit(self.probes.tcp, p["host_ip"], self.cfg.probe("tcp_port"),
                                    timeout)
                api, pinged, tcp_ok = f_api.result(), f_ping.result(), f_tcp.result()
            self.peer_status[name] = api
            sig = {"ping": pinged, "tcp": tcp_ok, "member": name in members,
                   "controller": api is not None}
            ps["last_signals"] = sig
            if any(sig.values()):
                if ps["down_since"]:
                    answered = ", ".join(k for k, v in sig.items() if v)
                    self.notify.resolve("peer-down-" + name,
                                        "%s is reachable again (%s)" % (name, answered))
                    self.notify.resolve("no-gw-" + name, "takeover no longer needed")
                    for c in self.cfg.cts():
                        self.notify.resolve("alive-%d" % c["id"],
                                            "CT %d: aborted failover no longer pending" % c["id"])
                ps["fail_rounds"] = 0
                ps["down_since"] = None
                ps["wake_sent"] = 0
            else:
                ps["fail_rounds"] += 1
                log.info("peer %s: all probes failed (%d/%d)", name, ps["fail_rounds"], rounds)
                if ps["fail_rounds"] >= rounds and not ps["down_since"]:
                    ps["down_since"] = t
                    self.notify.alert("peer-down-" + name,
                                      "%s is DOWN (no ping, no web UI, not in cluster, "
                                      "no controller for %d rounds). Takeover in %ds unless "
                                      "it returns." % (name, rounds, self.cfg.probe("failover_delay")))
            healthy = sig["member"] and sig["controller"]
            if healthy and not ps.get("healthy_since"):
                ps["healthy_since"] = t
            elif not healthy:
                ps["healthy_since"] = None
        return st

    # -------------------------------------------------------------- 3: isolated
    def check_isolation(self, st, maintenance):
        t = self.clock()
        limit = self.cfg.policy("isolation_stop_after")
        peers_gone = all(not any(self.state.peer(p["name"]).get("last_signals", {}).get(k)
                                 for k in ("ping", "tcp", "controller"))
                         for p in self.cfg.peers())
        isolated = (not st.get("gateway_ok")) and peers_gone
        iso = self.state.data
        if not isolated:
            if iso.get("isolated_since"):
                self.notify.resolve("isolated", "network is back (gateway or peer reachable)")
            iso["isolated_since"] = None
            self.recover_demoted(st)
            return
        iso["isolated_since"] = iso.get("isolated_since") or t
        self.notify.alert("isolated", "I cannot reach the gateway or any peer. I will NOT take "
                                      "anything over. Stopping my covered CTs after %ds." % limit)
        if not limit or maintenance or t - iso["isolated_since"] < limit:
            return
        for c in self.cfg.cts():
            e = self._ct_entry(st, c["id"])
            if not e or not e.get("local") or e.get("status") != "running":
                continue
            cs = self.state.ct(c["id"])
            if cs.get("demoted"):
                continue
            try:
                r = self.host.stop(c["id"])
                cs["demoted"] = True
                self.notify.alert("demoted-%d" % c["id"],
                                  "%sstopped CT %d (%s) because this node is isolated, so the "
                                  "other node can take over without two copies running"
                                  % ("[would have] " if _dry(r) else "", c["id"], c.get("name", "")))
            except HostError as err:
                self.notify.alert("demote-failed-%d" % c["id"],
                                  "could not stop CT %d while isolated: %s" % (c["id"], err))

    def recover_demoted(self, st):
        for c in self.cfg.cts():
            if self.cfg.container_mode(c):
                continue
            cs = self.state.ct(c["id"])
            if not cs.get("demoted"):
                continue
            e = self._ct_entry(st, c["id"])
            if e and e.get("local") and e.get("status") == "stopped":
                if self._liveness(c):
                    self.notify.info("CT %d answers elsewhere; the other node took it over. "
                                     "Leaving my copy stopped." % c["id"])
                    cs["demoted"] = False
                    continue
                try:
                    self.host.start(c["id"])
                    self.notify.resolve("demoted-%d" % c["id"],
                                        "network back: restarted CT %d here" % c["id"])
                    cs["demoted"] = False
                except HostError as err:
                    log.warning("could not restart demoted CT %d: %s", c["id"], err)
            else:
                cs["demoted"] = False
                self.notify.resolve("demoted-%d" % c["id"],
                                    "CT %d is now registered elsewhere; not restarting it here"
                                    % c["id"])

    # -------------------------------------------------------------- 4: takeover
    def maybe_takeover(self, st, maintenance):
        if maintenance:
            return
        t = self.clock()
        for p in self.cfg.peers():
            ps = self.state.peer(p["name"])
            if not ps["down_since"] or t - ps["down_since"] < self.cfg.probe("failover_delay"):
                continue
            if not st.get("gateway_ok"):
                self.notify.alert("no-gw-" + p["name"],
                                  "%s looks down, but I cannot reach the gateway either, so I "
                                  "might be the isolated one. NOT taking over." % p["name"])
                continue
            self.notify.resolve("no-gw-" + p["name"], "gateway reachable again")
            for c in self.cfg.cts():
                e = self._ct_entry(st, c["id"])
                if not e or e.get("conf_on") != [p["name"]]:
                    continue
                cs = self.state.ct(c["id"])
                if cs.get("takeover_episode") == ps["down_since"]:
                    continue
                self.takeover_ct(c, p, ps, st, e)

    def takeover_ct(self, c, p, ps, st, e):
        vmid, t = c["id"], self.clock()
        cs = self.state.ct(vmid)
        if self._peer_reachable_now(p):
            log.info("CT %d: %s answered again just now; not taking over", vmid, p["name"])
            return
        if self._liveness(c):
            self.notify.alert("alive-%d" % vmid,
                              "%s is unreachable but CT %d (%s) still answers on %s, so it is "
                              "ALIVE. Failover aborted; I will keep checking."
                              % (p["name"], vmid, c.get("name", ""), c.get("liveness_ip")))
            return
        self.notify.resolve("alive-%d" % vmid, "CT %d no longer answers on its IP" % vmid)
        arc = e.get("archive")
        if not arc:
            self.notify.alert("nocopy-%d" % vmid,
                              "cannot take over CT %d: I have no copy of it" % vmid)
            return
        age_min = (t - arc["created"]) / 60
        if age_min > self.cfg.policy("stale_factor") * c.get("interval_min", 30):
            self.notify.info("⚠️ taking over CT %d with a STALE copy (%.0f min old)"
                             % (vmid, age_min))
        try:
            results = []
            if not st.get("quorum", {}).get("quorate"):
                self.notify.alert("force-quorum",
                                  "⚠️ ENTERING FORCE MODE: running `pvecm expected 1` so "
                                  "this node can act alone while %s is down." % p["name"])
                results.append(self.host.force_quorum())
            results.append(self.host.claim(vmid, p["name"]))
            results.append(self.host.restore(vmid))
            results.append(self.host.start(vmid))
        except HostError as err:
            cs["attempts"] = cs.get("attempts", 0) + 1
            self.notify.alert("takeover-failed-%d" % vmid,
                              "takeover of CT %d failed (attempt %d): %s"
                              % (vmid, cs["attempts"], err))
            if cs["attempts"] >= 3:
                cs["takeover_episode"] = ps["down_since"]       # stop retrying this episode
            return
        cs["takeover_episode"] = ps["down_since"]
        cs["attempts"] = 0
        self.notify.resolve("takeover-failed-%d" % vmid)
        if _dry(*results):
            self.notify.info("DRY-RUN: would have taken over CT %d (%s) from %s using a "
                             "%.0f-minute-old copy" % (vmid, c.get("name", ""), p["name"], age_min))
        else:
            self.notify.info("took over CT %d (%s) from %s; copy was %.0f minutes old"
                             % (vmid, c.get("name", ""), p["name"], age_min))

    # -------------------------------------------------------------- 5: failback
    def maybe_failback(self, st, maintenance):
        policy = self.cfg.policy("failback")
        if policy == "never" or maintenance:
            return
        t = self.clock()
        for c in self.cfg.cts():
            pref = c["preferred_node"]
            e = self._ct_entry(st, c["id"])
            if pref == self.cfg.me or not e or not e.get("local") or e.get("status") != "running":
                continue
            ps = self.state.peer(pref)
            hs = ps.get("healthy_since")
            if not hs or t - hs < self.cfg.policy("failback_stable_minutes") * 60:
                continue
            if policy == "manual":
                self.notify.alert("failback-pending-%d" % c["id"],
                                  "CT %d runs here but prefers %s, which is healthy again. "
                                  "Run `pve-watchdog failback %d` when convenient." % (c["id"], pref, c["id"]))
                continue
            cs = self.state.ct(c["id"])
            if cs.get("failback_tried_at") and t - cs["failback_tried_at"] < 3600:
                continue
            try:
                self.failback(c)
            except HostError as err:
                log.info("failback of CT %d not possible yet: %s", c["id"], err)

    def failback(self, c):
        """Move CT c from this node back to its preferred node (planned, ~minutes).

        Only one failback at a time per controller (watcher or CLI)."""
        run = self._pair_failback if self.cfg.container_mode(c) else self._failback
        if not self.failback_lock:
            return run(c)
        with open(self.failback_lock, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.info("a failback is already running; not starting another")
                return False
            return run(c)

    def _peer_took_it(self, vmid, pref, why):
        """After a lost reply: did the preferred node register (restore) the CT?

        True as soon as it shows up registered on `pref`; False only if it is
        still registered nowhere after `unclear_wait` seconds."""
        log.warning("failback %d: reply from %s lost (%s); watching for the result", vmid, pref, why)
        deadline = self.clock() + self.unclear_wait
        while True:
            try:
                e = self._ct_entry(self.host.status(), vmid) or {}
            except HostError:
                e = {}
            if pref in e.get("conf_on", []):
                self.notify.info("CT %d: reply from %s was lost, but it has the CT registered; "
                                 "not rolling back. Check that it is running." % (vmid, pref))
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(15)

    def _failback(self, c):
        vmid, pref = c["id"], c["preferred_node"]
        cs = self.state.ct(vmid)
        cs["failback_tried_at"] = self.clock()
        target = self.cfg.nodes[pref]
        if self.peers.status(target) is None:
            raise HostError("preferred node's controller is not reachable")
        self.notify.info("starting failback of CT %d to %s (short outage)" % (vmid, pref))
        try:
            self.peers.action(target, "free_orphans", vmid)   # tidy its old disk first
        except Exception as err:
            log.info("failback %d: peer could not clean old disks (%s); continuing", vmid, err)
        results, released = [], False
        try:
            results.append(self.host.stop(vmid))
            results.append(self.host.backup(vmid, "stop"))
            results.append(self.host.push(vmid, pref))
            results.append(self.host.release(vmid))
            released = True
            try:
                reply = self.peers.action(target, "restore_start", vmid)
            except Exception as lost:
                # The request may or may not have been carried out. Never roll
                # back on a guess: watch where the CT gets registered instead.
                if self._peer_took_it(vmid, pref, lost):
                    reply = {"ok": True, "result": {}}
                else:
                    raise HostError("%s did not restore CT %d within %d min after the reply "
                                    "was lost (%s)" % (pref, vmid, self.unclear_wait // 60, lost))
            if not reply.get("ok"):
                raise HostError("peer could not restore/start: %s" % reply.get("error"))
            results.extend((reply.get("result") or {}).values())
        except Exception as err:
            self.notify.alert("failback-failed-%d" % vmid,
                              "failback of CT %d failed: %s. Bringing it back up here." % (vmid, err))
            try:
                if released:
                    self.host.restore(vmid)
                self.host.start(vmid)
            except HostError as err2:
                self.notify.alert("failback-rollback-%d" % vmid,
                                  "CT %d is DOWN: rollback also failed: %s" % (vmid, err2))
            return False
        cs.pop("failback_tried_at", None)    # the 1-hour back-off is only for failures
        self.notify.resolve("failback-failed-%d" % vmid)
        self.notify.resolve("failback-pending-%d" % vmid)
        if _dry(*results):
            self.notify.info("DRY-RUN: would have failed back CT %d to %s" % (vmid, pref))
        else:
            self.notify.info("CT %d is back on its preferred node %s" % (vmid, pref))
        return True

    # ------------------------------------------------------------ wake-on-LAN
    def maybe_wake(self, st, maintenance):
        """A peer that is down may simply be powered off: send it a magic packet.

        Never during maintenance (a planned shutdown should stay off), and not
        when I can't reach the gateway myself (then I'm probably the cut-off one).
        Repeats every `wake_every` seconds, at most `wake_tries` times."""
        if not self.cfg.policy("wake_on_lan") or maintenance or not st.get("gateway_ok"):
            return
        t = self.clock()
        for p in self.cfg.peers():
            mac = p.get("wol_mac")
            ps = self.state.peer(p["name"])
            if not mac or not ps["down_since"]:
                continue
            if t - ps["down_since"] < self.cfg.policy("wake_after"):
                continue
            sent = ps.get("wake_sent", 0)
            if sent >= self.cfg.policy("wake_tries"):
                continue
            if sent and t - ps.get("wake_last", 0) < self.cfg.policy("wake_every"):
                continue
            ps["wake_sent"], ps["wake_last"] = sent + 1, t
            if self.cfg.dry_run:
                log.info("DRY-RUN would send Wake-on-LAN to %s (%s)", p["name"], mac)
                continue
            try:
                self.wake(mac, self.cfg.cluster("broadcast", "255.255.255.255"))
                self.notify.info("sent Wake-on-LAN to %s (%s), try %d of %d" % (
                    p["name"], mac, sent + 1, self.cfg.policy("wake_tries")))
            except (OSError, ValueError) as err:
                log.warning("could not send Wake-on-LAN to %s: %s", p["name"], err)

    # ------------------------------------------------------------- 6: housekeep
    def housekeeping(self, st):
        t = self.clock()
        q = st.get("quorum", {})
        everyone = all(n in q.get("member_nodes", []) for n in self.cfg.nodes)
        if everyone and q.get("total"):
            normal = (q.get("expected") == q["total"] and q.get("quorum") == q["total"] // 2 + 1)
            if not normal:
                self.notify.alert("votes", "all nodes are back but votes look wrong: expected=%s "
                                           "total=%s quorum=%s" % (q.get("expected"), q["total"],
                                                                   q.get("quorum")))
            else:
                self.notify.resolve("votes", "votes back to normal (total %s, quorum %s)"
                                    % (q["total"], q.get("quorum")))
                self.notify.resolve("force-quorum", "left force mode: all nodes are back in the "
                                                    "cluster with normal votes")
        for c in self.cfg.cts():
            e = self._ct_entry(st, c["id"])
            if e and len(e.get("conf_on", [])) > 1:
                self.notify.alert("conflict-%d" % c["id"],
                                  "CT %d is registered on MORE THAN ONE node (%s). The tool will "
                                  "not touch it. Manual check needed."
                                  % (c["id"], ", ".join(e["conf_on"])))
            elif e:
                self.notify.resolve("conflict-%d" % c["id"])
                # Covered CTs must not autostart by themselves (boot arbitration
                # decides). Only enforced once the CT is mode = "live".
                if e.get("local") and e.get("onboot") and c.get("mode") == "live":
                    try:
                        r = self.host.adopt(c["id"])
                        if not _dry(r):
                            self.notify.info("CT %d: set onboot=0; the tool now decides when it "
                                             "starts at boot" % c["id"])
                    except HostError as err:
                        log.warning("could not set onboot=0 on CT %d: %s", c["id"], err)
        # With every peer up, takeover-related warnings no longer apply.
        if not any(self.state.peer(p["name"])["down_since"] for p in self.cfg.peers()):
            for c in self.cfg.cts():
                for key in ("alive-%d", "stale-takeover-%d"):
                    self.notify.resolve(key % c["id"], "CT %d: earlier failover warning cleared "
                                                       "(all nodes are up)" % c["id"])
        # Hourly: remove leftover disks of CTs that now live on another node.
        if t - self.state.data.get("last_orphan_sweep", 0) >= 3600:
            self.state.data["last_orphan_sweep"] = t
            for c in self.cfg.cts():
                e = self._ct_entry(st, c["id"])
                if e and not e.get("local") and e.get("volumes") and len(e.get("conf_on", [])) == 1:
                    try:
                        self.host.free_orphans(c["id"])
                    except HostError as err:
                        log.info("orphan sweep CT %d skipped: %s", c["id"], err)

    # ======================================================= standby containers
    # For CTs with standby = "container" there are two real copies that never
    # move: the ORIGINAL (101) on its preferred node and the STANDBY (1101) on
    # the other node. roles.json (in /etc/pve) says which one is active.

    def _pair(self, c, st):
        """(entry, active_role, my_role, my_member) or None."""
        e = self._ct_entry(st, c["id"], pair=True)
        if not e:
            return None
        mine = self.cfg.my_role(c)
        if mine is None:
            return None
        return e, e.get("active", "original"), mine, e["members"][mine]

    def pair_tick(self, st, maintenance):
        for c in self.cfg.cts():
            p = self._pair(c, st)
            if not p:
                continue
            e, active, mine, me_ = p
            self._pair_isolation(c, e, active, mine, me_, maintenance)
            if not maintenance:
                self._pair_takeover(c, e, active, mine, me_, st)
                self._pair_failback_check(c, e, active, mine, me_)
            self._pair_housekeeping(c, e, active, mine, me_)

    def _pair_isolation(self, c, e, active, mine, me_, maintenance):
        cs = self.state.ct(c["id"])
        iso = self.state.data.get("isolated_since")
        limit = self.cfg.policy("isolation_stop_after")
        if iso and limit and not maintenance and self.clock() - iso >= limit:
            if me_.get("status") == "running" and not cs.get("demoted"):
                try:
                    self.host.stop(me_["id"])
                    cs["demoted"] = True
                    self.notify.alert("demoted-%d" % c["id"],
                                      "stopped CT %d because this node is isolated, so the other "
                                      "node can take over without two copies running" % me_["id"])
                except HostError as err:
                    self.notify.alert("demote-failed-%d" % c["id"],
                                      "could not stop CT %d while isolated: %s" % (me_["id"], err))
            return
        if not iso and cs.get("demoted") and me_.get("status") == "stopped":
            cs["demoted"] = False
            if active == mine and not self._liveness(c):
                try:
                    self.host.activate(c["id"], mine)
                    self.notify.resolve("demoted-%d" % c["id"],
                                        "network back: restarted CT %d here" % me_["id"])
                except HostError as err:
                    log.warning("could not restart demoted CT %d: %s", me_["id"], err)
            else:
                self.notify.resolve("demoted-%d" % c["id"],
                                    "network back: the other copy of CT %d is active; "
                                    "leaving CT %d stopped" % (c["id"], me_["id"]))

    def _pair_takeover(self, c, e, active, mine, me_, st):
        if active == mine:
            return
        home = e["members"][active]["home"]
        p = self.cfg.nodes.get(home)
        ps = self.state.peer(home) if p else None
        if not ps or not ps["down_since"]:
            return
        if self.clock() - ps["down_since"] < self.cfg.probe("failover_delay"):
            return
        if not st.get("gateway_ok"):
            self.notify.alert("no-gw-" + home, "%s looks down, but I cannot reach the gateway "
                                               "either, so I might be the isolated one. NOT taking "
                                               "over." % home)
            return
        cs = self.state.ct(c["id"])
        if cs.get("takeover_episode") == ps["down_since"]:
            return
        if not me_.get("local") or me_.get("status") != "stopped":
            self.notify.alert("nocopy-%d" % c["id"],
                              "cannot take over CT %d: my copy CT %d is not ready here"
                              % (c["id"], me_["id"]))
            return
        if self._peer_reachable_now(p):
            return
        if self._liveness(c):
            self.notify.alert("alive-%d" % c["id"],
                              "%s is unreachable but CT %d still answers on %s, so it is ALIVE. "
                              "Failover aborted; I will keep checking." % (home, c["id"],
                                                                           c.get("liveness_ip")))
            return
        ref = (me_.get("refreshed") or {}).get("archive_created")
        age_min = (self.clock() - ref) / 60 if ref else float("inf")
        if age_min > self.cfg.policy("stale_factor") * c.get("interval_min", 30):
            self.notify.info("⚠️ starting CT %d with a STALE copy (%s)" % (
                me_["id"], "%.0f min old" % age_min if ref else "age unknown"))
        try:
            results = []
            if not st.get("quorum", {}).get("quorate"):
                self.notify.alert("force-quorum",
                                  "⚠️ ENTERING FORCE MODE: running `pvecm expected 1` so "
                                  "this node can act alone while %s is down." % home)
                results.append(self.host.force_quorum())
            results.append(self.host.activate(c["id"], mine))
        except HostError as err:
            cs["attempts"] = cs.get("attempts", 0) + 1
            self.notify.alert("takeover-failed-%d" % c["id"],
                              "starting CT %d failed (attempt %d): %s" % (me_["id"],
                                                                         cs["attempts"], err))
            if cs["attempts"] >= 3:
                cs["takeover_episode"] = ps["down_since"]
            return
        cs["takeover_episode"] = ps["down_since"]
        cs["attempts"] = 0
        self.notify.resolve("takeover-failed-%d" % c["id"])
        self.notify.info("%s CT %d (%s) in place of CT %d on %s; copy was %s old" % (
            "DRY-RUN: would have started" if _dry(*results) else "started",
            me_["id"], c.get("name", ""), c["id"], home,
            "%.0f min" % age_min if ref else "?"))

    def _pair_failback_check(self, c, e, active, mine, me_):
        """I run the standby and the original's node is healthy again."""
        if active != mine or mine == "original" or me_.get("status") != "running":
            return
        policy = self.cfg.policy("failback")
        if policy == "never":
            return
        pref = c["preferred_node"]
        hs = self.state.peer(pref).get("healthy_since")
        if not hs or self.clock() - hs < self.cfg.policy("failback_stable_minutes") * 60:
            return
        if policy == "manual":
            self.notify.alert("failback-pending-%d" % c["id"],
                              "CT %d is running in place of CT %d, and %s is healthy again. "
                              "Run `pve-watchdog failback %d` when convenient."
                              % (me_["id"], c["id"], pref, c["id"]))
            return
        cs = self.state.ct(c["id"])
        if cs.get("failback_tried_at") and self.clock() - cs["failback_tried_at"] < 3600:
            return
        try:
            self.failback(c)
        except HostError as err:
            log.info("failback of CT %d not possible yet: %s", c["id"], err)

    def _pair_failback(self, c):
        """Stop the standby FIRST, copy its data back, THEN start the original."""
        base, pref = c["id"], c["preferred_node"]
        sb = self.cfg.member_id(c, "standby")
        cs = self.state.ct(base)
        cs["failback_tried_at"] = self.clock()
        target = self.cfg.nodes[pref]
        if self.peers.status(target) is None:
            raise HostError("preferred node's controller is not reachable")
        self.notify.info("starting failback: stopping CT %d, then starting CT %d on %s "
                         "(short outage)" % (sb, base, pref))
        results, stopped = [], False
        try:
            results.append(self.host.stop(sb))
            stopped = True
            results.append(self.host.backup(sb, "stop"))
            results.append(self.host.push(base, pref))
            try:
                reply = self.peers.action(target, "refresh_activate", base)
            except Exception as lost:
                if self._original_took_over(c, pref, lost):
                    reply = {"ok": True, "result": {}}
                else:
                    raise HostError("%s did not start CT %d within %d min after the reply was "
                                    "lost (%s)" % (pref, base, self.unclear_wait // 60, lost))
            if not reply.get("ok"):
                raise HostError("%s could not update/start CT %d: %s"
                                % (pref, base, reply.get("error")))
            results.extend((reply.get("result") or {}).values())
        except Exception as err:
            self.notify.alert("failback-failed-%d" % base,
                              "failback of CT %d failed: %s. Starting CT %d here again."
                              % (base, err, sb))
            if stopped:
                try:
                    self.host.activate(base, "standby")
                except HostError as err2:
                    self.notify.alert("failback-rollback-%d" % base,
                                      "CT %d is DOWN: restarting CT %d also failed: %s"
                                      % (base, sb, err2))
            return False
        cs.pop("failback_tried_at", None)    # the 1-hour back-off is only for failures
        self.notify.resolve("failback-failed-%d" % base)
        self.notify.resolve("failback-pending-%d" % base)
        self.notify.info("%sCT %d is back on %s; CT %d is the standby again" % (
            "DRY-RUN: " if _dry(*results) else "", base, pref, sb))
        return True

    def _original_took_over(self, c, pref, why):
        """After a lost reply: did the original become the active copy?"""
        log.warning("failback %d: reply from %s lost (%s); watching for the result",
                    c["id"], pref, why)
        deadline = self.clock() + self.unclear_wait
        while True:
            try:
                e = self._ct_entry(self.host.status(), c["id"], pair=True) or {}
            except HostError:
                e = {}
            if e.get("active") == "original":
                self.notify.info("CT %d: reply from %s was lost, but the original is now the "
                                 "active copy; not rolling back. Check it is running."
                                 % (c["id"], pref))
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(15)

    def _pair_housekeeping(self, c, e, active, mine, me_):
        if me_.get("local") and me_.get("onboot") and c.get("mode") == "live":
            try:
                self.host.adopt(me_["id"])
            except HostError as err:
                log.warning("could not set onboot=0 on CT %d: %s", me_["id"], err)
        # Both copies running = split brain. Alert loudly, touch nothing.
        other = "standby" if mine == "original" else "original"
        home = e["members"][other]["home"]
        peer = ((self.peer_status.get(home) or {}).get("host") or {})
        pe = (peer.get("cts") or {}).get(str(c["id"])) or {}
        other_running = ((pe.get("members") or {}).get(other) or {}).get("status") == "running"
        if me_.get("status") == "running" and other_running:
            self.notify.alert("conflict-%d" % c["id"],
                              "BOTH copies of CT %d are running (CT %d here and CT %d on %s). "
                              "The tool will not touch them. Stop one by hand."
                              % (c["id"], me_["id"], e["members"][other]["id"], home))
        else:
            self.notify.resolve("conflict-%d" % c["id"])
        if me_.get("status") == "running" and active != mine:
            self.notify.alert("wrong-copy-%d" % c["id"],
                              "CT %d is running here, but roles.json says the %s copy is the "
                              "active one. Check by hand." % (me_["id"], active))
        else:
            self.notify.resolve("wrong-copy-%d" % c["id"])

    def _pair_boot(self, c, e):
        mine = self.cfg.my_role(c)
        if mine is None:
            return
        me_ = e["members"][mine]
        active = e.get("active", "original")
        if not me_.get("local") or me_.get("status") != "stopped" or active != mine:
            log.info("boot: CT %d (%s copy of %d) is %s; active copy is %s; nothing to do",
                     me_["id"], mine, c["id"], me_.get("status") or "not here", active)
            return
        if self._liveness(c):
            self.notify.alert("boot-alive-%d" % c["id"],
                              "after boot, CT %d already answers on %s elsewhere; NOT starting "
                              "CT %d" % (c["id"], c.get("liveness_ip"), me_["id"]))
            return
        try:
            r = self.host.activate(c["id"], mine)
            self.notify.info("%sstarted CT %d after boot" % (
                "DRY-RUN: would have " if _dry(r) else "", me_["id"]))
        except HostError as err:
            self.notify.alert("boot-start-%d" % c["id"],
                              "could not start CT %d after boot: %s" % (me_["id"], err))

    # --------------------------------------------------------------- the tick
    def tick(self):
        try:
            st = self.observe()
        except HostError as err:
            self.notify.alert("host-link", "controller cannot talk to its own host: %s" % err)
            return None
        self.notify.resolve("host-link", "controller can talk to its host again")
        maintenance = self._maintenance(st)
        self.check_isolation(st, maintenance)
        self.maybe_wake(st, maintenance)
        self.maybe_takeover(st, maintenance)
        self.maybe_failback(st, maintenance)
        self.pair_tick(st, maintenance)
        self.housekeeping(st)
        self.state.save()
        return st

    # -------------------------------------------------------- boot arbitration
    def boot_arbitration(self, sleep=time.sleep):
        """Right after a host boot: decide which registered CTs to start.

        Covered CTs have onboot=0, so Proxmox never starts them by itself.
        We start one only if it is registered HERE and nothing else answers
        on its IP (i.e. no other copy is running)."""
        st = self.host.status()
        up = st.get("uptime")
        if up is None or up > 900:
            log.info("host has been up %ss; not a fresh boot, skipping boot arbitration", up)
            return
        deadline = self.clock() + self.cfg.policy("boot_wait")
        while self.clock() < deadline:
            if any(self.peers.status(p) is not None for p in self.cfg.peers()):
                break
            sleep(5)
        log.info("boot arbitration: host up %.0fs, deciding which CTs to start", up)
        st = self.host.status()
        for c in self.cfg.cts():
            if not c.get("start_at_boot", True):
                continue
            pe = self._ct_entry(st, c["id"], pair=True)
            if pe:
                self._pair_boot(c, pe)
                continue
            e = self._ct_entry(st, c["id"])
            if not e:
                continue
            if len(e.get("conf_on", [])) > 1:
                continue                            # conflict: housekeeping alerts
            if not e.get("local") or e.get("status") != "stopped":
                log.info("boot: CT %d is %s; nothing to do", c["id"],
                         "running here" if e.get("local") else
                         "registered on %s" % ",".join(e.get("conf_on", [])) or "nowhere")
                continue
            if self._liveness(c):
                self.notify.alert("boot-alive-%d" % c["id"],
                                  "after boot, CT %d already answers on %s elsewhere; NOT "
                                  "starting my copy" % (c["id"], c.get("liveness_ip")))
                continue
            try:
                r = self.host.start(c["id"])
                self.notify.info("%sstarted CT %d after boot" % ("DRY-RUN: would have " if _dry(r) else "", c["id"]))
            except HostError as err:
                self.notify.alert("boot-start-%d" % c["id"],
                                  "could not start CT %d after boot: %s" % (c["id"], err))


# ------------------------------------------------------------------ replication
def _pair_sync(cfg, host, peers, notify, state, c, e, force, t):
    """Standby container: back up the ACTIVE copy, send it, refresh the other copy."""
    base = c["id"]
    mine = cfg.my_role(c)
    if mine is None or e.get("active", "original") != mine:
        return
    me_ = e["members"][mine]
    if not me_.get("local") or me_.get("status") != "running":
        return
    other = "standby" if mine == "original" else "original"
    home = e["members"][other]["home"]
    node = cfg.nodes[home]
    arc = e.get("archive")
    fresh_mine = arc and arc.get("source", base) == me_["id"]
    need_backup = (force or not fresh_mine or
                   t - arc["created"] >= c.get("interval_min", 30) * 60)
    refreshed = state.ct(base).setdefault("refreshed_on", {})
    missed = bool(arc) and refreshed.get(home) != arc["created"]
    if need_backup or missed:
        try:
            r = host.backup(me_["id"], "snapshot") if need_backup else {}
            cur = r.get("archive") or arc
            ps = peers.status(node)
            if ps is None:
                log.info("sync CT %d: %s controller unreachable; will retry", base, home)
                return
            pool = (ps.get("host") or {}).get("pool") or {}
            need = (cur or {}).get("used_bytes") or 0
            if pool.get("size") and (pool["used"] + need) * 100.0 / pool["size"] > \
                    cfg.cluster("pool_max_used_pct", 80):
                notify.alert("pool-%s-%d" % (home, base),
                             "%s thin pool too full to refresh the copy of CT %d. Not sending "
                             "it." % (home, base))
                return
            notify.resolve("pool-%s-%d" % (home, base), "%s has room again" % home)
            pr = host.push(base, home)
            reply = peers.action(node, "refresh", base, timeout=6 * 3600)
            if not reply.get("ok"):
                raise HostError("%s could not refresh its copy: %s" % (home, reply.get("error")))
            if cur and not _dry(r, pr, *((reply.get("result") or {}).values())):
                refreshed[home] = cur["created"]
            notify.resolve("sync-failed-%d" % base, "replication of CT %d works again" % base)
            log.info("sync CT %d -> %s done%s", base, home, " (dry-run)" if _dry(r, pr) else "")
        except Exception as err:
            notify.alert("sync-failed-%d" % base, "replication of CT %d failed: %s" % (base, err))
        return
    if cfg.dry_run or c.get("mode") != "live":
        return
    ps = peers.status(node)
    if ps is None:
        return
    pe = ((ps.get("host") or {}).get("cts") or {}).get(str(base)) or {}
    ref = (((pe.get("members") or {}).get(other) or {}).get("refreshed") or {}).get("archive_created")
    max_age = cfg.policy("stale_factor") * c.get("interval_min", 30) * 60
    key = "stale-%s-%d" % (home, base)
    if not ref or t - ref > max_age:
        notify.alert(key, "the copy of CT %d on %s is stale or missing" % (base, home))
    else:
        notify.resolve(key, "the copy of CT %d on %s is fresh again" % (base, home))


def sync(cfg, host, peers, notify, state, only=None, force=False, clock=time.time):
    """Copy each CT that runs on this node to the other node(s) when it is due."""
    st = host.status()
    t = clock()
    limit = cfg.cluster("pool_max_used_pct", 80)
    for c in cfg.cts():
        vmid = c["id"]
        if only is not None and vmid != int(only):
            continue
        e = (st.get("cts") or {}).get(str(vmid)) or {}
        if e.get("standby") == "container":
            _pair_sync(cfg, host, peers, notify, state, c, e, force, t)
            continue
        if not e.get("local") or e.get("status") != "running" or len(e.get("conf_on", [])) != 1:
            continue
        arc = e.get("archive")
        need_backup = force or not arc or t - arc["created"] >= c.get("interval_min", 30) * 60
        # Which backup has actually reached each peer? If a peer missed the
        # latest one (e.g. it was unreachable), resend it as soon as possible.
        delivered = state.ct(vmid).setdefault("delivered", {})
        missed = bool(arc) and any(delivered.get(p["name"]) != arc["created"] for p in cfg.peers())
        due = need_backup or missed
        if due:
            try:
                r = host.backup(vmid, "snapshot") if need_backup else {}
                cur = r.get("archive") or arc
                need = (cur or {}).get("used_bytes") or 0
                for p in cfg.peers():
                    if cur and delivered.get(p["name"]) == cur["created"]:
                        continue                        # this peer already has it
                    ps = peers.status(p)
                    if ps is None:
                        log.info("sync CT %d: %s controller unreachable, skipping push", vmid, p["name"])
                        continue
                    pool = (ps.get("host") or {}).get("pool") or {}
                    if pool.get("size"):
                        after = (pool["used"] + need) * 100.0 / pool["size"]
                        if after > limit:
                            notify.alert("pool-%s-%d" % (p["name"], vmid),
                                         "%s thin pool would be %.0f%% full after restoring CT %d "
                                         "(limit %s%%). Not sending the copy." % (p["name"], after, vmid, limit))
                            continue
                        notify.resolve("pool-%s-%d" % (p["name"], vmid))
                    pr = host.push(vmid, p["name"])
                    if cur and not _dry(pr):
                        delivered[p["name"]] = cur["created"]
                notify.resolve("sync-failed-%d" % vmid, "replication of CT %d works again" % vmid)
                log.info("sync CT %d done%s", vmid, " (dry-run)" if _dry(r) else "")
            except HostError as err:
                notify.alert("sync-failed-%d" % vmid, "replication of CT %d failed: %s" % (vmid, err))
        # Staleness of the copy on each peer (live mode only; dry-run never copies).
        # Skipped in the pass that just copied: the peer's status may predate it.
        if due or cfg.dry_run or c.get("mode") != "live":
            continue
        for p in cfg.peers():
            ps = peers.status(p)
            if ps is None:
                continue
            parc = ((ps.get("host") or {}).get("cts") or {}).get(str(vmid), {}).get("archive")
            max_age = cfg.policy("stale_factor") * c.get("interval_min", 30) * 60
            if not parc or t - parc["created"] > max_age:
                notify.alert("stale-%s-%d" % (p["name"], vmid),
                             "copy of CT %d on %s is stale or missing" % (vmid, p["name"]))
            else:
                notify.resolve("stale-%s-%d" % (p["name"], vmid),
                               "copy of CT %d on %s is fresh again" % (vmid, p["name"]))
    state.save()
