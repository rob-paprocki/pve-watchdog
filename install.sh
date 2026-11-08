#!/bin/bash
# pve-watchdog installer. Run ON a Proxmox host, as root, from this folder:
#
#     ./install.sh pve1          # PREVIEW: prints every command, changes nothing
#     ./install.sh pve1 --yes    # actually do it
#
# What it does, in plain language:
#   1. Host:  installs the gatekeeper script (/usr/local/sbin/pve-watchdog-hostcmd)
#             and creates /var/lib/pve-watchdog/{archive,work} for backup files.
#   2. Config (first node only): copies config.example.toml to
#             /etc/pve/pve-watchdog/config.toml (dry_run = true) and creates the
#             secret file with a random controller signing key.
#   3. Controller container: creates the privileged controller LXC for this
#             node (ID and IP from the config), installs Python into it, and
#             copies the controller code + systemd units in.
#   4. SSH:   gives the controller its own key and lets that key run ONLY the
#             gatekeeper, ONLY from the controller's IP (/root/.ssh/authorized_keys2,
#             a per-host file; the cluster-shared key file is not touched).
#   5. Starts the controller services.
#
# It never creates /etc/pve-watchdog/allow-live, so the host stays in dry-run.
# It does not touch disks, storage.cfg, corosync, votes, or any other container.
set -euo pipefail

NODE="${1:-}"
YES="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="debian-13-standard_13.6-1_amd64.tar.zst"

if [[ -z "$NODE" ]]; then
    echo "usage: $0 <node-name> [--yes]"; exit 2
fi
if [[ "$(hostname -s)" != "$NODE" ]]; then
    echo "this host is $(hostname -s), not $NODE. Run it on $NODE."; exit 2
fi

step() { echo; echo "### $*"; }
run() {
    echo "+ $*"
    if [[ "$YES" == "--yes" ]]; then
        "$@"
    fi
}

CFG=/etc/pve/pve-watchdog/config.toml
SRC_CFG="$CFG"
[[ -f "$CFG" ]] || SRC_CFG="$HERE/config.example.toml"

# Read this node's controller ID/IP and the gateway from the config.
read -r CID CIP GW < <(python3 -I - "$SRC_CFG" "$NODE" <<'PY'
import sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
n = next(n for n in cfg["node"] if n["name"] == sys.argv[2])
print(n["controller_id"], n["controller_ip"], cfg["cluster"]["gateway"])
PY
)
HOST_IP="$(python3 -I - "$SRC_CFG" "$NODE" <<'PY'
import sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
print(next(n for n in cfg["node"] if n["name"] == sys.argv[2])["host_ip"])
PY
)"
echo "node=$NODE host_ip=$HOST_IP controller id=$CID ip=$CIP gateway=$GW"
[[ "$YES" == "--yes" ]] || echo "(PREVIEW ONLY: add --yes to run these commands)"

step "1. Host gatekeeper"
run install -m 0755 "$HERE/host/pve-watchdog-hostcmd" /usr/local/sbin/pve-watchdog-hostcmd
run mkdir -p /etc/pve-watchdog /var/lib/pve-watchdog/archive /var/lib/pve-watchdog/work
# Short host command: `pve-watchdog status`, `pve-watchdog maintenance on 1h`
run install -m 0755 "$HERE/host/pve-watchdog" /usr/local/sbin/pve-watchdog
# Planned shutdown/reboot => maintenance on for 20 min automatically
run install -m 0644 "$HERE/host/pve-watchdog-planned-shutdown.service" /etc/systemd/system/
run systemctl daemon-reload
run systemctl enable --now pve-watchdog-planned-shutdown.service
# Wake-on-LAN on the bridge's network card, re-applied at every boot
run install -m 0644 "$HERE/host/pve-watchdog-wol.service" /etc/systemd/system/
run systemctl daemon-reload
run systemctl enable pve-watchdog-wol.service
run systemctl restart pve-watchdog-wol.service

step "1b. Notes with a link to the web UI (web UI > node > Summary)"
APPLY=""; [[ "$YES" == "--yes" ]] && APPLY="--apply"
python3 -I "$HERE/tools/pve_notes.py" node "$NODE" "$CIP" $APPLY

step "2. Shared config + secret (only if missing)"
if [[ ! -f "$CFG" ]]; then
    run mkdir -p /etc/pve/pve-watchdog
    run cp "$HERE/config.example.toml" "$CFG"
else
    echo "  $CFG already exists, leaving it alone"
fi
if [[ ! -f /etc/pve/priv/pve-watchdog.secret.toml ]]; then
    KEY="$(python3 -I -c 'import secrets; print(secrets.token_hex(32))')"
    echo "+ write /etc/pve/priv/pve-watchdog.secret.toml (random hmac_key, empty discord_webhook)"
    if [[ "$YES" == "--yes" ]]; then
        umask 077
        printf 'hmac_key = "%s"\n# Paste your Discord webhook URL between the quotes:\ndiscord_webhook = ""\n' \
            "$KEY" > /etc/pve/priv/pve-watchdog.secret.toml
    fi
else
    echo "  secret file already exists, leaving it alone"
fi

