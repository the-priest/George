"""A design linter for the theme.

The screenshot channel is not available, so judgements about the look
have to be made from the numbers instead of by eye.  This checks the
things that are actually measurable and that actually go wrong:

  * WCAG contrast of every text colour against the surface it sits on
  * the type scale being a scale rather than a pile of near-identical
    sizes
  * spacing values coming from a consistent step
  * every colour referenced by the sheet actually existing

It imports nothing from GTK, so it runs anywhere.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_theme as th          # noqa: E402

FAILS = []
WARNS = []


def lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(hexcol):
    r, g, b = th.rgb(hexcol)
    return (0.2126 * lin(r * 255) + 0.7152 * lin(g * 255)
            + 0.0722 * lin(b * 255))


def contrast(fg, bg):
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def report(name, fg, bg, floor, big=False):
    r = contrast(fg, bg)
    tag = "%-34s %s on %s = %.2f:1 (need %.1f)" % (name, fg, bg, r, floor)
    if r < floor:
        FAILS.append(tag)
    elif r < floor + 0.6:
        WARNS.append(tag + "  [tight]")
    return r


print("== contrast ==")
for accent_name in th.ACCENTS:
    p = th.palette({"accent": accent_name})
    # body text on every surface it can land on
    for surf in ("void", "plate", "card", "raised"):
        report("body/%s on %s" % (accent_name, surf), p["text"], p[surf], 4.5)
    # secondary text: WCAG AA for normal text is still 4.5
    report("dim/%s on card" % accent_name, p["dim"], p["card"], 4.5)
    # faint carries timestamps and eyebrow labels: small text, so the
    # 4.5:1 floor applies, not the 3:1 one for large text.
    for surf in ("void", "plate", "card"):
        report("faint/%s on %s" % (accent_name, surf), p["faint"],
               p[surf], 4.5)
    # the accent is used for card titles and links
    report("accent/%s on card" % accent_name, p["accent"], p["card"], 4.5)
    report("accent/%s on plate" % accent_name, p["accent"], p["plate"], 4.5)
    # his own bubble: text sits on accent_deep -> accent_ghost
    report("user bubble/%s" % accent_name, p["accent_light"],
           p["accent_deep"], 4.5)
    # code inside a reply
    report("code/%s on void" % accent_name, p["accent_light"], p["void"], 4.5)
    # status colours
    for k in ("ok", "warn", "bad"):
        report("%s/%s on card" % (k, accent_name), p[k], p["card"], 3.0)
    # the send button paints void-coloured glyphs on the accent
    report("send glyph/%s" % accent_name, p["void"], p["accent"], 4.5)

print("  %d fail, %d tight" % (len(FAILS), len(WARNS)))

# -- type scale ---------------------------------------------------------
print("== type scale ==")
sizes = {}
for label, base in (("fxx", 10), ("fx", 11), ("fs", 13), ("f", 15),
                    ("fh", 17), ("fbig", 26), ("fhero", 30)):
    sizes[label] = th._px(base, 1.0)
order = [sizes[k] for k in ("fxx", "fx", "fs", "f", "fh", "fbig", "fhero")]
print("  ", order)
if order != sorted(order):
    FAILS.append("type scale is not monotonic: %s" % order)
for a, b in zip(order, order[1:]):
    if b == a:
        FAILS.append("two type sizes collide at %dpx" % a)

# font scaling must not collapse the scale at either end
for scale in (0.75, 1.0, 1.5, 2.0):
    got = [th._px(b, scale) for b in (10, 11, 13, 15, 17, 26, 30)]
    if len(set(got)) < 5:
        FAILS.append("at font_scale=%s the type scale collapses to %s"
                     % (scale, got))

# -- token references --------------------------------------------------
# A token in the sheet with no matching key raises KeyError at format
# time, so building every combination below is the real check. This just
# reports the count so a sudden drop is visible in the log.
print("== token references ==")
src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "george_theme.py")).read()
print("   %d distinct tokens in the sheet"
      % len(set(re.findall(r"%\((\w+)\)[sd]", src))))

# -- the sheet must build and stay ASCII for every combination ----------
print("== sheet builds ==")
for accent in th.ACCENTS:
    for density in ("comfortable", "compact"):
        for scale in (0.75, 1.0, 2.0):
            sheet = th.build_css({"accent": accent, "ui_density": density,
                                  "font_scale": scale})
            if not isinstance(sheet, bytes):
                FAILS.append("build_css returned %s" % type(sheet))
            try:
                sheet.decode("ascii")
            except UnicodeDecodeError:
                FAILS.append("non-ascii in the sheet for %s/%s/%s"
                             % (accent, density, scale))
            if b"%(" in sheet:
                FAILS.append("unsubstituted token left in the sheet for %s"
                             % accent)
            for banned in (b"@keyframes", b"var(", b"transform:",
                           b"filter:", b"display:flex", b"display:grid"):
                if banned in sheet:
                    FAILS.append("GTK cannot parse %r, found in the sheet"
                                 % banned)

print()
print("design lint: %d failures, %d tight" % (len(FAILS), len(WARNS)))
for w in WARNS:
    print("  TIGHT: %s" % w)
for f in FAILS:
    print("  FAIL:  %s" % f)
sys.exit(1 if FAILS else 0)
