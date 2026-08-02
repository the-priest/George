"""Inline markdown -> Pango, and the interleaving that silently ate
formatting.

md_to_pango ran its passes independently, so the tags they produced
could OVERLAP instead of nesting:

    `*`*    ->  <tt><span><i></span></tt></i>
    ******  ->  <b><i></b></i>

Pango rejects overlapping tags, safe_markup falls back to plain text on
a rejection, and the fallback is for the WHOLE label -- so one confusing
fragment stripped the bold, the links and the code styling out of the
entire reply. Roughly 1 in 8 fuzzed inputs hit it.

Code spans and links are now stashed behind placeholders before the
emphasis passes run, so overlap is impossible by construction.
"""
import os
import random
import sys
import tempfile

_t = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = _t + "/cfg"
os.environ["XDG_DATA_HOME"] = _t + "/data"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi                          # noqa: E402
gi.require_version("Gtk", "4.0")
import george_hud as hud           # noqa: E402

CODE, DIM = "#9fe9ff", "#72859a"
FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def render(s):
    return hud.md_to_pango(s, CODE, DIM)


# -- the two minimal cases that used to break --------------------------
for pathological in ("`*`*", "*`*`", "******", "***", "**`**`**"):
    check(hud.validate_markup(render(pathological)),
          "overlapping tags from %r: %s" % (pathological,
                                            render(pathological)))

# -- realistic prose has to keep its formatting ------------------------
cases = {
    "*emphasis with `code` inside*":
        "<i>emphasis with <tt><span foreground='%s'>code</span></tt> "
        "inside</i>" % CODE,
    "**see [the wiki](https://x) now**":
        "<b>see <a href='https://x'>the wiki</a> now</b>",
    "*italic [a](https://x) end*":
        "<i>italic <a href='https://x'>a</a> end</i>",
    # a code span is LITERAL: the asterisks show, b is not italicised
    "`a *b* c`":
        "<tt><span foreground='%s'>a *b* c</span></tt>" % CODE,
    # a stashed code span inside a stashed link needs more than one
    # restore pass
    "[`code link`](https://z)":
        "<a href='https://z'><tt><span foreground='%s'>code link"
        "</span></tt></a>" % CODE,
}
for src, want in cases.items():
    got = render(src)
    check(got == want, "%r\n    got  %s\n    want %s" % (src, got, want))
    check(hud.validate_markup(got), "%r produced invalid markup" % src)

# -- escaping still happens --------------------------------------------
check("&lt;" in render("a < b"), "< was not escaped")
check("&amp;" in render("a & b"), "& was not escaped")
check("<b>" not in render("<b>not a tag</b>"), "raw HTML got through")

# -- and the fuzz that found it ----------------------------------------
random.seed(5)
alphabet = "abc *_`#[]()<>&\"'\n-1. \\|~"
rejected = 0
for _ in range(12000):
    s = "".join(random.choice(alphabet)
                for _ in range(random.randint(0, 140)))
    if not hud.validate_markup(render(s)):
        rejected += 1
check(rejected == 0,
      "%d of 12000 fuzzed inputs still produce markup Pango rejects"
      % rejected)

print("markup checks; failures: %d  (fuzz rejections: %d/12000)"
      % (len(FAILS), rejected))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
