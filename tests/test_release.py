"""Tests of the release tooling: pattern checks, public-copy builder, changelog, publisher."""
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
build = load("build_public", os.path.join(ROOT, "scripts", "release", "build_public.py"))
changelog = load("changelog", os.path.join(ROOT, "scripts", "release", "changelog.py"))
publish = load("publish_public", os.path.join(ROOT, "scripts", "release", "publish_public.py"))

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


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.wl = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        self.wl.write("SN-ABC123\n")
        self.wl.close()
        self.pat = build.load_wordlist(self.wl.name)

    def out(self):
        return os.path.join(tempfile.mkdtemp(), "copy")

    def test_private_removed_and_excludes_honoured(self):
        d = make_repo({"README.md": "hi [lic](LICENSE)\n", "LICENSE": "GPL\n",
                       "private/plan.md": "mine\n", "private/public-exclude.txt": ".github/workflows/cut.yml\n",
                       ".github/workflows/cut.yml": "private workflow\n"})
        files = build.build("HEAD", self.out(), self.pat, repo=d)
        self.assertEqual(files, ["LICENSE", "README.md"])

    def test_word_in_file_name_fails_without_printing_it(self):
        d = make_repo({"docs/SN-ABC123.md": "nothing\n"})
        with self.assertRaises(build.PublishError) as e:
            build.build("HEAD", self.out(), self.pat, repo=d)
        self.assertNotIn("ABC123", str(e.exception))
        self.assertIn("path withheld", str(e.exception))

    def test_word_in_content_and_message(self):
        d = make_repo({"a.md": "serial sn-abc123\n"})
        with self.assertRaises(build.PublishError):
            build.build("HEAD", self.out(), self.pat, repo=d)
        d2 = make_repo({"a.md": "fine\n"})
        with self.assertRaises(build.PublishError):
            build.build("HEAD", self.out(), self.pat, ["release for SN-ABC123"], repo=d2)

    def test_empty_or_missing_wordlist_fails(self):
        with open(self.wl.name, "w") as f:
            f.write("\n# only a comment\n")
        with self.assertRaises(build.PublishError):
            build.load_wordlist(self.wl.name)
        with self.assertRaises(build.PublishError):
            build.load_wordlist("/nonexistent/wordlist")
        with self.assertRaises(build.PublishError):
            build.load_wordlist(None, None)

    def test_non_utf8_fails(self):
        d = make_repo({"a.md": "x\n"})
        with open(os.path.join(d, "blob.bin"), "wb") as f:
            f.write(b"\xff\xfe\x00bad")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "bin")
        with self.assertRaises(build.PublishError):
            build.build("HEAD", self.out(), self.pat, repo=d)

    def test_link_into_private_fails(self):
        d = make_repo({"README.md": "see [plan](private/plan.md)\n", "private/plan.md": "x\n"})
        with self.assertRaises(build.PublishError) as e:
            build.build("HEAD", self.out(), self.pat, repo=d)
        self.assertIn("link", str(e.exception))

    def _linked_repo(self, target):
        d = make_repo({"a.md": "x\n", "private/secret.md": "mine\n"})
        os.symlink(target, os.path.join(d, "innocent.md"))
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "link")
        return d

    def test_relative_symlink_refused(self):
        d = self._linked_repo("private/secret.md")
        with self.assertRaises(build.PublishError) as e:
            build.build("HEAD", self.out(), self.pat, repo=d)
        self.assertIn("symbolic link", str(e.exception))
        self.assertNotIn("secret", str(e.exception))

    def test_absolute_symlink_refused(self):
        d = self._linked_repo("/etc/hosts")
        with self.assertRaises(build.PublishError) as e:
            build.build("HEAD", self.out(), self.pat, repo=d)
        self.assertNotIn("innocent", str(e.exception))

    def test_untracked_files_cannot_slip_in(self):
        d = make_repo({"a.md": "x\n"})
        with open(os.path.join(d, "stray-notes.md"), "w") as f:
            f.write("my %s\n" % IP("10", "9", "8", "7"))
        self.assertEqual(build.build("HEAD", self.out(), self.pat, repo=d), ["a.md"])


CL = """# Changelog

## [Unreleased]

**Upgrading:** nothing to do.

### Fixed
- a bug

## [1.2.3] - 2026-01-01

**Upgrading:** nothing to do.

### Added
- things
"""


