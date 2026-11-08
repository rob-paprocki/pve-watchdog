# Notes for Claude

If `private/RELEASING.md` exists, follow it when asked to cut a release.

- Tests: `PYTHONPATH=controller python3 -m unittest discover -s tests`
- Public files must pass `python3 tests/check_repo.py` (example network
  `192.168.100.0/24`, MACs `00:00:5E:00:53:xx`, placeholder emails only).
- Every user-visible change gets a line under `## [Unreleased]` in
  `CHANGELOG.md`; commits that don't need one say `[no changelog]`.
- The host gatekeeper (`host/pve-watchdog-hostcmd`) is the safety boundary:
  every rule it enforces must stay enforced in code, with a test.
