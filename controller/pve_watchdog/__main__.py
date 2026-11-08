"""pve-watchdog command line (run inside the controller container).

  pve-watchdog status                   what every node and covered CT looks like
  pve-watchdog plan                     DRY-RUN rehearsal: what would happen if the
                                   peer died right now (never changes anything)
  pve-watchdog maintenance on [2h|30m]  suppress failover (expires by itself)
  pve-watchdog maintenance off
  pve-watchdog sync [--now] [ID]        copy due CTs to the peer (the timer runs this)
  pve-watchdog failback ID              move CT back to its preferred node now
  pve-watchdog test-notify              send a test message to Discord
  pve-watchdog wake NODE                send a Wake-on-LAN magic packet to NODE
  pve-watchdog daemon                   the watcher (systemd runs this)
"""
import fcntl
import logging
import os
import re
import sys
import threading
import time
import tomllib

from . import __version__, probes
from .config import Config
from .engine import Engine, sync
from .hostlink import Host, HostError
from .notify import Notifier
from .peerapi import PeerClient, PeerServer
from .state import State

LOCAL = "/etc/pve-watchdog/local.toml"        # which node am I, and my host's IP
VAR = "/var/lib/pve-watchdog"
LOCK = "/run/pve-watchdog.lock"
FAILBACK_LOCK = "/run/pve-watchdog-failback.lock"

log = logging.getLogger("pve-watchdog")


def setup_logging(verbose=False):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)


def load_all(force_dry=False, state_file="state.json"):
    """Each kind of process keeps its own memory file, so the watcher (which
    saves every 10 s) never overwrites what the sync job remembers:
    state.json = watcher, sync-state.json = sync timer, cli-state.json = CLI."""
    with open(LOCAL, "rb") as f:
        local = tomllib.load(f)
    host = Host(local["host_ip"], force_dry=force_dry)
    got = host.get_config()
    if got["node"] != local["node"]:
        raise SystemExit("this controller is set up for %s but its host says it is %s"
                         % (local["node"], got["node"]))
    cfg = Config(got["config"], got["secret"], me=local["node"])
    os.makedirs(VAR, exist_ok=True)
    state = State(VAR + "/" + state_file)
    notifier = Notifier(cfg.discord_webhook, cfg.me, VAR + "/notify-queue.jsonl",
                        state.data, dry_run_prefix=cfg.dry_run or force_dry)
    peers = PeerClient(cfg.hmac_key, cfg.api_port, cfg.me)
    return cfg, host, peers, notifier, state


class BrainLock:
    """One changing sequence at a time inside this controller."""

    def __init__(self, blocking=True):
        self.blocking = blocking

    def __enter__(self):
        self.f = open(LOCK, "w")
        flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
        fcntl.flock(self.f, flags)
        return self

    def __exit__(self, *a):
        self.f.close()


def parse_duration(s):
    m = re.fullmatch(r"(\d+)\s*([hm]?)", s or "1h")
    if not m:
        raise SystemExit("duration like 2h or 30m")
    n = int(m.group(1))
    return n * 3600 if m.group(2) in ("h", "") else n * 60


