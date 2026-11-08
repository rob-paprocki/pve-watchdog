"""pve-watchdog controller ("the brain").

Runs inside the controller container on every node. Every node runs the
same code with the same shared config; there is no master. Each controller
watches the other nodes, keeps backup copies flowing, and decides when its
own node should start or stop a covered container.

It never runs Proxmox commands itself: it asks its own host's gatekeeper
(pve-watchdog-hostcmd) over SSH, and the gatekeeper re-checks every safety rule.
"""
__version__ = "1.0.0"
