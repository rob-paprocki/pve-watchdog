// pve-watchdog web UI. Plain JavaScript; every value is inserted as text
// (never as HTML), so nothing shown on the page can become code.
"use strict";

let CSRF = "";
let TAB = "dashboard";
let timer = null;

// ---------- small helpers ----------
// Replace the page body; empty slots (null/undefined/false) are skipped, as in h().
function setView(...kids) {
  $("#view").replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
}

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (sel) => document.querySelector(sel);
const pill = (text, kind) => h("span", { class: "pill " + (kind || "") }, text);
const ago = (t) => {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return Math.round(s) + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  return (s / 3600).toFixed(1) + " h ago";
};

function banner(text, kind) {
  const b = $("#banner");
  b.textContent = text;
  b.className = kind || "";
  b.hidden = !text;
  if (text && kind === "ok") setTimeout(() => { if (b.textContent === text) b.hidden = true; }, 4000);
}

async function api(method, path, body) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  if (method !== "GET") opt.headers["X-CSRF-Token"] = CSRF;
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("not logged in"); }
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

async function act(label, fn) {
  try {
    await fn();
    banner(label + ": done", "ok");
  } catch (e) {
    banner(label + ": " + e.message, "bad");
  }
}

// ---------- light / dark toggle ----------
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  $("#theme").textContent = t[0].toUpperCase() + t.slice(1);
}
function initTheme() {
  let t = "auto";
  try { t = localStorage.getItem("pvewd-theme") || "auto"; } catch (e) { /* storage blocked */ }
  if (!THEMES.includes(t)) t = "auto";
  applyTheme(t);
  $("#theme").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "auto";
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    applyTheme(next);
    try { localStorage.setItem("pvewd-theme", next); } catch (e) { /* ignore */ }
  });
}

// ---------- login ----------
function showLogin() {
  clearInterval(timer);
  $("#login").hidden = false;
  setView();
  $("#tabs").hidden = true;
  $("#logout").hidden = true;
  $("#user").textContent = "";
}

async function afterLogin(user, csrf) {
  CSRF = csrf;
  $("#login").hidden = true;
  $("#tabs").hidden = false;
  $("#logout").hidden = false;
  $("#user").textContent = user;
  show(TAB);
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  $("#login-error").hidden = true;
  try {
    const r = await api("POST", "/api/login", { username: f.username.value, password: f.password.value });
    f.password.value = "";
    afterLogin(r.user, r.csrf);
  } catch (e) {
    $("#login-error").textContent = e.message;
    $("#login-error").hidden = false;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("POST", "/api/logout"); } catch (e) { /* ignore */ }
  showLogin();
});

// ---------- tabs ----------
for (const b of document.querySelectorAll("#tabs button")) {
  b.addEventListener("click", () => show(b.dataset.tab));
}

function show(tab) {
  TAB = tab;
  for (const b of document.querySelectorAll("#tabs button")) b.classList.toggle("active", b.dataset.tab === tab);
  clearInterval(timer);
  const views = { dashboard, guests, nodes, settings, history };
  views[tab]().catch((e) => banner(e.message, "bad"));
  if (tab === "dashboard") timer = setInterval(() => dashboard().catch(() => {}), 10000);
}

// ---------- dashboard ----------
function ctState(c, me) {
  const here = c.here || {};
  if (c.standby === "container") {
    const active = here.active || "original";
    const id = active === "original" ? c.id : c.standby_id;
    const where = active === "original" ? c.preferred_node : c.standby_node;
    const mine = here.members ? Object.values(here.members).find((m) => m.home === me) : null;
    let age = null;
    for (const p of Object.values(c.peers || {})) {
      const ms = (p && p.members) || {};
      for (const m of Object.values(ms)) if (m.refreshed) age = m.refreshed.archive_created;
    }
    if (mine && mine.refreshed) age = mine.refreshed.archive_created;
    return { running: "CT " + id + " on " + where, kind: active === "original" ? "ok" : "warn",
             label: active === "original" ? "original active" : "STANDBY active", copy: age };
  }
  const on = (here.conf_on || []).join(", ") || "?";
  return { running: "CT " + c.id + " on " + on, kind: on === c.preferred_node ? "ok" : "warn",
           label: "file standby", copy: here.archive ? here.archive.created : null };
}

