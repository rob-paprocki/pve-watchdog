#!/usr/bin/env bash
# Regenerate the README diagrams in docs/images from their Mermaid sources.
#
#   docs/images/render.sh            # every *.mmd in docs/images
#   docs/images/render.sh NAME...    # only NAME.mmd
#
# For each NAME.mmd it writes NAME-light.svg, NAME-dark.svg and NAME.svg (the
# one the README shows, which switches palette with the reader's theme).
#
# The tool versions are pinned in package.json and package-lock.json next to
# this script: @mermaid-js/mermaid-cli 12.2.0, mermaid 12.3.0, puppeteer
# 25.13.0. They are installed with `npm ci` into a temporary folder that is
# deleted afterwards, so nothing is added to the repository.
#
# Needs Node.js 22.13 or later, npm and python3. Puppeteer downloads its own
# Chrome on the first run (cached in ~/.cache/puppeteer). To use a Chrome or
# Chromium you already have instead, set PUPPETEER_EXECUTABLE_PATH to it.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cp "$here/package.json" "$here/package-lock.json" "$work/"
if [ -n "${PUPPETEER_EXECUTABLE_PATH:-}" ]; then
  export PUPPETEER_SKIP_DOWNLOAD=1
fi
(cd "$work" && npm ci --no-audit --no-fund --loglevel=error)

# Chrome refuses to start as root unless its sandbox is off. The input is
# this repository's own diagram source, so that is acceptable here.
args='[]'
[ "$(id -u)" -eq 0 ] && args='["--no-sandbox"]'
if [ -n "${PUPPETEER_EXECUTABLE_PATH:-}" ]; then
  printf '{"executablePath":"%s","args":%s}\n' "$PUPPETEER_EXECUTABLE_PATH" "$args" >"$work/puppeteer.json"
else
  printf '{"args":%s}\n' "$args" >"$work/puppeteer.json"
fi

if [ "$#" -eq 0 ]; then
  set --
  for src in "$here"/*.mmd; do
    set -- "$@" "$(basename "$src" .mmd)"
  done
fi

for name in "$@"; do
  for theme in light dark; do
    "$work/node_modules/.bin/mmdc" --quiet \
      --input "$here/$name.mmd" \
      --configFile "$here/mermaid-$theme.json" \
      --puppeteerConfigFile "$work/puppeteer.json" \
      --backgroundColor transparent \
      --svgId "$name" \
      --output "$work/$name-$theme.svg"
  done
  python3 -I "$here/postprocess.py" "$work/$name-light.svg" "$work/$name-dark.svg" "$here/$name"
done
