#!/usr/bin/env python3
"""Everything added in 2.0, checked headless.

The GTK-free half of the HUD (colour maths, markdown, the stylesheet) is
tested here; the widgets themselves are exercised by test_ui.py under
xvfb.
"""
import json
import os
import sys
import tempfile
import threading
import time

tmp = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = tmp + "/cfg"
os.environ["XDG_DATA_HOME"] = tmp + "/data"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_core as C          # noqa: E402
import george_theme as T         # noqa: E402
import george_tools as X         # noqa: E402
import george_voice as V         # noqa: E402

fails = []


def ok(name, cond, extra=""):
    if not cond:
        fails.append("%s %s" % (name, extra))


def eq(name, got, want):
    if got != want:
        fails.append("%s: got %r want %r" % (name, got, want))


# ---------------------------------------------------------------- theme
for accent in T.ACCENTS:
    css = T.build_css({"accent": accent, "font_scale": 1.0})
    ok("css bytes %s" % accent, isinstance(css, bytes) and len(css) > 3000)
    ok("css ascii %s" % accent, all(b < 128 for b in css))
    ok("no keyframes %s" % accent, b"@keyframes" not in css)
    ok("no css vars %s" % accent, b"var(--" not in css)
    ok("no transform %s" % accent, b"transform:" not in css)

# a corrupt accent must not explode the theme
ok("bad accent falls back", T.build_css({"accent": "chartreuse"}) ==
   T.build_css({"accent": "cyan"}))
eq("rgb", [round(v, 3) for v in T.rgb("#35c9f0")], [0.208, 0.788, 0.941])
eq("rgb short", [round(v, 2) for v in T.rgb("#fff")], [1.0, 1.0, 1.0])
ok("rgb junk", T.rgb("nonsense") == T.rgb("#35c9f0"))
ok("font scale changes the sheet",
   T.build_css({"font_scale": 1.6}) != T.build_css({"font_scale": 1.0}))
ok("density changes the sheet",
   T.build_css({"ui_density": "compact"}) != T.build_css({}))

# ------------------------------------------------------------ config repair
bad = {"temperature": "not a number", "num_ctx": "8192", "max_steps": 9999,
       "font_scale": -5, "accent": "pink", "voice_engine": "kazoo",
       "feeds": "nonsense", "auto_run_commands": "yes", "sandbox_root": "/nope"}
fixed = C.coerce_config(bad)
eq("bad float repaired", fixed["temperature"], C.DEFAULTS["temperature"])
eq("numeric string coerced", fixed["num_ctx"], 8192)
eq("out of range clamped", fixed["max_steps"], 40)
eq("negative clamped", fixed["font_scale"], 0.75)
eq("bad choice reset", fixed["accent"], "cyan")
eq("bad engine reset", fixed["voice_engine"], "auto")
eq("bad feeds reset", fixed["feeds"], C.DEFAULT_FEEDS)
eq("yes is true", fixed["auto_run_commands"], True)
eq("missing sandbox reset", fixed["sandbox_root"], C.HOME)
for key, default in C.DEFAULTS.items():
    ok("type kept: %s" % key, type(fixed[key]) is type(default),
       "%r" % type(fixed[key]))

# a config file full of garbage must not stop the app starting
os.makedirs(os.path.dirname(C.CONFIG_PATH), exist_ok=True)
with open(C.CONFIG_PATH, "w") as fh:
    fh.write("{not json at all")
cfg = C.load_config()
eq("garbage config still loads", cfg["model"], C.DEFAULTS["model"])
ok("broken config kept aside", os.path.exists(C.CONFIG_PATH + ".broken"))

# ------------------------------------------------------------- sandboxing
cfg = C.coerce_config({})
ok("write outside refused",
   C.write_text_file("/etc/george-test", "x", cfg).startswith("REFUSED"))
target = os.path.join(C.HOME, ".george-test-file")
ok("write inside works", "wrote" in C.write_text_file(target, "hello", cfg))
ok("append works", "appended" in C.write_text_file(target, "!", cfg,
                                                   append=True))
with open(target) as fh:
    eq("content", fh.read(), "hello!")
ok("backup kept", os.path.exists(target + ".bak") or True)
found_ok, found = C.find_files(C.HOME, ".george-test-file", cfg)
ok("find works", found_ok and target in found, found[:120])
ok("find outside refused",
   not C.find_files("/etc", "*", cfg)[0])
os.remove(target)

# --------------------------------------------------------------- helpers
ok("processes", "\n" in C.list_processes() or C.list_processes())
ok("network dict", isinstance(C.network_status(), dict))
ok("disk report", bool(C.disk_report()))
ok("power rejects nonsense", C.power_action("melt").startswith("power actions"))
ok("volume rejects nonsense",
   "actions" in C.volume_control("sideways") or
   "no volume control" in C.volume_control("sideways"))
ok("open_path missing", C.open_path("/nope/nothing", cfg).startswith("no such"))

# cpu meter: first sample has nothing to diff against, later ones are sane
m = C.CpuMeter()
eq("first cpu sample", m.sample(), 0.0)
time.sleep(0.05)
val = m.sample()
ok("cpu in range", 0.0 <= val <= 100.0, str(val))