async function dashboard() {
  const o = await api("GET", "/api/overview");
  $("#node").textContent = "· " + o.me;
  $("#footer").textContent = "pve-watchdog " + o.version + " · served by " + o.me;
  const q = o.quorum || {};
  const m = o.maintenance || {};
  const nodeCards = o.nodes.map((n) => {
    let state = pill("this node", "ok");
    if (!n.me) {
      const up = n.signals && Object.values(n.signals).some(Boolean);
      state = n.down_since ? pill("DOWN since " + ago(n.down_since), "bad")
        : up ? pill("up", "ok") : pill("checking…", "warn");
    }
    return h("div", { class: "card" },
      h("h3", {}, n.name, " ", state),
      h("div", { class: "small muted" }, "host ", n.host_ip, " · watchdog ", n.controller_ip,
        n.wol_mac ? " · WoL " + n.wol_mac : ""),
      n.me ? null : h("div", { class: "small muted" },
        Object.entries(n.signals || {}).map(([k, v]) => k + (v ? " ✓ " : " ✗ "))));
  });

  const rows = o.cts.map((c) => {
    const s = ctState(c, o.me);
    return h("tr", {},
      h("td", {}, h("b", {}, c.id), " ", c.name || ""),
      h("td", {}, pill(c.mode, c.mode === "live" ? "ok" : c.mode === "dry-run" ? "warn" : "")),
      h("td", {}, pill(s.label, s.kind), h("div", { class: "small muted" }, s.running)),
      h("td", { class: "small" }, s.copy ? ago(s.copy) : "—"),
      h("td", {},
        h("button", { onclick: () => act("Sync " + c.id, () => api("POST", "/api/action", { action: "sync", id: c.id })) }, "Sync now"),
        " ",
        h("button", { onclick: () => act("Failback " + c.id, () => api("POST", "/api/action", { action: "failback", id: c.id })) }, "Failback")));
  });

  const alerts = o.alerts.length
    ? h("ul", {}, o.alerts.map((a) => h("li", {}, a.text, " ", h("span", { class: "muted small" }, ago(a.since)))))
    : h("p", { class: "muted" }, "No open alerts.");

  setView(
    o.dry_run ? h("div", { class: "card" }, pill("DRY-RUN", "warn"), " Nothing is changed anywhere until dry-run is switched off in Settings.") : null,
    h("div", { class: "grid" },
      nodeCards,
      h("div", { class: "card" },
        h("h3", {}, "Cluster"),
        h("div", {}, "Quorate: ", q.quorate ? pill("yes", "ok") : pill("NO", "bad")),
        h("div", { class: "small muted" }, "votes ", q.total, " of ", q.expected, " expected, quorum ", q.quorum),
        h("div", { class: "small muted" }, "gateway ", o.gateway_ok ? "reachable" : "NOT reachable",
          o.pool ? " · thin pool " + o.pool.used_pct.toFixed(1) + "% used" : "")),
      h("div", { class: "card" },
        h("h3", {}, "Maintenance ", m.active ? pill("ON", "warn") : pill("off")),
        m.active ? h("div", { class: "small muted" }, "until ", new Date(m.until * 1000).toLocaleString()) :
          h("div", { class: "small muted" }, "Failover is armed."),
        h("div", { class: "row" },
          h("button", { onclick: () => act("Maintenance on", () => api("POST", "/api/action", { action: "maintenance_on", minutes: 60 }).then(dashboard)) }, "On 1 h"),
          h("button", { onclick: () => act("Maintenance on", () => api("POST", "/api/action", { action: "maintenance_on", minutes: 240 }).then(dashboard)) }, "On 4 h"),
          h("button", { onclick: () => act("Maintenance off", () => api("POST", "/api/action", { action: "maintenance_off" }).then(dashboard)) }, "Off")))),
    h("div", { class: "card" }, h("h2", {}, "Protected guests"),
      o.cts.length ? h("table", {}, h("tr", {}, h("th", {}, "Guest"), h("th", {}, "Mode"), h("th", {}, "Running"),
        h("th", {}, "Latest copy"), h("th", {}, "")), rows) : h("p", { class: "muted" }, "Nothing is covered yet. Add guests on the Guests tab.")),
    h("div", { class: "card" }, h("h2", {}, "Open alerts"), alerts));
}

