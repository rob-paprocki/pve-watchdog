#!/usr/bin/env python3
"""Draw the README / docs diagrams as plain SVG, in a light and a dark version.

  python3 scripts/diagrams/make_diagrams.py        # writes docs/images/diagram-*.svg

GitHub's mobile app doesn't draw Mermaid, but it shows images, so the diagrams
are images. They are text (SVG), so the public-copy word-list scan reads them
like any other file. The README picks the version with <picture>, following the
visitor's light/dark setting, like the screenshots.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(os.path.dirname(HERE)), "docs", "images")

THEMES = {
    "light": {"text": "#1f2328", "muted": "#59636e", "box": "#f6f8fa", "line": "#d0d7de",
              "panel": "#ffffff", "arrow": "#0969da", "ok": "#1a7f37", "okbg": "#dafbe1",
              "bad": "#cf222e", "badbg": "#ffebe9", "ask": "#9a6700", "askbg": "#fff8c5"},
    "dark": {"text": "#e6edf3", "muted": "#9198a1", "box": "#161b22", "line": "#3d444d",
             "panel": "#0d1117", "arrow": "#4493f8", "ok": "#3fb950", "okbg": "#12261e",
             "bad": "#f85149", "badbg": "#2d1214", "ask": "#d29922", "askbg": "#272115"},
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


class Svg:
    def __init__(self, w, h, t):
        self.w, self.h, self.t, self.parts = w, h, t, []

    def text(self, x, y, lines, size=13, color="text", weight="normal", anchor="middle"):
        lines = lines.split("\n")
        y0 = y - (len(lines) - 1) * size * 0.6
        for i, line in enumerate(lines):
            self.parts.append('<text x="%g" y="%g" font-size="%d" font-weight="%s" fill="%s" '
                              'text-anchor="%s" dominant-baseline="middle">%s</text>'
                              % (x, y0 + i * size * 1.2, size, weight, self.t[color], anchor, line))

    def box(self, x, y, w, h, label, kind="box", size=13, weight="normal"):
        fill, stroke, color = {
            "box": ("box", "line", "text"), "ok": ("okbg", "ok", "text"), "bad": ("badbg", "bad", "text"),
            "ask": ("askbg", "ask", "text"), "quiet": ("box", "line", "muted"),
        }[kind]
        self.parts.append('<rect x="%g" y="%g" width="%g" height="%g" rx="8" fill="%s" stroke="%s" '
                          'stroke-width="1.5"/>' % (x, y, w, h, self.t[fill], self.t[stroke]))
        self.text(x + w / 2, y + h / 2, label, size, color, weight)

    def panel(self, x, y, w, h, title):
        self.parts.append('<rect x="%g" y="%g" width="%g" height="%g" rx="12" fill="%s" stroke="%s" '
                          'stroke-width="1.5" stroke-dasharray="6 4"/>'
                          % (x, y, w, h, self.t["panel"], self.t["line"]))
        self.text(x + w / 2, y + 20, title, 15, "text", "600")

    def arrow(self, pts, label=None, at=None, dashed=False, both=False, color="arrow", anchor="middle"):
        d = "M" + " L".join("%g %g" % p for p in pts)
        self.parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.8"%s marker-end="url(#a-%s)"%s/>'
                          % (d, self.t[color], ' stroke-dasharray="6 4"' if dashed else "", color,
                             ' marker-start="url(#s-%s)"' % color if both else ""))
        if label:
            self.text(at[0], at[1], label, 12, "muted", anchor=anchor)

    def render(self, title):
        defs = "".join(
            '<marker id="a-{c}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{f}"/></marker>'
            '<marker id="s-{c}" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto"><path d="M10 0 L0 5 L10 10 z" fill="{f}"/></marker>'.format(c=c, f=self.t[c])
            for c in ("arrow", "muted", "ok", "bad"))
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
                'font-family="{font}" role="img" aria-label="{title}">\n<title>{title}</title>\n'
                '<defs>{defs}</defs>\n{body}\n</svg>\n').format(
                    w=self.w, h=self.h, font=FONT, title=title, defs=defs, body="\n".join(self.parts))


def architecture(t):
    s = Svg(560, 330, t)
    for x, node, wd, ct, kind in ((10, "pve1", "Watchdog\nCT 9100", "CT 110 pihole\nrunning", "ok"),
                                  (350, "pve2", "Watchdog\nCT 9101", "CT 1110 pihole\nstandby, stopped", "quiet")):
        s.panel(x, 10, 200, 310, node)
        s.box(x + 25, 45, 150, 54, wd, weight="600")
        s.box(x + 25, 145, 150, 44, "Gatekeeper")
        s.box(x + 25, 240, 150, 60, ct, kind)
        s.arrow([(x + 100, 99), (x + 100, 143)], "SSH", (x + 122, 121))
        s.arrow([(x + 100, 189), (x + 100, 238)])
    s.arrow([(187, 72), (373, 72)], "checks\nevery 10 s", (280, 50), both=True)
    s.arrow([(187, 270), (373, 270)], "copy\nevery 30 min", (280, 248), dashed=True, color="ok")
    return s.render("What runs where: a watchdog and a gatekeeper on each node, the container on pve1, "
                    "its stopped standby on pve2")


def timeline(t):
    left, right = 140, 420
    steps = [
        ("note", left, "Power cut", "bad"),
        ("msg", "ping, web port, cluster, watchdog", False, "muted"),
        ("note", right, "No answer for 6 rounds", "box"),
        ("msg", "Wake-on-LAN", True, "arrow"),
        ("note", right, "Wait 90 s, then ping and ARP\nCT 110's own IP", "box"),
        ("note", right, "Nothing answers: start CT 1110\n(about 3 min in)", "ok"),
        ("note", left, "Power back,\nhealthy for 10 min", "box"),
        ("note", right, "Stop CT 1110,\ntake a final backup", "box"),
        ("msg", "copy the backup into CT 110", False, "arrow"),
        ("note", left, "Start CT 110", "ok"),
        ("note", right, "CT 1110 is the standby again", "quiet"),
    ]
    heights = [52 if (s[0] == "note" and "\n" in s[2]) else 40 for s in steps]
    total = 70 + sum(h + 14 for h in heights) + 50
    s = Svg(560, total, t)
    for x, name in ((left, "pve1"), (right, "pve2")):
        s.parts.append('<line x1="%d" y1="50" x2="%d" y2="%d" stroke="%s" stroke-width="1.5" '
                       'stroke-dasharray="4 4"/>' % (x, x, total - 46, t["line"]))
        s.box(x - 60, 10, 120, 36, name, weight="600")
        s.box(x - 60, total - 46, 120, 36, name, weight="600")
    y = 66
    for step, h in zip(steps, heights):
        if step[0] == "note":
            _, x, label, kind = step
            s.box(x - 125, y, 250, h, label, kind, size=12)
        else:
            _, label, dashed, color = step
            s.arrow([(right - 4, y + h - 10), (left + 4, y + h - 10)], label, ((left + right) / 2, y + h - 24),
                    dashed=dashed, color=color)
        y += h + 14
    return s.render("A power cut on pve1: pve2 notices, wakes it, waits, starts the standby, "
                    "and hands the container back with its data when pve1 is healthy")


def decisions(t):
    s = Svg(660, 640, t)
    cx, w = 195, 290
    left = cx - w / 2
    rx, rw = 405, 245
    rows = [
        (20, "All four checks fail\nfor another node", "box", None),
        (100, "6 rounds in a row?", "ask", None),
        (170, "Wait 90 s\n(Wake-on-LAN goes out meanwhile)", "box", None),
        (250, "Node back?", "ask", ("yes", "Nothing to do", "quiet")),
        (320, "Gateway reachable?", "ask", ("no", "This node is the one cut off:\ndo nothing", "quiet")),
        (390, "Container's own IP answers\nping or ARP?", "ask", ("yes", "Abort and alert", "bad")),
        (470, "Quorum on its own?", "ask", ("no", "pvecm expected 1\n(force mode, loud alert)", "bad")),
        (570, "Check the IP once more,\nstart the standby", "ok", None),
    ]
    prev = None
    for i, (y, label, kind, side) in enumerate(rows):
        h = 50 if "\n" in label else 40
        s.box(left, y, w, h, label, kind, weight="600" if kind == "ask" else "normal")
        if prev is not None:
            py, ph, _ = prev
            # the answer that leads straight down the main column
            nxt = {2: "yes", 4: "no", 5: "yes", 6: "no", 7: "yes"}.get(i)
            s.arrow([(cx, py + ph), (cx, y - 2)], nxt, (cx + 22, (py + ph + y) / 2))
        if side:
            word, text, skind = side
            sh = 50 if "\n" in text else 40
            sy = y + h / 2 - sh / 2
            s.box(rx, sy, rw, sh, text, skind, size=12)
            s.arrow([(left + w, y + h / 2), (rx - 2, y + h / 2)], word, ((left + w + rx) / 2, y + h / 2 - 12))
        if i == 1:      # "6 rounds in a row?" -> no: keep checking
            s.arrow([(left, y + h / 2), (left - 18, y + h / 2), (left - 18, 45), (left - 2, 45)],
                    "no", (left - 4, y - 14), anchor="end")
        prev = (y, h, kind)
    # force mode, then start the standby anyway
    s.arrow([(rx + rw / 2, 470 + 25 + 25), (rx + rw / 2, 595), (left + w + 2, 595)])
    return s.render("How a watchdog decides to take over: every check that has to pass before "
                    "it starts a standby, and where it stops instead")


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, fn in (("architecture", architecture), ("timeline", timeline), ("decisions", decisions)):
        for theme, t in THEMES.items():
            path = os.path.join(OUT, "diagram-%s-%s.svg" % (name, theme))
            with open(path, "w") as f:
                f.write(fn(t))
            print("wrote", os.path.relpath(path))


if __name__ == "__main__":
    main()
