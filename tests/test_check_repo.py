"""Tests of tests/check_repo.py: the pattern checks and the image checks."""
import hashlib
import importlib.util
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_repo = load("check_repo", os.path.join(HERE, "check_repo.py"))

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")


def IP(*parts):
    return ".".join(parts)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True,
                          check=True, env=ENV).stdout.strip()


def make_repo(files):
    d = tempfile.mkdtemp()
    git(d, "init", "-q", "-b", "main")
    for path, text in files.items():
        p = os.path.join(d, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    return d

class PatternTests(unittest.TestCase):
    def kinds(self, text):
        return [k for _, k in check_repo.scan_text(text)]

    def test_allowed_values(self):
        self.assertEqual(self.kinds("gw 192.168.100.1, dns 1.1.1.1, lo 127.0.0.1, doc 203.0.113.9\n"
                                    "mac 00:00:5E:00:53:0a, mail admin@example.com, x@y.invalid\n"
                                    "bcast 255.255.255.255 v6 2001:db8::1 ::1\n"
                                    "time 01:02:52 version 1.2.3 port 8006"), [])

    def test_disallowed_values(self):
        # The "bad" samples are assembled at runtime so that no disallowed value
        # appears literally in this (public) file.
        self.assertEqual(self.kinds(IP("10", "1", "2", "3")), ["IP address"])         # private
        self.assertEqual(self.kinds(IP("100", "64", "1", "2")), ["IP address"])       # CGNAT
        self.assertEqual(self.kinds(IP("8", "8", "4", "5")), ["IP address"])          # public
        self.assertEqual(self.kinds(":".join(["fd12", "3456", "789a", "", "1"])), ["IPv6 address"])
        self.assertEqual(self.kinds(":".join(["e0", "51", "d8", "00", "11", "22"])), ["MAC address"])
        self.assertEqual(self.kinds("me" + "@" + "mail-provider.com"), ["email address"])

    def test_report_never_contains_the_value(self):
        bad = IP("10", "20", "30", "40")
        d = make_repo({"a.txt": "secret %s here\n" % bad, "private/notes.md": bad + "\n"})
        problems = check_repo.check(d)
        self.assertEqual(problems, ["a.txt:1: IP address not allowed in public files"])   # private/ skipped
        self.assertNotIn(bad, "".join(problems))

    def test_this_repo_is_clean(self):
        self.assertEqual(check_repo.check(ROOT), [])


def png(*chunks, tail=b""):
    out = b"\x89PNG\r\n\x1a\n"
    for kind, data in (("IHDR", b"\0" * 13),) + chunks + (("IEND", b""),):
        out += len(data).to_bytes(4, "big") + kind.encode() + data + b"\0\0\0\0"
    return out + tail


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "docs", "images"))

    def put(self, rel, data, approve=True):
        with open(os.path.join(self.root, rel), "wb") as f:
            f.write(data)
        if approve:
            with open(os.path.join(self.root, "docs/images/approved.sha256"), "a") as f:
                f.write("%s  %s\n" % (hashlib.sha256(data).hexdigest(), os.path.basename(rel)))

    def problems(self):
        return check_repo.check(self.root)

    def test_approved_png_passes(self):
        self.put("docs/images/shot.png", png(("IDAT", b"pixels")))
        self.assertEqual(self.problems(), [])

    def test_unlisted_png_fails(self):
        self.put("docs/images/shot.png", png(("IDAT", b"pixels")), approve=False)
        self.assertTrue(any("not listed" in p for p in self.problems()))

    def test_changed_png_fails(self):
        self.put("docs/images/shot.png", png(("IDAT", b"pixels")))
        with open(os.path.join(self.root, "docs/images/shot.png"), "wb") as f:
            f.write(png(("IDAT", b"other pixels")))
        self.assertTrue(any("not listed" in p for p in self.problems()))

    def test_metadata_chunk_fails_even_if_approved(self):
        self.put("docs/images/shot.png", png(("tEXt", b"Comment\0host secret"), ("IDAT", b"x")))
        self.assertTrue(any("metadata" in p for p in self.problems()))

    def test_data_after_end_fails(self):
        self.put("docs/images/shot.png", png(("IDAT", b"x"), tail=b"hidden"))
        self.assertTrue(any("after its end" in p for p in self.problems()))

    def test_png_outside_images_dir_fails(self):
        self.put("shot.png", png(("IDAT", b"x")))
        self.assertTrue(any("not UTF-8" in p for p in self.problems()))

    def test_approval_is_for_one_name(self):
        data = png(("IDAT", b"x"))
        self.put("docs/images/a.png", data)
        self.put("docs/images/b.png", data, approve=False)
        self.assertTrue(any("b.png" in p and "not listed" in p for p in self.problems()))

    def test_repo_images_pass(self):
        self.assertEqual([p for p in check_repo.check(ROOT) if "docs/images" in p], [])


if __name__ == "__main__":
    unittest.main()
