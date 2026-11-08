#!/usr/bin/python3 -I
"""Put a checked public copy into the public repository as ONE commit.

  publish_public.py PUBLIC_CLONE COPY_DIR VERSION [--push] [--first-base SHA]

--first-base: only for the FIRST release, when the owner has already made
commits in the public repo by hand: publish on top of exactly that commit.

Without --push it only prepares the commit and tag locally (a rehearsal).

Checks before anything is committed:
  * the public repo hasn't moved: its main branch sits exactly on the commit
    its newest v* tag points at, and that tag is the previous release
    (for the very first release: no tags yet and a single initial commit);
  * if vVERSION already exists there with the same content, there is nothing
    to do (a re-run after a half-done publish finishes cleanly).

The commit is "pve-watchdog VERSION" by a bot identity (never the owner's
email) on top of the previous version; the whole tree is replaced, so files
deleted since the last release are deleted publicly too. Branch and tag are
pushed together (--atomic).
"""
import os
import re
import shutil
import subprocess
import sys

BOT = {"GIT_AUTHOR_NAME": "pve-watchdog release", "GIT_COMMITTER_NAME": "pve-watchdog release",
       "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
       "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com"}
NAME = "pve-watchdog"


class PublishError(Exception):
    pass


def git(repo, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, env=e)
    if p.returncode != 0:
        raise PublishError("git %s failed: %s" % (" ".join(args[:2]), p.stderr.strip()[-300:]))
    return p.stdout.strip()


def vkey(tag):
    return tuple(int(x) for x in tag[1:].split("."))


def release_tags(repo):
    tags = [t for t in git(repo, "tag", "--list", "v*").split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return sorted(tags, key=vkey)


def tree_of(repo, ref):
    return git(repo, "rev-parse", ref + "^{tree}")


def check_unmoved(repo, version, first_base=None):
    tags = release_tags(repo)
    head = git(repo, "rev-parse", "HEAD")
    if "v" + version in tags:
        return "already-tagged"
    if not tags:
        if first_base:
            # First release on top of commits the owner made by hand: allowed only
            # if the public repo is EXACTLY at the commit he acknowledged.
            if not head.startswith(first_base.lower()) or len(first_base) < 7:
                raise PublishError("the public repo is not at the acknowledged commit %s"
                                   % first_base[:12])
            return "first"
        if git(repo, "rev-list", "--count", "HEAD") != "1":
            raise PublishError("the public repo has no release tag but more than its initial "
                               "commit: someone committed there directly")
        return "first"
    newest = tags[-1]
    if git(repo, "rev-parse", newest + "^{commit}") != head:
        raise PublishError("the public main branch is not on %s: someone committed there "
                           "directly; nothing published" % newest)
    if vkey(newest) >= vkey("v" + version):
        raise PublishError("%s is not newer than the public %s" % (version, newest))
    return newest


def replace_tree(repo, copy):
    for name in os.listdir(repo):
        if name == ".git":
            continue
        p = os.path.join(repo, name)
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)
    for name in os.listdir(copy):
        src, dst = os.path.join(copy, name), os.path.join(repo, name)
        shutil.copytree(src, dst, symlinks=True) if os.path.isdir(src) else shutil.copy2(src, dst)
    git(repo, "add", "-A")


def prepare(repo, copy, version, first_base=None):
    state = check_unmoved(repo, version, first_base)
    tag = "v" + version
    if state == "already-tagged":
        # Finish a half-done publish: the tag must hold exactly this content.
        replace_tree(repo, copy)
        staged = git(repo, "write-tree")
        git(repo, "reset", "-q", "--hard", "HEAD")
        if staged != tree_of(repo, tag):
            raise PublishError("%s already exists in the public repo with different content" % tag)
        return "exists"
    replace_tree(repo, copy)
    msg = "%s %s" % (NAME, version)
    git(repo, "commit", "-q", "--allow-empty", "-m", msg, env=BOT)
    git(repo, "tag", "-a", tag, "-m", msg, env=BOT)
    return "prepared"


def push(repo, version):
    git(repo, "push", "--atomic", "origin", "HEAD:main", "v" + version)


def main(argv):
    try:
        repo, copy, version = argv[1], argv[2], argv[3]
        first_base = argv[argv.index("--first-base") + 1] if "--first-base" in argv else None
        result = prepare(repo, copy, version, first_base)
        if result == "exists":
            print("publish_public: v%s is already published with this content; nothing to do" % version)
            return 0
        if "--push" in argv:
            push(repo, version)
            print("publish_public: pushed %s %s (commit + tag v%s)" % (NAME, version, version))
        else:
            print("publish_public: REHEARSAL. Prepared %s %s locally; nothing pushed." % (NAME, version))
            print(git(repo, "show", "--stat", "--format=%an <%ae>%n%s", "HEAD")[:3000])
    except (PublishError, IndexError) as e:
        print("publish_public: STOPPED, nothing published.\n%s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
