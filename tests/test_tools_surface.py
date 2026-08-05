"""Every tool, called three ways.

THE GAP THIS CLOSES: 27 of 42 tool functions were never named in any
test. The agent loop around them was well covered; the tools themselves
were not. Every bug he reported by screenshot -- the false "news is on
your screen", the dead vision pull, "I can't run code" -- lived in a
tool, and every one of them passed the whole suite.

So: call EVERY registered tool with valid args, with no args, and with
junk args, and assert the contract that the loop depends on:

  * it returns a string, always
  * it never raises, whatever it is handed
  * it does not claim an effect it did not have
  * it does not perform a side effect without asking

Network and desktop calls are stubbed, so this runs anywhere and is
about the tools' own logic rather than the machine's mood.
"""
import os
import sys
import tempfile

_t = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = _t + "/cfg"
os.environ["XDG_DATA_HOME"] = _t + "/data"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_core as gc          # noqa: E402
import george_tools as gt         # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------------
# Stubs. Nothing here may touch the network, the screen or the session.
# ---------------------------------------------------------------------
CALLS = {"confirm": 0, "shell": [], "opened": [], "spoke": []}

gc.http_get = lambda url, timeout=15: (
    '<rss><channel><item><title>T</title><link>http://x.invalid</link>'
    '<description>d</description></item></channel></rss>')
gt.http_get = gc.http_get
gt.wiki_search = lambda t, n=5: []
gt.web_search = lambda q, n=6: [{"title": "R", "url": "http://x.invalid",
                                 "snippet": "s"}]
gt.html_to_text = lambda b: "page text"
# Deliberately PARTIAL: a tool must not KeyError on a third-party shape
# that lost a field. tool_weather used to index this dict directly.
gt.weather = lambda loc: {"place": "Dublin", "temp_c": 14, "desc": "rain"}
gt.open_in_browser = lambda u, cfg: CALLS["opened"].append(u) or \
    "opened %s on screen" % u
gt.run_shell = lambda cmd, timeout=90: (
    CALLS["shell"].append(cmd) or (0, "output"))
# Real signature is (ok, path) -- getting this wrong here is exactly the
# kind of drift this file exists to catch.
gt.take_screenshot = lambda: (True, os.path.join(_t, "shot.png"))
gt.clipboard_read = lambda: "clip"
gt.clipboard_write = lambda t: True
gt.media_control = lambda a: "media %s" % a
gt.volume_control = lambda a, l=5: "volume %s" % a
gt.list_processes = lambda sort, n=10: "proc list"
gt.network_report = lambda: "net report"
gt.disk_report = lambda: "/ 50% used"
gt.launch_app = lambda a: "launched %s" % a
gt.power_action = lambda a: "power %s" % a
open(os.path.join(_t, "shot.png"), "wb").write(b"x")


class FakeTTS:
    def speak(self, text):
        CALLS["spoke"].append(text)

    def stop(self):
        pass


class FakeAgent:
    """Stands in for Agent. Must implement everything the tools touch --
    see the AST check below, which fails if a tool starts calling
    something this does not have."""

    def __init__(self, allow=True):
        self.cfg = dict(gc.DEFAULTS)
        self.cfg["sandbox_root"] = _t
        self.allow = allow
        self.cards = []
        self.steps = []
        self.memory = gt.MemoryStore()
        self.tts = FakeTTS()

    def step(self, text=""):
        self.steps.append(text)

    def tool_card(self, *a):
        self.cards.append(a)

    def confirm(self, title, body):
        CALLS["confirm"] += 1
        return self.allow

    def show_news(self, items):
        pass

    def show_image(self, path):
        pass

    def show_vitals(self, st):
        pass

    def show_weather(self, w):
        pass


# The fake has to keep up with the real thing. Walk george_tools for
# every `ag.<attr>` and fail if the stand-in is missing one -- otherwise
# this whole file silently stops covering a tool the day it starts using
# a new callback.
import ast as _ast                 # noqa: E402
_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "george_tools.py")).read()
_needed = {n.attr for n in _ast.walk(_ast.parse(_src))
           if isinstance(n, _ast.Attribute)
           and isinstance(n.value, _ast.Name) and n.value.id == "ag"}
_probe = FakeAgent()
for _attr in sorted(_needed):
    if not hasattr(_probe, _attr):
        FAILS.append("FakeAgent is missing ag.%s, which the tools call "
                     "- this file is not testing what it claims to"
                     % _attr)


# Valid args per tool. A tool missing from here gets {} for the happy
# case too -- which is correct for the ones that take nothing.
VALID = {
    "lookup": {"term": "python"},
    "web_search": {"query": "python"},
    "research": {"query": "python", "read": 1},
    "open_page": {"url": "http://x.invalid"},
    "show": {"url": "http://x.invalid"},
    "open_path": {"path": _t},
    "news": {"topic": "", "count": 5},
    "weather": {"location": "Dublin"},
    "run": {"command": "ls"},
    "code": {"language": "python", "source": "print(1)"},
    "pkg": {"action": "search", "package": "ripgrep"},
    "launch": {"app": "gedit"},
    "media": {"action": "play"},
    "volume": {"action": "get"},
    "power": {"action": "lock"},
    "read_file": {"path": os.path.join(_t, "f.txt")},
    "write_file": {"path": os.path.join(_t, "w.txt"), "text": "hello"},
    "find": {"pattern": "*.txt", "path": _t},
    "list_dir": {"path": _t},
    "clipboard": {"mode": "read"},
    "remember": {"key": "k", "value": "v"},
    "recall": {"query": "k"},
    "forget": {"key": "k"},
    "note": {"text": "a note"},
    "calc": {"expression": "2+2"},
    "timer": {"seconds": 1, "label": "t"},
    "say": {"text": "hello"},
    "see": {"question": "what is on screen"},
    "processes": {"sort": "cpu"},
    "screenshot": {},
    "system": {},
    "disk": {},
    "network": {},
    "diagnose": {},
}
open(os.path.join(_t, "f.txt"), "w").write("file contents")

