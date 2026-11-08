#!/bin/bash
# pve-watchdog one-command installer. Run on each Proxmox node, as root:
#
#   bash -c "$(curl -fsSL https://github.com/rob-paprocki/pve-watchdog/releases/latest/download/get.sh)"
#
# It downloads this release, checks it against the release's SHA256SUMS,
# unpacks it to /opt/pve-watchdog/<version> and starts the setup wizard.
set -euo pipefail

VERSION="@VERSION@"                       # filled in when the release is built
REPO="${PVEWD_REPO:-rob-paprocki/pve-watchdog}"
BASE="https://github.com/$REPO/releases/download/v$VERSION"
TARBALL="pve-watchdog-$VERSION.tar.gz"
DEST="/opt/pve-watchdog/$VERSION"

die() { echo "pve-watchdog: $*" >&2; exit 1; }

[[ "$VERSION" != "@VERSION@" ]] || die "this get.sh is not from a release; download it from a release page"
[[ $EUID -eq 0 ]] || die "please run as root"
command -v pveversion >/dev/null || die "this is not a Proxmox VE host"
command -v python3 >/dev/null || die "python3 is missing"
for c in curl sha256sum tar; do command -v "$c" >/dev/null || die "$c is missing"; done

echo "pve-watchdog $VERSION: downloading from github.com/$REPO"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL -o "$TMP/$TARBALL" "$BASE/$TARBALL"
curl -fsSL -o "$TMP/SHA256SUMS" "$BASE/SHA256SUMS"
( cd "$TMP" && grep " $TARBALL\$" SHA256SUMS | sha256sum -c --quiet - ) \
    || die "checksum mismatch: the download is damaged or not from this release"
echo "checksum OK"

mkdir -p "$DEST"
tar -xzf "$TMP/$TARBALL" -C "$DEST" --strip-components=1
ln -sfn "$DEST" /opt/pve-watchdog/current
echo "unpacked to $DEST"

exec python3 -I "$DEST/tools/setup_wizard.py" </dev/tty