step "3. Controller container $CID"
if compgen -G "/etc/pve/nodes/*/lxc/$CID.conf" >/dev/null || \
   compgen -G "/etc/pve/nodes/*/qemu-server/$CID.conf" >/dev/null; then
    echo "  ID $CID already exists; not creating it"
else
    if ! pveam list local | grep -q "$TEMPLATE"; then
        run pveam download local "$TEMPLATE"
    fi
    run pct create "$CID" "local:vztmpl/$TEMPLATE" \
        --hostname "watchdog-${NODE#pve-}" --unprivileged 0 --features nesting=1 \
        --cores 1 --memory 256 --swap 256 --rootfs local-lvm:4 --ostype debian \
        --net0 "name=eth0,bridge=vmbr0,ip=$CIP/24,gw=$GW" --nameserver "$GW" \
        --onboot 1 --startup order=1 --tags pve-watchdog-controller \
        --description "pve-watchdog watchdog for $NODE"
    run pct start "$CID"
fi
echo "+ wait (up to 60 s) until the controller can resolve deb.debian.org"
if [[ "$YES" == "--yes" ]]; then
    for _ in $(seq 30); do
        pct exec "$CID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
        sleep 2
    done
fi
run pct exec "$CID" -- bash -c "apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 openssh-client iputils-ping"

step "4. Controller code, identity and SSH key"
run tar -C "$HERE/controller" -cf /tmp/pve-watchdog-controller.tar pve_watchdog systemd pve-watchdog
run pct push "$CID" /tmp/pve-watchdog-controller.tar /root/pve-watchdog-controller.tar
run pct exec "$CID" -- bash -c "mkdir -p /opt/pve-watchdog /etc/pve-watchdog /var/lib/pve-watchdog && \
    tar -C /opt/pve-watchdog -xf /root/pve-watchdog-controller.tar && \
    install -m 0755 /opt/pve-watchdog/pve-watchdog /usr/local/bin/pve-watchdog && ln -sf /usr/local/bin/pve-watchdog /usr/bin/pve-watchdog && \
    cp /opt/pve-watchdog/systemd/*.service /opt/pve-watchdog/systemd/*.timer /etc/systemd/system/ && \
    printf 'node = \"%s\"\nhost_ip = \"%s\"\n' '$NODE' '$HOST_IP' > /etc/pve-watchdog/local.toml && \
    ( [ -f /etc/pve-watchdog/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -C pve-watchdog-controller-$NODE -f /etc/pve-watchdog/id_ed25519 )"
# HTTPS certificate for the web UI (made once; self-signed, valid 10 years).
echo "+ web UI certificate for https://$CIP:8443 (only if the controller has none yet)"
if [[ "$YES" == "--yes" ]] && ! pct exec "$CID" -- test -f /etc/pve-watchdog/webui.crt; then
    TMPD="$(mktemp -d)"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=watchdog-${NODE#pve-}" \
        -addext "subjectAltName=IP:$CIP" -keyout "$TMPD/webui.key" -out "$TMPD/webui.crt" 2>/dev/null
    pct push "$CID" "$TMPD/webui.key" /etc/pve-watchdog/webui.key --perms 0600
    pct push "$CID" "$TMPD/webui.crt" /etc/pve-watchdog/webui.crt --perms 0644
    rm -rf "$TMPD"
fi
# Same timezone as the host, so alert times read naturally.
TZ_HOST="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
run pct exec "$CID" -- ln -sf "/usr/share/zoneinfo/$TZ_HOST" /etc/localtime
# The controller must recognise its own host: take the host key straight from the host.
run pct exec "$CID" -- bash -c "echo '$HOST_IP $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)' > /etc/pve-watchdog/known_hosts"

echo "+ allow that key to run ONLY the gatekeeper, ONLY from $CIP (in /root/.ssh/authorized_keys2)"
if [[ "$YES" == "--yes" ]]; then
    PUB="$(pct exec "$CID" -- cat /etc/pve-watchdog/id_ed25519.pub)"
    touch /root/.ssh/authorized_keys2
    chmod 600 /root/.ssh/authorized_keys2
    grep -v "pve-watchdog-controller-$NODE" /root/.ssh/authorized_keys2 > /root/.ssh/authorized_keys2.new || true
    echo "restrict,command=\"/usr/local/sbin/pve-watchdog-hostcmd\",from=\"$CIP\" $PUB" >> /root/.ssh/authorized_keys2.new
    mv /root/.ssh/authorized_keys2.new /root/.ssh/authorized_keys2
fi

step "4b. The watchdog container's Notes (link to the web UI)"
if [[ "$YES" == "--yes" ]]; then
    python3 -I "$HERE/tools/pve_notes.py" ct "$CID" "$NODE" "$CIP" --apply
else
    python3 -I "$HERE/tools/pve_notes.py" ct "$CID" "$NODE" "$CIP" 2>/dev/null \
        || echo "  would set the watchdog container's Notes (link to https://$CIP:8443)"
fi

step "5. Start the controller services"
run pct exec "$CID" -- systemctl daemon-reload
run pct exec "$CID" -- systemctl enable pve-watchdog.service pve-watchdog-sync.timer
run pct exec "$CID" -- systemctl restart pve-watchdog.service pve-watchdog-sync.timer

echo
echo "Done. Check with:  pct exec $CID -- pve-watchdog status"