JUNK = [
    {},
    {"query": None, "path": None, "command": None, "url": None},
    {"query": 12345, "path": [], "command": {}, "url": object()},
    {"unexpected_key": "surprise"},
    {"query": "x" * 5000},
]

# Tools that legitimately change something and MUST ask first.
MUST_CONFIRM = {"code", "write_file", "power"}
# Tools whose observation must never assert the screen unless it worked.
SCREEN_WORDS = ("on his screen", "on your screen", "now on screen")

untested = [t for t in gt.TOOLS if t not in VALID]
check(not untested, "no valid-args case for: %s" % untested)

for name in sorted(gt.TOOLS):
    fn = gt.TOOLS[name]

    # --- junk args must never raise -----------------------------------
    for bad in JUNK:
        ag = FakeAgent()
        try:
            out = fn(dict(bad), ag)
        except Exception as exc:
            FAILS.append("%s raised on junk args %r: %r"
                         % (name, bad, exc))
            continue
        check(isinstance(out, str),
              "%s returned %s, not a string, on junk args"
              % (name, type(out).__name__))

    # --- valid args ---------------------------------------------------
    before = CALLS["confirm"]
    ag = FakeAgent()
    try:
        out = fn(dict(VALID.get(name, {})), ag)
    except Exception as exc:
        FAILS.append("%s raised on VALID args: %r" % (name, exc))
        continue
    check(isinstance(out, str) and out.strip(),
          "%s returned nothing useful on valid args: %r" % (name, out))

    if name in MUST_CONFIRM:
        check(CALLS["confirm"] > before,
              "%s changed something without asking him first" % name)

    # --- a declined confirmation must stop it -------------------------
    if name in MUST_CONFIRM:
        ag = FakeAgent(allow=False)
        out = fn(dict(VALID.get(name, {})), ag)
        low = out.lower()
        check("declin" in low or "refus" in low or "not " in low,
              "%s does not report a refusal clearly: %r" % (name, out[:90]))
        check("do not retry" in low or "declined" in low,
              "%s does not tell the model to stop after a refusal" % name)

# --- the screen claim, specifically -----------------------------------
# `news` fills a sidebar card and opens nothing. It must say so.
ag = FakeAgent()
out = gt.TOOLS["news"](dict(VALID["news"]), ag).lower()
check("nothing has been opened" in out,
      "news does not state that it opened nothing")
for w in SCREEN_WORDS:
    if w in out:
        check("nothing has been opened on his screen" in out,
              "news implies the screen without the disclaimer")

# `show` may claim it, but only by relaying what open_in_browser said
ag = FakeAgent()
out = gt.TOOLS["show"](dict(VALID["show"]), ag)
check(CALLS["opened"], "show did not actually try to open anything")

# --- read-only tools must not have asked or run anything --------------
CALLS["confirm"] = 0
CALLS["shell"] = []
for name in ("system", "disk", "network", "processes", "list_dir",
             "read_file", "recall", "calc", "weather", "news"):
    ag = FakeAgent()
    gt.TOOLS[name](dict(VALID.get(name, {})), ag)
check(CALLS["confirm"] == 0,
      "a read-only tool asked for confirmation %d times" % CALLS["confirm"])

# --- calc must compute, not guess -------------------------------------
ag = FakeAgent()
check("4" in gt.TOOLS["calc"]({"expression": "2+2"}, ag),
      "calc got 2+2 wrong")
bad = gt.TOOLS["calc"]({"expression": "__import__('os').system('x')"}, ag)
check("os" not in bad or "error" in bad.lower() or "cannot" in bad.lower(),
      "calc evaluated something it should not have: %r" % bad[:80])

# --- write_file must respect the sandbox ------------------------------
ag = FakeAgent()
out = gt.TOOLS["write_file"]({"path": "/etc/passwd", "text": "x"}, ag)
check("REFUSED" in out or "sandbox" in out.lower(),
      "write_file accepted a path outside the sandbox: %r" % out[:90])

# --- memory round trip ------------------------------------------------
ag = FakeAgent()
gt.TOOLS["remember"]({"key": "colour", "value": "teal"}, ag)
out = gt.TOOLS["recall"]({"query": "colour"}, ag)
check("teal" in out, "recall did not find what remember stored: %r" % out)
gt.TOOLS["forget"]({"key": "colour"}, ag)
out = gt.TOOLS["recall"]({"query": "colour"}, ag)
check("teal" not in out, "forget did not remove the fact")

print("tool-surface checks; failures: %d  (%d tools x %d arg shapes)"
      % (len(FAILS), len(gt.TOOLS), len(JUNK) + 1))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