# ------------------------------------------------------------------ commands
def cmd_daemon():
    # Right after a host boot the network may not be up yet: wait for the host.
    for attempt in range(60):
        try:
            cfg, host, peers, notifier, state = load_all()
            break
        except HostError as e:
            if attempt == 59:
                raise
            log.info("host not reachable yet (%s); retrying in 5 s", e)
            time.sleep(5)
    engine = Engine(cfg, host, peers, probes, notifier, state, failback_lock=FAILBACK_LOCK)
    busy = threading.Lock()

    def status_fn():
        return {"node": cfg.me, "time": time.time(), "version": __version__,
                "dry_run": cfg.dry_run, "host": engine.local}

    def action_fn(from_node, verb, vmid):
        if from_node not in cfg.nodes or from_node == cfg.me:
            raise ValueError("unknown requesting node")
        if verb == "reload":                       # another node saved the shared config
            reload_now.set()
            return {"reload": True}
        c = cfg.ct(vmid)
        if not c:
            raise ValueError("CT %s is not covered" % vmid)
        with busy:
            if verb == "restore_start":
                r1 = host.restore(vmid)
                r2 = host.start(vmid)
                notifier.info("restored and started CT %d at the request of %s" % (vmid, from_node))
                return {"restore": r1, "start": r2}
            if verb == "free_orphans":
                return {"free_orphans": host.free_orphans(vmid)}
            # Standby containers: my copy of the pair is the one that lives here.
            mine = cfg.my_role(c)
            if verb in ("refresh", "refresh_activate"):
                if not cfg.container_mode(c) or mine is None:
                    raise ValueError("CT %s has no standby copy on this node" % vmid)
                out = {"refresh": host.refresh(cfg.member_id(c, mine))}
                if verb == "refresh_activate":
                    out["activate"] = host.activate(vmid, mine)
                    notifier.info("updated and started CT %d at the request of %s"
                                  % (cfg.member_id(c, mine), from_node))
                return out
        raise ValueError("unknown verb")

    if cfg.hmac_key:
        PeerServer(cfg.me, cfg.nodes[cfg.me]["controller_ip"], cfg.api_port, cfg.hmac_key,
                   status_fn, action_fn).start()
    else:
        notifier.alert("no-key", "no hmac_key in pve-watchdog.secret.toml: controllers cannot talk")
    reload_now = threading.Event()
    start_web_ui(engine, host, notifier, reload_now)
    notifier.info("controller started (v%s, %s)" % (__version__, "DRY-RUN" if cfg.dry_run else "LIVE"))
    with busy:
        engine.boot_arbitration()
    last_reload = time.time()
    while True:
        t0 = time.time()
        try:
            with busy:
                engine.tick()
            notifier.flush()
        except Exception:                      # never let one bad tick kill the watcher
            log.exception("tick failed")
        if time.time() - last_reload > 60 or reload_now.is_set():   # pick up config edits
            try:
                fresh = load_all()[0]
                engine.cfg = fresh
                cfg = fresh
                notifier.webhook = fresh.discord_webhook     # pick up a new webhook URL
                last_reload = time.time()
                if reload_now.is_set():
                    log.info("config re-read on request (saved in the web UI)")
                reload_now.clear()
            except Exception as e:
                log.warning("config reload failed, keeping the old one: %s", e)
        time.sleep(max(1, cfg.probe("interval") - (time.time() - t0)))


WEB_CERT = "/etc/pve-watchdog/webui.crt"
WEB_KEY = "/etc/pve-watchdog/webui.key"
WEB_PORT = 8443


def start_web_ui(engine, host, notifier, reload_now):
    """Serve the web UI on this controller's IP, port 8443 (if a certificate exists)."""
    from .webui import WebApp, WebServer

    class Ctx:
        def cfg(self):
            return engine.cfg

        def reload(self):
            # The config lives in /etc/pve, so every node already has the new
            # file; tell the other watchdogs to re-read it now, not within a minute.
            reload_now.set()
            cfg = engine.cfg

            def tell_peers():
                for p in cfg.peers():
                    try:
                        engine.peers.action(p, "reload", 0, timeout=5)
                    except Exception as e:
                        log.info("could not ask %s to reload: %s", p["name"], e)
            threading.Thread(target=tell_peers, daemon=True).start()

        def sync_alerts(self):
            return State(VAR + "/sync-state.json").data.get("alerts", {})

    ctx = Ctx()
    ctx.host, ctx.engine, ctx.notifier = host, engine, notifier
    if not (os.path.exists(WEB_CERT) and os.path.exists(WEB_KEY)):
        log.warning("web UI not started: no certificate at %s (re-run install.sh)", WEB_CERT)
        return None
    try:
        srv = WebServer(WebApp(ctx), engine.cfg.nodes[engine.cfg.me]["controller_ip"], WEB_PORT,
                        WEB_CERT, WEB_KEY)
        srv.start()
        log.info("web UI on https://%s:%d", engine.cfg.nodes[engine.cfg.me]["controller_ip"], WEB_PORT)
        return srv
    except OSError as e:
        log.warning("web UI could not start: %s", e)
        return None