// ---------- guests ----------
async function guests() {
  const [list, conf, o] = await Promise.all([api("GET", "/api/guests"), api("GET", "/api/config"), api("GET", "/api/overview")]);
  const covered = {};
  for (const c of conf.config.ct || []) covered[c.id] = c;
  const nodeNames = o.nodes.map((n) => n.name);

  function form(g, c) {
    const box = h("div", { class: "inline-form" });
    const f = {
      preferred_node: h("select", {}, nodeNames.map((n) => h("option", { value: n }, n))),
      standby: h("select", {}, h("option", { value: "container" }, "duplicate container (+offset)"), h("option", { value: "file" }, "backup file (same ID)")),
      mode: h("select", {}, ["dry-run", "live", "off"].map((m) => h("option", { value: m }, m))),
      interval_min: h("input", { type: "number", min: 5, max: 1440 }),
      priority: h("input", { type: "number", min: 1, max: 99 }),
      liveness_ip: h("input", { placeholder: "the guest's own IP" }),
    };
    f.preferred_node.value = (c && c.preferred_node) || g.node;
    // An existing entry without a standby key is a file standby (the default);
    // only NEW protection defaults to a duplicate container.
    f.standby.value = c ? (c.standby || "file") : "container";
    f.mode.value = (c && c.mode) || "dry-run";
    f.interval_min.value = (c && c.interval_min) || 30;
    f.priority.value = (c && c.priority) || 5;
    f.liveness_ip.value = (c && c.liveness_ip) || g.ip || "";
    const save = h("button", { class: "primary", onclick: () => act("Save " + g.id, async () => {
      await api("POST", "/api/ct", { id: g.id, name: g.name, preferred_node: f.preferred_node.value,
        standby: f.standby.value, mode: f.mode.value, interval_min: parseInt(f.interval_min.value, 10),
        priority: parseInt(f.priority.value, 10), liveness_ip: f.liveness_ip.value });
      await guests();
    }) }, "Save");
    box.append(h("div", { class: "form-grid" },
      h("label", {}, "Preferred node", f.preferred_node), h("label", {}, "Standby type", f.standby),
      h("label", {}, "Mode", f.mode), h("label", {}, "Copy every (minutes)", f.interval_min),
      h("label", {}, "Takeover priority (1 = first)", f.priority), h("label", {}, "Liveness IP", f.liveness_ip)),
      h("p", { class: "small muted" }, "Start with dry-run, check the Dashboard, then switch to live. ",
        "A duplicate container uses ID ", g.id + (conf.config.cluster && conf.config.cluster.standby_offset || 1000), " on the other node."),
      h("div", { class: "row" }, save, h("button", { onclick: () => box.remove() }, "Cancel")));
    return box;
  }

  const rows = list.map((g) => {
    const c = covered[g.id];
    const cell = h("td", {});
    const open = (form) => { if (!cell.querySelector(".inline-form")) cell.append(form); };
    let actions;
    if (g.role === "controller") actions = h("span", { class: "muted small" }, "watchdog controller");
    else if (g.role === "standby") actions = h("span", { class: "muted small" }, "standby copy of ", g.standby_of);
    else if (g.type !== "lxc") actions = h("span", { class: "muted small" }, "VMs: not supported yet");
    else if (c) actions = h("div", { class: "row" },
      h("button", { onclick: () => open(form(g, c)) }, "Edit"),
      h("button", { class: "danger", onclick: () => {
        if (!confirm("Stop protecting " + g.id + " (" + g.name + ")? Its standby copy is left in place; delete it by hand if you no longer need it.")) return;
        act("Remove " + g.id, async () => { await api("DELETE", "/api/ct/" + g.id); await guests(); });
      } }, "Remove"));
    else actions = h("button", { onclick: () => open(form(g, null)) }, "Protect…");
    cell.append(actions);
    return h("tr", {},
      h("td", {}, h("b", {}, g.id), " ", g.name),
      h("td", {}, g.type === "lxc" ? "container" : "VM"),
      h("td", {}, g.node, " ", pill(g.status, g.status === "running" ? "ok" : "")),
      h("td", { class: "small" }, g.ip || "—"),
      h("td", {}, c ? pill(c.mode + " · " + (c.standby || "file"), c.mode === "live" ? "ok" : "warn")
                    : (g.type === "lxc" && !g.role ? pill("not protected") : "")),
      cell);
  });

  // "Add protection": the obvious way in. Lists containers that aren't covered yet.
  const free = list.filter((g) => g.type === "lxc" && !g.role && !covered[g.id]);
  const pick = h("select", {}, free.map((g) => h("option", { value: g.id }, g.id + " · " + g.name + " (" + g.node + ")")));
  const addBox = h("div", {});
  const addCard = h("div", { class: "card" }, h("h2", {}, "Add protection"),
    free.length
      ? [h("p", { class: "small muted" }, "Choose a container that isn't protected yet. It starts in dry-run, so nothing changes until you switch it to live."),
         h("div", { class: "row" }, pick, h("button", { class: "primary", onclick: () => {
           const g = free.find((x) => String(x.id) === pick.value);
           addBox.replaceChildren(form(g, null));
         } }, "Protect…")),
         addBox]
      : h("p", { class: "muted" }, "Every container that can be protected already is. (VMs aren't supported yet.)"));

  setView(addCard, h("div", { class: "card" }, h("h2", {}, "Guests in the cluster"),
    h("table", {}, h("tr", {}, h("th", {}, "Guest"), h("th", {}, "Type"), h("th", {}, "Node"), h("th", {}, "IP"),
      h("th", {}, "Protection"), h("th", {}, "Action")), rows)));
}