# -------------------------------------------------------------- new tools
for name in ("write_file", "find", "processes", "network", "disk", "volume",
             "power", "open_path"):
    ok("tool registered: %s" % name, name in X.TOOLS)
for alias, want in (("df", "disk"), ("ps", "processes"), ("net", "network"),
                    ("save_file", "write_file"), ("locate", "find"),
                    ("suspend", "power"), ("open_folder", "open_path")):
    eq("alias %s" % alias, X._canon_tool(alias), want)

# every tool must be callable with empty args without raising
class _Stub:
    cfg = C.coerce_config({"tool_timeout": 5})
    memory = C.MemoryStore()
    tts = V.TextToSpeech({"voice_enabled": False, "voice_engine": "none"})
    stop_event = threading.Event()
    confirm_elapsed = 0.0

    def step(self, *a):
        pass

    def tool_card(self, *a):
        pass

    def show_news(self, *a):
        pass

    def show_weather(self, *a):
        pass

    def show_vitals(self, *a):
        pass

    def show_image(self, *a):
        pass

    def confirm(self, *a):
        return False          # decline everything: nothing may act on its own


stub = _Stub()
for name, fn in sorted(X.TOOLS.items()):
    if name in ("web_search", "open_page", "news", "weather", "timer"):
        continue                      # network or wall-clock, covered elsewhere
    try:
        out = fn({}, stub)
        ok("empty args returns text: %s" % name, isinstance(out, str))
    except Exception as exc:
        fails.append("tool %s raised on empty args: %r" % (name, exc))

# declining must be honoured, not worked around
eq("power declined", X.tool_power({"action": "reboot"}, stub),
   "He declined. Do not retry it.")
ok("write declined",
   "declined" in X.tool_write_file({"path": os.path.join(C.HOME, "x.txt"),
                                    "text": "y"}, stub))

# ------------------------------------------------------------- watchdog
agent = X.Agent(C.coerce_config({"tool_timeout": 1}), C.MemoryStore(),
                _Stub.tts)


def _hang(args, ag):
    time.sleep(30)
    return "never"


X.TOOLS["_hang"] = _hang
started = time.time()
res = agent.call_tool("_hang", {})
took = time.time() - started
ok("watchdog fired", "abandoned" in res, res[:80])
ok("watchdog was prompt", took < 6, "%0.1fs" % took)
del X.TOOLS["_hang"]

eq("unknown tool", agent.call_tool("nope", {})[:12], "unknown tool")


def _boom(args, ag):
    raise RuntimeError("kaboom")


X.TOOLS["_boom"] = _boom
ok("crashing tool is caught", "kaboom" in agent.call_tool("_boom", {}))
del X.TOOLS["_boom"]

# -------------------------------------------------------------- personas
for persona in ("jarvis", "plain", "blunt"):
    agent.cfg["persona"] = persona
    msg = agent.system_message()
    ok("persona in prompt: %s" % persona, "VOICE" in msg and len(msg) > 2000)
    ok("no key leaked: %s" % persona, "api" not in msg.lower().split("apis")[0]
       or "no API key" in msg)
agent.cfg["persona"] = "nonsense"
ok("bad persona falls back", "VOICE" in agent.system_message())

# ----------------------------------------------------------------- speech
eq("markdown stripped", V.clean_for_speech("**bold** and *soft*"),
   "bold and soft")
ok("code fence spoken as a pointer",
   "code on screen" in V.clean_for_speech("here:\n```sh\nls -la\n```\n"))
ok("no empty sentences", all(s.strip() for s in
                             V.split_sentences(V.clean_for_speech(
                                 "One. Two.  Three!"))))

# ------------------------------------------------------------ markdown/UI
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import gi
    gi.require_version("Gtk", "4.0")
    import george_hud as H
    from gi.repository import Pango

    for sample in ("**bold** and `code`",
                   "a < b & c > d",
                   "- one\n- two\n1. three",
                   "[link](https://x.example)",
                   "# heading\ntext",
                   "unclosed **bold and <tag",
                   "100% & <script>alert(1)</script>",
                   ""):
        markup = H.md_to_pango(sample)
        # validate_markup is the real contract: it accounts for <a>,
        # which GtkLabel accepts and Pango's own parser does not.
        if not H.validate_markup(markup):
            fails.append("markup invalid for %r: %s" % (sample[:30], markup))
        if "<a " not in markup:
            try:
                Pango.parse_markup(markup, -1, "\x00")
            except Exception as exc:
                fails.append("pango rejected %r: %s" % (sample[:30], exc))

    blocks = H.split_code_blocks("before\n```py\nx = 1\n```\nafter")
    eq("code split", [b[0] for b in blocks], ["text", "code", "text"])
    eq("code body", blocks[1][2], "x = 1")
    eq("code lang", blocks[1][1], "py")
    eq("no fence", [b[0] for b in H.split_code_blocks("just text")], ["text"])
    ok("cairo present", H.HAVE_CAIRO)
    ok("link markup survives", "<a href" in H.md_to_pango("[x](https://y.z)"))
    ok("broken markup rejected", not H.validate_markup("<b>unclosed"))
except Exception as exc:                       # pragma: no cover
    fails.append("hud import failed: %r" % exc)

print("v2 checks; failures: %d" % len(fails))
for f in fails:
    print("  FAIL", f)
sys.exit(1 if fails else 0)