def cmd_sync(args):
    force = "--now" in args
    rest = [a for a in args if a != "--now"]
    cfg, host, peers, notifier, state = load_all(state_file="sync-state.json")
    try:
        with BrainLock(blocking=False):
            sync(cfg, host, peers, notifier, state, only=rest[0] if rest else None, force=force)
    except BlockingIOError:
        print("another pve-watchdog action is running; skipping this sync")
    notifier.flush()


def cmd_status():
    cfg, host, peers, notifier, state = load_all()
    st = host.status()
    q = st["quorum"]
    print("node %s   mode: %s   maintenance: %s" % (
        cfg.me, "DRY-RUN" if cfg.dry_run else "LIVE",
        "ON" if st["maintenance"].get("active") else "off"))
    print("cluster: quorate=%s expected=%s total=%s quorum=%s members=%s" % (
        q["quorate"], q["expected"], q["total"], q["quorum"], ",".join(q["member_nodes"])))
    print("gateway reachable: %s   thin pool: %.1f%% used" % (st["gateway_ok"], st["pool"]["used_pct"]))
    for p in cfg.peers():
        ps = peers.status(p)
        print("peer %-12s controller: %s" % (p["name"], "reachable" if ps else "NOT reachable"))
    print()
    print("%-6s %-12s %-8s %-14s %-9s %s" % ("CT", "name", "mode", "registered on", "status", "my copy"))
    for c in cfg.cts():
        e = st["cts"].get(str(c["id"]), {})
        if e.get("standby") == "container":
            mine = cfg.my_role(c)
            m = (e.get("members") or {}).get(mine) or {}
            ref = (m.get("refreshed") or {}).get("archive_created")
            age = ("copy %.0f min old" % ((time.time() - ref) / 60)) if ref else "copy: -"
            act = e.get("active", "original")
            act_id = cfg.member_id(c, act)
            print("%-6s %-12s %-8s active: %s (CT %d on %s) | here: CT %s %s, %s" % (
                c["id"], c.get("name", ""), c["mode"], act, act_id,
                e["members"][act]["home"], m.get("id"), m.get("status") or "missing", age))
            continue
        arc = e.get("archive")
        age = "%.0f min old" % ((time.time() - arc["created"]) / 60) if arc else "none"
        print("%-6s %-12s %-8s %-14s %-9s %s" % (c["id"], c.get("name", ""), c["mode"],
                                                 ",".join(e.get("conf_on", [])) or "-",
                                                 e.get("status") or "-", age))
    alerts = dict(state.data.get("alerts", {}))
    alerts.update(State(VAR + "/sync-state.json").data.get("alerts", {}))
    if alerts:
        print("\nopen alerts:")
        for k, a in alerts.items():
            print("  - %s" % a["text"])


def cmd_plan():
    """Rehearse a takeover of every peer's CTs. Always dry: the host is told
    --dry-run, so even a LIVE system changes nothing."""
    cfg, host, peers, notifier, state = load_all(force_dry=True)
    st = host.status()
    print("=== DRY-RUN rehearsal on %s (nothing will be changed) ===" % cfg.me)
    for p in cfg.peers():
        for c in cfg.cts():
            e = st["cts"].get(str(c["id"]), {})
            if e.get("standby") == "container":
                mine, act = cfg.my_role(c), e.get("active", "original")
                if mine is None or act == mine or e["members"][act]["home"] != p["name"]:
                    continue
                print("\n--- if %s died: start CT %d in place of CT %d (%s) ---" % (
                    p["name"], cfg.member_id(c, mine), c["id"], c.get("name", "")))
                steps = [("force-quorum (only if not quorate)", host.force_quorum),
                         ("activate", lambda c=c, mine=mine: host.activate(c["id"], mine))]
                _print_steps(steps)
                continue
            if e.get("conf_on") != [p["name"]]:
                continue
            print("\n--- if %s died: take over CT %d (%s) ---" % (p["name"], c["id"], c.get("name", "")))
            steps = []
            if not st["quorum"]["quorate"] or len(cfg.nodes) == 2:
                steps.append(("force-quorum (only if not quorate)", host.force_quorum))
            steps += [("claim", lambda c=c, p=p: host.claim(c["id"], p["name"])),
                      ("restore", lambda c=c: host.restore(c["id"])),
                      ("start", lambda c=c: host.start(c["id"]))]
            _print_steps(steps)
    print("\n(Checks marked NO are expected while the peer is alive: they are exactly the "
          "guards that stop a takeover from happening now.)")


