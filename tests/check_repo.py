#!/usr/bin/python3 -I
"""Fail if a published file contains a personal-looking value.

Checks every git-tracked file (or every file under a directory), except
anything in private/:

  * email addresses: only placeholders (example.com and its subdomains,
    .invalid) and a few service addresses are allowed;
  * IPv4/IPv6 addresses: only the example network 192.168.100.0/24, loopback,
    0.0.0.0 / 255.255.255.255, the documentation ranges and a few well-known
    public resolvers;
  * MAC addresses: only the documentation range 00:00:5E:00:53:xx.

Everything else fails. This is an allow-list on purpose: it catches real
values without them ever being written down here (this file is public).
Messages name the file, line and KIND of value, never the value itself.

usage: check_repo.py [DIR]      (default: the git checkout this file is in)
"""
import ipaddress
import os
import re
import subprocess
import sys

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.]*\d)")
IPV6 = re.compile(r"(?<![\w:.])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:])")
MAC = re.compile(r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}(?![0-9A-Fa-f:-])")

ALLOWED_EMAILS = {"git@github.com", "noreply@github.com",
                  "41898282+github-actions[bot]@users.noreply.github.com"}
EXAMPLE_NET = ipaddress.ip_network("192.168.100.0/24")
ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]
ALLOWED_IPS = {ipaddress.ip_address(a) for a in (
    "0.0.0.0", "255.255.255.255", "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9",
    "::", "::1")}


def email_ok(addr):
    a = addr.lower()
    domain = a.split("@", 1)[1]
    return (a in ALLOWED_EMAILS or domain == "example.com" or domain.endswith(".example.com")
            or domain.endswith(".invalid") or domain in ("example.org", "example.net"))


def ip_ok(ip):
    if ip.version == 4 and ip in EXAMPLE_NET:
        return True
    return ip in ALLOWED_IPS or any(ip in n for n in ALLOWED_NETS if n.version == ip.version)


def scan_text(text):
    """Yield (line_number, kind) for every disallowed value."""
    for n, line in enumerate(text.splitlines(), 1):
        for m in EMAIL.finditer(line):
            if not email_ok(m.group(0)):
                yield n, "email address"
        for m in IPV4.finditer(line):
            try:
                ip = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            if not ip_ok(ip):
                yield n, "IP address"
        for m in IPV6.finditer(line):
            try:
                ip = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            if not ip_ok(ip):
                yield n, "IPv6 address"
        for m in MAC.finditer(line):
            if not m.group(0).upper().replace("-", ":").startswith("00:00:5E:00:53:"):
                yield n, "MAC address"


def files_in(root):
    if os.path.isdir(os.path.join(root, ".git")) or os.path.isfile(os.path.join(root, ".git")):
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True,
                             check=True).stdout.decode()
        return [f for f in out.split("\0") if f]
    found = []
    for d, _dirs, names in os.walk(root):
        for name in names:
            found.append(os.path.relpath(os.path.join(d, name), root))
    return found


def check(root):
    problems = []
    for rel in sorted(files_in(root)):
        if rel == "private" or rel.startswith("private/") or rel.startswith(".git/"):
            continue
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            problems.append("%s: not UTF-8 text" % rel)
            continue
        except OSError:
            continue
        for line, kind in scan_text(text):
            problems.append("%s:%d: %s not allowed in public files" % (rel, line, kind))
        for _line, kind in scan_text(rel):                # file names too
            problems.append("%s: %s in the file name" % (rel, kind))
    return problems


def main(argv):
    root = argv[1] if len(argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    problems = check(root)
    for p in problems:
        print(p)
    print("check_repo: %s" % ("%d problem(s)" % len(problems) if problems else "OK"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
