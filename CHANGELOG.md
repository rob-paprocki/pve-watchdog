# Changelog

All notable changes to pve-watchdog are listed here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Every release says whether you need
to do anything when upgrading.

## [Unreleased]

## [1.1.1] - 2026-11-08
**Upgrading:** re-run `get.sh` on each node; nothing else to do.

### Changed
- The diagrams in the README and docs are images now, so they also show in
  GitHub's mobile app (it doesn't draw Mermaid).

### Fixed
- After a node rebooted in the middle of a backup, every later backup of that
  container failed ("Is a directory") until the leftover files were removed by
  hand. Leftovers from an interrupted backup are now cleaned up first.

## [1.1.0] - 2026-11-08
**Upgrading:** re-run `get.sh` on each node to get the web UI fixes; nothing else
changes.

### Added
- Web UI screenshots and diagrams in the README; the screenshots follow the
  visitor's light or dark mode.

### Changed
- The README is shorter and leads with what it does and how to install it. The
  full design, failure modes, go-live steps and uninstall moved to
  `docs/how-it-works.md`.

### Fixed
- The web UI dashboard showed a stray "null" at the top when dry-run was off.
- The web UI footer showed version 1.0.0 on 1.0.1; a release now writes its
  version into the code too.

## [1.0.1] - 2026-11-08
**Upgrading:** re-run the installer on each node (or `get.sh`); nothing else to do.

### Security
- A protected container's standby ID that is already used by an unrelated
  container is refused, and the watchdog only ever overwrites, starts or stops
  a standby that carries the `pve-watchdog-standby` tag.
- Leftover-disk cleanup now counts disks referenced only by snapshots as in use.
- The web UI's limit of 5 failed logins can no longer be raced with parallel attempts.

### Fixed
- A setting saved in the web UI now takes effect on every node within seconds
  (the other watchdogs are told to re-read the shared config right away,
  instead of within a minute).
- The release workflow can also be started by hand for a tag, and skips a
  release that already exists.

## [1.0.0] - 2026-11-08
**Upgrading:** nothing to do. This is the first release; install with the
one-command installer described in the README.

### Added
- Failover for selected LXC containers on Proxmox VE clusters with local
  (LVM-thin) storage: when a node dies, another node starts the container with
  the same MAC address and IP.
- Two kinds of standby per container: a true duplicate container (ID + 1000,
  started in seconds) or a backup file restored under the same ID.
- Automatic failback after the original node has been healthy for 10 minutes:
  the standby is stopped first and its data is copied back, so nothing is lost.
- Guards against two copies running at once: a four-signal "is it really down?"
  check, a gateway check, a liveness check on each container's own IP, boot
  arbitration, self-demotion of a cut-off node, and force-quorum only when the
  other node is truly gone.
- Host gatekeeper that is the only part running Proxmox commands, with
  code-enforced safety rules and a strict config schema. Dry-run by default.
- Web UI on every watchdog (`https://<watchdog-ip>:8443`, Proxmox admin login):
  dashboard, protect/edit containers, nodes, settings, config history, light/dark theme.
- Wake-on-LAN: a node that goes down is sent magic packets; the setting is
  re-applied at every boot.
- Maintenance mode, switched on automatically for normal reboots and shutdowns.
- Discord alerts when a problem starts and when it clears.
- One-command installer with a setup wizard.
- Proxmox Notes on each node and watchdog container with a clickable link to
  the web UI.
