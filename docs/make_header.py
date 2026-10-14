"""Generate the README header SVG. Self-hosted on purpose.

capsule-render and friends time out through GitHub's camo proxy often enough to leave a broken
image on the front page, so the header is generated here and committed. The card on the right is
the real validation result from docs/validation.json. Regenerate with:  python docs/make_header.py

Validates as XML before writing: a bare '&' in SVG text is invalid XML and GitHub renders it as
"Invalid image source" with no other warning.
"""
import json
import pathlib
import xml.dom.minidom

HERE = pathlib.Path(__file__).parent
W, H = 1200, 340
BG = "#0A0A14"
AMBER, CORAL, PINK = "#FFB020", "#FF6B4A", "#FF4E88"
LIME, BLUE, DIM, TEXT = "#A3E635", "#5B9BFF", "#8B8BA7", "#E8E8F0"
MONO = "ui-monospace,SFMono-Regular,Menlo,monospace"
SANS = "-apple-system,Segoe UI,Helvetica,sans-serif"
BAR = 368


def bar(x0, y0, parts, total):
    """One stacked bar: (count, colour) parts, scaled to `total` repos."""
    out, x = [], x0
    for count, colour in parts:
        if not count:
            continue
        w = BAR * count / total
        out.append(f'<rect x="{x:.1f}" y="{y0}" width="{w - 2:.1f}" height="14" rx="3" fill="{colour}"/>')
        x += w
    return out


def card(x0, y0, sets):
    s, u = sets["suspected"], sets["unsuspected"]
    sv, uv = s["verdicts"], u["verdicts"]
    lines = [
        f'<rect x="{x0}" y="{y0}" width="404" height="226" rx="10" fill="#12121F" stroke="{DIM}" stroke-opacity="0.35"/>',
        f'<text x="{x0 + 18}" y="{y0 + 30}" fill="{TEXT}">{s["repos"] + u["repos"]} random repos from a fake-star study</text>',
        f'<text x="{x0 + 18}" y="{y0 + 62}" fill="{CORAL}" font-weight="700">{s["repos"]} the study suspected</text>',
        *bar(x0 + 18, y0 + 72, [(sv["farm"], CORAL), (sv["recent"] + sv["mixed"], AMBER), (sv["clean"], LIME),
                                (sv["unknown"], DIM)], s["repos"]),
        f'<text x="{x0 + 18}" y="{y0 + 106}" fill="{TEXT}">{sv["farm"]} farm signature &#183; {sv["unknown"]} too '
        f'little data &#183; {sv["clean"]} clean</text>',
        f'<text x="{x0 + 18}" y="{y0 + 140}" fill="{LIME}" font-weight="700">{u["repos"]} it did not suspect</text>',
        *bar(x0 + 18, y0 + 150, [(uv["clean"], LIME), (uv["recent"] + uv["mixed"], AMBER), (uv["farm"], CORAL),
                                 (uv["unknown"], DIM)], u["repos"]),
        f'<text x="{x0 + 18}" y="{y0 + 184}" fill="{TEXT}">{uv["clean"]} clean &#183; {uv["recent"] + uv["mixed"]} '
        f'recent or mixed &#183; {uv["farm"]} farm</text>',
        f'<text x="{x0 + 18}" y="{y0 + 210}" fill="{DIM}">checked {sets["checked"]} &#183; '
        f'time-matched control</text>',
    ]
    return "\n    ".join(lines)


def pills(x0, y0, items):
    out, x = [], x0
    for text, colour in items:
        w = 26 + 8.1 * len(text)
        out.append(f'<rect x="{x:.0f}" y="{y0}" width="{w:.0f}" height="28" rx="14" fill="none" '
                   f'stroke="{colour}" stroke-opacity="0.55"/>')
        out.append(f'<text x="{x + w / 2:.0f}" y="{y0 + 19}" fill="{colour}" text-anchor="middle">{text}</text>')
        x += w + 12
    return "\n    ".join(out)


def build():
    result = json.loads((HERE / "validation.json").read_text(encoding="utf-8"))
    sets = {**result["sets"], "checked": result["checked"]}
    sv, uv = sets["suspected"]["verdicts"], sets["unsuspected"]["verdicts"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"
     viewBox="0 0 {W} {H}" role="img"
     aria-label="starsift: check whether the accounts behind a GitHub repo's stars look real. On repos a
     fake-star study suspected, it found a farm signature on {sv['farm']} of {sets['suspected']['repos']};
     on repos it did not suspect, {uv['farm']} of {sets['unsuspected']['repos']}.">
  <defs>
    <linearGradient id="grad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{AMBER}"/>
      <stop offset="55%" stop-color="{CORAL}"/>
      <stop offset="100%" stop-color="{PINK}"/>
    </linearGradient>
    <linearGradient id="fade" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{CORAL}" stop-opacity="0.22"/>
      <stop offset="100%" stop-color="{BG}" stop-opacity="0"/>
    </linearGradient>
    <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
      <path d="M28 0 L0 0 0 28" fill="none" stroke="#FFFFFF" stroke-opacity="0.04" stroke-width="1"/>
    </pattern>
  </defs>

  <rect width="{W}" height="{H}" fill="{BG}"/>
  <rect width="{W}" height="{H}" fill="url(#grid)"/>
  <circle cx="{W - 160}" cy="70" r="240" fill="url(#fade)"/>

  <text x="64" y="86" font-family="{MONO}" font-size="13" letter-spacing="3.5" fill="{LIME}">GITHUB STARS
        &#183; FAKE ACCOUNT CHECK</text>

  <text x="60" y="164" font-family="Georgia,'Times New Roman',serif" font-size="68" font-weight="700"
        fill="url(#grad)">starsift</text>

  <text x="64" y="204" font-family="{SANS}" font-size="19" fill="{TEXT}">Check whether the accounts behind
        a repo's stars look real.</text>
  <text x="64" y="232" font-family="{SANS}" font-size="19" fill="{DIM}">Works now that GitHub hides the
        stargazer list.</text>

  <g font-family="{MONO}" font-size="12">
    {pills(64, 262, [("ZERO DEPENDENCIES", AMBER), ("TIME-MATCHED CONTROL", LIME), ("GH ARCHIVE", BLUE),
                     ("MIT", PINK)])}
  </g>

  <g font-family="{MONO}" font-size="12.5">
    {card(750, 57, sets)}
  </g>
</svg>
"""


if __name__ == "__main__":
    svg = build()
    out = HERE / "header.svg"
    xml.dom.minidom.parseString(svg)          # fails loudly on a bare '&'
    out.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(svg)} bytes, valid XML)")
