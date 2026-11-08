"""The Proxmox Notes block: added once, replaced in place, user text kept."""
import importlib.util
import os
import unittest

spec = importlib.util.spec_from_file_location(
    "pve_notes", os.path.join(os.path.dirname(__file__), "..", "tools", "pve_notes.py"))
notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notes)


class NotesTests(unittest.TestCase):
    def test_link_is_clickable_markdown(self):
        self.assertIn("](https://192.168.100.21:8443)", notes.node_block("192.168.100.21"))

    def test_added_to_empty_and_to_existing_notes(self):
        self.assertTrue(notes.merge("", notes.node_block("192.168.100.21")).startswith(notes.START))
        out = notes.merge("My own notes.", notes.node_block("192.168.100.21"))
        self.assertTrue(out.startswith("My own notes.\n\n" + notes.START))

    def test_replaced_in_place_keeping_user_text(self):
        first = notes.merge("Top.\n\nBottom.", notes.node_block("192.168.100.21"))
        first = first.replace("Bottom.\n\n", "")  + "\n\nBottom."
        second = notes.merge(first, notes.node_block("192.168.100.22"))
        self.assertEqual(second.count(notes.START), 1)
        self.assertIn("192.168.100.22:8443", second)
        self.assertNotIn("192.168.100.21:8443", second)
        self.assertTrue(second.startswith("Top."))
        self.assertTrue(second.endswith("Bottom."))

    def test_old_unmarked_note_is_upgraded(self):
        legacy = ("Mine.\n\n**pve-watchdog** protects selected containers on this cluster.\n\n"
                  "- Status: `pve-watchdog status`")
        out = notes.merge(legacy, notes.node_block("192.168.100.21"))
        self.assertTrue(out.startswith("Mine."))
        self.assertNotIn("protects selected containers", out)
        self.assertEqual(out.count(notes.START), 1)


if __name__ == "__main__":
    unittest.main()
