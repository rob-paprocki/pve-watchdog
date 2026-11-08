"""Offline test of the setup wizard's first-node path (fake cluster, scripted answers)."""
import builtins
import importlib.util
import os
import tomllib
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("wizard", os.path.join(HERE, "..", "tools", "setup_wizard.py"))
wiz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wiz)


class FirstNodeTests(unittest.TestCase):
    def run_wizard(self, answers):
        it = iter(answers)
        with mock.patch.object(wiz, "cluster_nodes", lambda: [("pve1", "192.168.100.11"), ("pve2", "192.168.100.12")]), \
             mock.patch.object(wiz, "used_ids", lambda: {100, 101, 9100}), \
             mock.patch.object(wiz, "default_gateway", lambda: "192.168.100.1"), \
             mock.patch.object(wiz, "bridge_mac", lambda host=None, node=None: "00:00:5e:00:53:0%d" % (1 if host is None else 2)), \
             mock.patch.object(wiz, "ip_in_use", lambda ip: ip == "192.168.100.99"), \
             mock.patch.object(wiz, "me", lambda: "pve1"), \
             mock.patch.object(builtins, "input", lambda prompt="": next(it)), \
             mock.patch("builtins.print"):
            return wiz.first_node_config()

    def test_builds_valid_dry_run_config(self):
        text = self.run_wizard([
            "",                    # gateway: accept detected
            "", "192.168.100.21", "",      # pve1: suggested ID (9101), IP, detected MAC
            "", "192.168.100.22", "",      # pve2: suggested ID (9102), IP, detected MAC
        ])
        cfg = tomllib.loads(text)
        self.assertTrue(cfg["dry_run"])
        self.assertEqual(cfg["cluster"]["gateway"], "192.168.100.1")
        nodes = {n["name"]: n for n in cfg["node"]}
        self.assertEqual(set(nodes), {"pve1", "pve2"})
        self.assertEqual(nodes["pve1"]["controller_id"], 9101)        # 9100 was taken
        self.assertEqual(nodes["pve2"]["controller_id"], 9102)
        self.assertEqual(nodes["pve2"]["wol_mac"], "00:00:5e:00:53:02")
        self.assertEqual(cfg.get("ct", []), [])                       # nothing protected yet
        wiz.gatekeeper().validate_cfg(cfg)                            # passes the real rules

    def test_rejects_used_id_and_busy_ip(self):
        text = self.run_wizard([
            "",
            "100", "9200", "192.168.100.99", "192.168.100.21", "",   # 100 taken, .99 answers ping
            "", "192.168.100.21", "192.168.100.22", "",              # .21 already used above
        ])
        nodes = {n["name"]: n for n in tomllib.loads(text)["node"]}
        self.assertEqual(nodes["pve1"]["controller_id"], 9200)
        self.assertEqual(nodes["pve2"]["controller_ip"], "192.168.100.22")


if __name__ == "__main__":
    unittest.main()
