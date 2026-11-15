#!/usr/bin/env python3
"""Turn a light and a dark mermaid-cli render of one diagram into the files
the README uses. Run by render.sh; its opening comment says how.

usage: python3 -I postprocess.py LIGHT_RAW.svg DARK_RAW.svg OUT_PREFIX

Writes OUT_PREFIX-light.svg, OUT_PREFIX-dark.svg and OUT_PREFIX.svg:

- Each file gets a fixed width and height (its viewBox size), so a Markdown
  image shows it at its natural size on a wide screen and only scales it
  down on a narrow one. Mermaid writes width="100%", which would stretch it.
- Each file gets an opaque rounded panel behind the diagram, in its theme's
  page colour. If a viewer picks the other theme's palette, the reader sees
  a readable card rather than light lines on a dark page.
- OUT_PREFIX.svg is the light render plus the dark render's differing CSS
  rules inside @media (prefers-color-scheme: dark), so one file follows the
  reader's theme. It refuses to build if the two renders differ anywhere
  outside <style>, because then one stylesheet cannot switch between them.
"""
import math
import re
import sys

PANEL = {
    # fill, border: GitHub's page background and default border colours.
    'light': ('#ffffff', '#d1d9e0'),
    'dark': ('#0d1117', '#3d444d'),
}


def split_rules(css):
    """Split a flat stylesheet into top-level rules, keeping @-blocks whole."""
    rules, depth, start = [], 0, 0
    for i, ch in enumerate(css):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                rules.append(css[start:i + 1].strip())
                start = i + 1
    return rules


def fix_root(svg, theme):
    tag = re.search(r'<svg\b[^>]*>', svg).group(0)
    svg_id = re.search(r'\sid="([^"]+)"', tag).group(1)
    x, y, w, h = (float(v) for v in re.search(r'viewBox="([^"]+)"', tag).group(1).split())
    new = re.sub(r'\s(width|style)="[^"]*"', '', tag)
    new = new.replace('<svg ', f'<svg width="{math.ceil(w)}" height="{math.ceil(h)}" ', 1)
    svg = svg.replace(tag, new, 1)
    # Gradient defs that only the "neo" look uses.
    svg = re.sub(r'<linearGradient id="[^"]*-gradient".*?</linearGradient>', '', svg, flags=re.S)
    fill, stroke = PANEL[theme]
    css = f'#{svg_id} .readme-panel{{fill:{fill};stroke:{stroke};stroke-width:1px;}}'
    svg = svg.replace('</style>', css + '</style>', 1)
    panel = (f'<rect class="readme-panel" x="{x + 0.5:g}" y="{y + 0.5:g}" '
             f'width="{w - 1:g}" height="{h - 1:g}" rx="8" ry="8"/>')
    # Straight after the opening <svg> tag, so the panel is drawn first: sequence
    # diagrams put their participants and lifelines before <style>.
    return svg.replace(new, new + panel, 1)


def style_of(svg):
    return re.search(r'<style>(.*?)</style>', svg, re.S).group(1)


def main():
    light_in, dark_in, prefix = sys.argv[1:4]
    with open(light_in, encoding='utf-8') as f:
        light = fix_root(f.read(), 'light')
    with open(dark_in, encoding='utf-8') as f:
        dark = fix_root(f.read(), 'dark')

    def strip(s):
        return re.sub(r'<style>.*?</style>', '', s, flags=re.S)

    if strip(light) != strip(dark):
        sys.exit(f'{prefix}: light and dark renders differ outside <style>; '
                 'cannot build a single theme-aware SVG')

    lrules = set(split_rules(style_of(light)))
    drules = [r for r in split_rules(style_of(dark)) if r not in lrules]
    media = '@media (prefers-color-scheme: dark){' + ''.join(drules) + '}'
    auto = light.replace('</style>', media + '</style>', 1)

    for path, text in ((prefix + '-light.svg', light), (prefix + '-dark.svg', dark),
                       (prefix + '.svg', auto)):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    print(f'{prefix}.svg: {len(drules)} dark rules')


if __name__ == '__main__':
    main()