class ChangelogTests(unittest.TestCase):
    def test_bumps(self):
        self.assertEqual(changelog.plan(CL)["version"], "1.2.4")
        self.assertEqual(changelog.plan(CL.replace("### Fixed", "### Added", 1))["version"], "1.3.0")
        self.assertEqual(changelog.plan(CL.replace("- a bug", "- **Breaking** x", 1))["version"], "2.0.0")
        self.assertEqual(changelog.plan(CL.replace("### Fixed", "### Removed", 1))["version"], "2.0.0")
        self.assertEqual(changelog.plan(CL, bump="minor")["version"], "1.3.0")

    def test_first_release(self):
        first = CL.split("## [1.2.3]")[0]
        self.assertEqual(changelog.plan(first)["version"], "1.0.0")

    def test_upgrading_line_required(self):
        with self.assertRaises(changelog.ChangelogError):
            changelog.plan(CL.replace("**Upgrading:** nothing to do.\n\n### Fixed", "### Fixed", 1))

    def test_finalize(self):
        new = changelog.finalize(CL, "1.2.4", "2026-02-02")
        self.assertIn("## [Unreleased]\n\n## [1.2.4] - 2026-02-02", new)
        self.assertEqual(changelog.notes(new, "1.2.4").splitlines()[0], "**Upgrading:** nothing to do.")
        with self.assertRaises(changelog.ChangelogError):
            changelog.finalize(new, "1.2.3", "2026-02-02")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.public = make_repo({".github/FUNDING.yml": "github: someone\n"})
        self.copy = tempfile.mkdtemp()
        for path, text in {"README.md": "v1\n", "old.txt": "goes away later\n"}.items():
            with open(os.path.join(self.copy, path), "w") as f:
                f.write(text)

    def test_first_release_then_second_deletes_removed_files(self):
        self.assertEqual(publish.prepare(self.public, self.copy, "1.0.0"), "prepared")
        self.assertEqual(git(self.public, "log", "-1", "--format=%s|%ae"),
                         "pve-watchdog 1.0.0|41898282+github-actions[bot]@users.noreply.github.com")
        self.assertEqual(git(self.public, "rev-list", "--count", "HEAD"), "2")   # on top of initial
        os.unlink(os.path.join(self.copy, "old.txt"))
        self.assertEqual(publish.prepare(self.public, self.copy, "1.1.0"), "prepared")
        self.assertFalse(os.path.exists(os.path.join(self.public, "old.txt")))
        self.assertEqual(git(self.public, "tag", "--list"), "v1.0.0\nv1.1.0")

    def test_direct_commit_on_public_repo_stops_it(self):
        publish.prepare(self.public, self.copy, "1.0.0")
        with open(os.path.join(self.public, "hotfix.txt"), "w") as f:
            f.write("someone committed here\n")
        git(self.public, "add", "-A")
        git(self.public, "commit", "-q", "-m", "direct")
        with self.assertRaises(publish.PublishError):
            publish.prepare(self.public, self.copy, "1.1.0")

    def test_first_release_refused_if_public_already_has_extra_commits(self):
        with open(os.path.join(self.public, "x.txt"), "w") as f:
            f.write("x\n")
        git(self.public, "add", "-A")
        git(self.public, "commit", "-q", "-m", "extra")
        with self.assertRaises(publish.PublishError):
            publish.prepare(self.public, self.copy, "1.0.0")

    def test_first_release_on_acknowledged_base(self):
        with open(os.path.join(self.public, "x.txt"), "w") as f:
            f.write("owner edit\n")
        git(self.public, "add", "-A")
        git(self.public, "commit", "-q", "-m", "owner edit")
        head = git(self.public, "rev-parse", "HEAD")
        with self.assertRaises(publish.PublishError):                     # wrong base
            publish.prepare(self.public, self.copy, "1.0.0", first_base="0000000")
        self.assertEqual(publish.prepare(self.public, self.copy, "1.0.0", first_base=head[:7]), "prepared")

    def test_rerun_after_half_done_publish_is_a_no_op(self):
        publish.prepare(self.public, self.copy, "1.0.0")         # tag made, (pretend) push of main failed
        self.assertEqual(publish.prepare(self.public, self.copy, "1.0.0"), "exists")
        with open(os.path.join(self.copy, "README.md"), "w") as f:
            f.write("different\n")
        with self.assertRaises(publish.PublishError):              # same tag, other content
            publish.prepare(self.public, self.copy, "1.0.0")

    def test_older_version_refused(self):
        publish.prepare(self.public, self.copy, "1.1.0")
        with self.assertRaises(publish.PublishError):
            publish.prepare(self.public, self.copy, "1.0.5")


if __name__ == "__main__":
    unittest.main()