def _print_steps(steps):
    for label, fn in steps:
        try:
            r = fn()
        except HostError as err:
            print("  %-36s ERROR: %s" % (label, err))
            continue
        print("  %s" % label)
        for w in r.get("would_run", []):
            print("      would run: %s" % w)
        for chk in r.get("checks", []):
            print("      [%s] %s" % ("ok " if chk["ok"] else "NO ", chk["check"]))
        if r.get("note"):
            print("      note: %s" % r["note"])


def cmd_maintenance(args):
    cfg, host, peers, notifier, state = load_all()
    if not args or args[0] not in ("on", "off"):
        raise SystemExit("pve-watchdog maintenance on [2h|30m] | off")
    if args[0] == "on":
        secs = parse_duration(args[1] if len(args) > 1 else "1h")
        r = host.maintenance("on", secs)
        print("maintenance ON until %s (cluster-wide)" % time.strftime("%Y-%m-%d %H:%M", time.localtime(r["until"])))
    else:
        host.maintenance("off")
        print("maintenance off")


def cmd_failback(args):
    cfg, host, peers, notifier, state = load_all(state_file="cli-state.json")
    c = cfg.ct(args[0]) if args else None
    if not c:
        raise SystemExit("usage: pve-watchdog failback <covered CT id>")
    engine = Engine(cfg, host, peers, probes, notifier, state, failback_lock=FAILBACK_LOCK)
    st = engine.observe()
    e = st["cts"].get(str(c["id"]), {})
    if e.get("standby") == "container":
        mine = cfg.my_role(c)
        me_ = (e.get("members") or {}).get(mine) or {}
        if mine != "standby" or e.get("active") != "standby" or me_.get("status") != "running":
            raise SystemExit("nothing to fail back: run this on the node where the standby "
                             "(CT %d) is running" % cfg.member_id(c, "standby"))
    elif not e.get("local"):
        raise SystemExit("CT %d is not registered on this node; run this on the node that has it" % c["id"])
    with BrainLock():
        ok = engine.failback(c)
    state.save()
    notifier.flush()
    print("failback %s" % ("done" if ok else "FAILED (see alerts)"))


def cmd_wake(args):
    """Send a Wake-on-LAN magic packet to another node: pve-watchdog wake pve2"""
    from . import wol
    cfg, host, peers, notifier, state = load_all(state_file="cli-state.json")
    if not args or args[0] not in cfg.nodes:
        raise SystemExit("usage: pve-watchdog wake <%s>" % "|".join(cfg.nodes))
    mac = cfg.nodes[args[0]].get("wol_mac")
    if not mac:
        raise SystemExit("no wol_mac for %s in the config" % args[0])
    wol.send(mac, cfg.cluster("broadcast", "255.255.255.255"))
    print("magic packet sent to %s (%s)" % (args[0], mac))


def cmd_test_notify(args):
    cfg, host, peers, notifier, state = load_all()
    notifier.info(" ".join(args) if args else "test message from the pve-watchdog controller")
    notifier.flush()
    print("queued; sent if the webhook is configured and reachable")


def main(argv):
    setup_logging("-v" in argv)
    argv = [a for a in argv if a != "-v"]
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, args = argv[1], argv[2:]
    table = {"daemon": lambda: cmd_daemon(), "sync": lambda: cmd_sync(args),
             "status": lambda: cmd_status(), "plan": lambda: cmd_plan(),
             "maintenance": lambda: cmd_maintenance(args),
             "failback": lambda: cmd_failback(args), "test-notify": lambda: cmd_test_notify(args),
             "wake": lambda: cmd_wake(args)}
    if cmd not in table:
        print(__doc__)
        return 2
    table[cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