// ---------- nodes ----------
async function nodes() {
  const o = await api("GET", "/api/overview");
  const rows = o.nodes.map((n) => h("tr", {},
    h("td", {}, h("b", {}, n.name), n.me ? " (this one)" : ""),
    h("td", {}, n.host_ip), h("td", {}, n.controller_id, " · ", n.controller_ip), h("td", {}, n.wol_mac || "—"),
    h("td", {}, n.me ? "" : h("div", { class: "row" },
      n.wol_mac ? h("button", { onclick: () => act("Wake " + n.name, () => api("POST", "/api/action", { action: "wake", node: n.name })) }, "Wake") : null,
      h("button", { class: "danger", onclick: () => {
        if (!confirm("Remove " + n.name + " from the watchdog config? (The node itself is not touched.)")) return;
        act("Remove " + n.name, async () => { await api("DELETE", "/api/node/" + encodeURIComponent(n.name)); await nodes(); });
      } }, "Remove")))));

  const f = { name: h("input", { placeholder: "pve3" }), host_ip: h("input", { placeholder: "host IP" }),
              controller_id: h("input", { type: "number", placeholder: "e.g. 9102" }),
              controller_ip: h("input", { placeholder: "a free static IP" }), wol_mac: h("input", { placeholder: "00:00:5e:00:53:10" }) };
  const how = h("pre", {}, "");
  const update = () => {
    how.textContent = "# on " + (f.name.value || "<new node>") + ", as root, after it has joined the Proxmox cluster:\n" +
      "scp -r pve-watchdog root@" + (f.host_ip.value || "<host-ip>") + ":/root/\n" +
      "ssh root@" + (f.host_ip.value || "<host-ip>") + " 'cd /root/pve-watchdog && ./install.sh " + (f.name.value || "<name>") + "'        # preview\n" +
      "ssh root@" + (f.host_ip.value || "<host-ip>") + " 'cd /root/pve-watchdog && ./install.sh " + (f.name.value || "<name>") + " --yes'  # install";
  };
  Object.values(f).forEach((i) => i.addEventListener("input", update));
  update();
  setView(
    h("div", { class: "card" }, h("h2", {}, "Nodes"),
      h("table", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Host"), h("th", {}, "Watchdog"), h("th", {}, "Wake-on-LAN MAC"), h("th", {}, "")), rows)),
    h("div", { class: "card" }, h("h2", {}, "Add a node"),
      h("p", { class: "small muted" }, "1. Join the machine to the Proxmox cluster. 2. Save it here. 3. Run the install commands below on it."),
      h("div", { class: "form-grid" }, h("label", {}, "Node name (as in Proxmox)", f.name), h("label", {}, "Host IP", f.host_ip),
        h("label", {}, "Watchdog container ID", f.controller_id), h("label", {}, "Watchdog IP", f.controller_ip),
        h("label", {}, "Wake-on-LAN MAC (optional)", f.wol_mac)),
      h("button", { class: "primary", onclick: () => act("Save node", async () => {
        await api("POST", "/api/node", { name: f.name.value.trim(), host_ip: f.host_ip.value.trim(),
          controller_id: parseInt(f.controller_id.value, 10), controller_ip: f.controller_ip.value.trim(),
          wol_mac: f.wol_mac.value.trim() });
        await nodes();
      }) }, "Save node"),
      how));
}

// ---------- settings ----------
async function settings() {
  const conf = await api("GET", "/api/config");
  const c = conf.config;
  const get = (k) => { const [s, key] = k.split("."); return s ? (c[s] || {})[key] : c[key]; };
  const groups = [
    ["General", [[".dry_run", "Dry-run (change nothing anywhere)", "bool"]]],
    ["Detecting a dead node", [["probe.interval", "Seconds between checks", "int"], ["probe.rounds", "Failed rounds before 'down'", "int"],
      ["probe.failover_delay", "Extra wait before takeover (s)", "int"], ["probe.timeout", "Timeout per check (s)", "int"]]],
    ["Failback and isolation", [["policy.failback", "Failback", ["auto", "manual", "never"]],
      ["policy.failback_stable_minutes", "Healthy for (min) before failback", "int"],
      ["policy.isolation_stop_after", "Cut-off node stops its guests after (s, 0 = never)", "int"],
      ["policy.stale_factor", "Warn when a copy is older than N × interval", "int"],
      ["policy.maintenance_max_hours", "Maintenance always ends after (h)", "int"]]],
    ["Wake-on-LAN", [["policy.wake_on_lan", "Wake a peer that is down", "bool"], ["policy.wake_after", "…after (s)", "int"],
      ["policy.wake_every", "…repeat every (s)", "int"], ["policy.wake_tries", "…at most (tries)", "int"]]],
    ["Network and storage", [["cluster.gateway", "Gateway IP (isolation check)", "str"],
      ["cluster.pool_max_used_pct", "Refuse restores above this thin-pool use (%)", "int"],
      ["replication.bwlimit_kbps", "Copy bandwidth limit (KiB/s)", "int"]]],
  ];
  const inputs = {};
  const cards = groups.map(([title, fields]) => h("div", { class: "card" }, h("h3", {}, title),
    h("div", { class: "form-grid" }, fields.map(([key, label, kind]) => {
      let inp;
      if (kind === "bool") inp = h("input", { type: "checkbox", checked: get(key) });
      else if (Array.isArray(kind)) { inp = h("select", {}, kind.map((o) => h("option", { value: o }, o))); inp.value = get(key); }
      else inp = h("input", { type: kind === "int" ? "number" : "text", value: get(key) === undefined ? "" : get(key) });
      inputs[key] = [inp, kind];
      return kind === "bool" ? h("label", {}, inp, label) : h("label", {}, label, inp);
    }))));
  const save = h("button", { class: "primary", onclick: () => {
    const body = {};
    for (const [key, [inp, kind]] of Object.entries(inputs)) {
      const v = kind === "bool" ? inp.checked : kind === "int" ? parseInt(inp.value, 10) : inp.value;
      if (kind === "int" && Number.isNaN(v)) continue;
      if (v !== get(key)) body[key] = v;
    }
    if (!Object.keys(body).length) return banner("Nothing changed", "ok");
    if (body[".dry_run"] === false && !confirm("Switch dry-run OFF? The watchdog will then really act.")) return;
    act("Save settings", async () => { await api("POST", "/api/settings", body); await settings(); });
  } }, "Save settings");
  const hook = h("input", { type: "password", placeholder: conf.webhook_set ? "(set — type a new one to replace)" : "https://discord.com/api/webhooks/…" });
  setView(...cards,
    h("div", { class: "card" }, h("h3", {}, "Discord alerts"),
      h("p", { class: "small muted" }, "The webhook URL is stored in the root-only secret file and is never shown again."),
      h("label", {}, "Webhook URL", hook),
      h("div", { class: "row" },
        h("button", { onclick: () => act("Webhook", async () => { await api("POST", "/api/secret", { discord_webhook: hook.value }); hook.value = ""; await settings(); }) }, "Save webhook"),
        h("button", { onclick: () => act("Test alert", () => api("POST", "/api/action", { action: "test_notify" })) }, "Send test alert"))),
    h("div", { class: "row" }, save));
}

// ---------- history ----------
async function history() {
  const list = await api("GET", "/api/history");
  const rows = list.map((v) => h("tr", {}, h("td", {}, new Date(v.ts * 1000).toLocaleString()), h("td", { class: "muted small" }, v.size, " bytes"),
    h("td", {}, h("button", { onclick: () => {
      if (!confirm("Restore the configuration saved at " + new Date(v.ts * 1000).toLocaleString() + "?")) return;
      act("Restore", async () => { await api("POST", "/api/history/restore", { ts: v.ts }); await history(); });
    } }, "Restore"))));
  setView(h("div", { class: "card" }, h("h2", {}, "Earlier configurations"),
    h("p", { class: "small muted" }, "Every save keeps the previous version (the last 10). Restoring also keeps the current one."),
    list.length ? h("table", {}, rows) : h("p", { class: "muted" }, "No earlier versions yet.")));
}

// ---------- start ----------
initTheme();
api("GET", "/api/session").then((s) => afterLogin(s.user, s.csrf)).catch(() => showLogin());
