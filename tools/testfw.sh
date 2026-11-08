#!/bin/bash
# TEST HELPER ONLY: make this host unreachable from/to ONE peer (host + controller),
# so the peer believes this node is dead. Everything else on the LAN still works.
#
#   testfw.sh on  <peer-host-ip> <peer-controller-ip>   # block (auto-undo in 20 min)
#   testfw.sh off                                       # remove the block now
#
# Every rule carries the comment "pvewd-test"; "off" removes only those rules.
set -euo pipefail
TAG=pvewd-test

off() {
    for chain in INPUT OUTPUT; do
        while iptables -S "$chain" | grep -q -- "--comment $TAG"; do
            rule="$(iptables -S "$chain" | grep -m1 -- "--comment $TAG" | sed "s/^-A /-D /")"
            eval iptables $rule
        done
    done
    systemctl stop pvewd-test-undo.timer 2>/dev/null || true
    echo "test block removed"
}

case "${1:-}" in
on)
    PEER="$2"; PEERCTL="$3"
    for ip in "$PEER" "$PEERCTL"; do
        iptables -I INPUT  -s "$ip" -m comment --comment "$TAG" -j DROP
        iptables -I OUTPUT -d "$ip" -m comment --comment "$TAG" -j DROP
    done
    systemd-run --quiet --unit pvewd-test-undo --on-active=20m "$0" off
    echo "blocking $PEER and $PEERCTL (auto-undo in 20 min)"
    ;;
off) off ;;
*) echo "usage: $0 on <peer-host-ip> <peer-controller-ip> | off"; exit 2 ;;
esac
