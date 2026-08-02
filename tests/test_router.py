"""The intent router.

George's loop made the model decide things the words had already
decided. "What's the weather?" cost two model round trips: one to pick
the weather tool, one to write the answer. On CPU inference that is
most of the latency of a turn, spent on a choice that was never in
doubt.

The router runs the obviously-implied tools BEFORE the first model
call, so the model gets one call with the observations already in hand.

It is deliberately conservative: a miss costs one extra round trip,
which is exactly what happened before, but a WRONG prefetch wastes a
tool run and pollutes the context. Every "must not route" case below is
as important as the ones that must.
"""
import json
import os
import sys
import tempfile

_t = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = _t + "/cfg"
os.environ["XDG_DATA_HOME"] = _t + "/data"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_core as gc          # noqa: E402
import george_intent as R         # noqa: E402
import george_tools as gt         # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def tools_of(text):
    p = R.route(text)
    if p is None:
        return None
    return [t for t, _a in p.prefetch]


# -- small talk needs no tools -----------------------------------------
for greeting in ("hi", "hey", "hello george", "thanks", "cheers",
                 "how are you", "what can you do", "good morning",
                 "ok", "nice one", "bye"):
    p = R.route(greeting)
    check(p is not None and p.chat and not p.prefetch,
          "%r should route to chat with no tools, got %s"
          % (greeting, R.describe(p)))

# -- the obvious cases must prefetch the right tool --------------------
EXPECT = {
    "what's the weather": ["weather"],
    "weather in galway": ["weather"],
    "do i need a coat": ["weather"],
    "how's the box doing": ["system"],
    "how much ram do i have": ["system"],
    "what distro am i on": ["system"],
    "uptime": ["system"],
    "disk space": ["disk"],
    "am i running out of space": ["disk"],
    "what's eating the cpu": ["system", "processes"],
    # slowness now gets the pre-analysed verdict, not two raw dumps the
    # model has to compare itself
    "why is my box so slow": ["diagnose"],
    "whats wrong with my box": ["diagnose"],
    "is everything ok": ["diagnose"],
    "is ripgrep installed": ["pkg"],
    "install ripgrep": ["pkg"],
    "am i online": ["network"],
    "what's my ip": ["network"],
    "what's on the news": ["news"],
    "any news": ["news"],
    "chilling can u pull up some news": ["news"],
    "brief me": ["system", "weather", "news"],
    "what did i miss": ["system", "weather", "news"],
    "how's everything": ["system", "weather", "news"],
    "search the web for pacman hooks": ["web_search"],
    "google rust async": ["web_search"],
    "open https://news.ycombinator.com": ["show"],
    "show me www.rte.ie": ["show"],
    "what do you know about me": ["recall"],
    "what time is it": [],
}
for text, want in EXPECT.items():
    got = tools_of(text)
    check(got == want, "%r -> %s, wanted %s" % (text, got, want))

# -- a URL beats a keyword inside it -----------------------------------
# "open https://news.ycombinator.com" used to match the NEWS rule,
# because the word "news" is in the hostname, and pulled RSS feeds
# instead of opening the page he named.
p = R.route("open https://news.ycombinator.com")
check(p is not None and p.prefetch[0][0] == "show",
      "a URL containing a keyword routed to the keyword's tool")
check(p.prefetch[0][1]["url"].startswith("https://news.ycombinator"),
      "the URL was not carried through: %r" % (p.prefetch[0][1],))

# -- and these must NOT route ------------------------------------------
MUST_NOT = [
    # capability questions, not requests
    "can you explain how tcp works",
    "how would you check the weather",
    "why did you say that",
    "next time just tell me the headline",
    "from now on skip the preamble",
    # real work the model has to think about
    "write me a python script that parses json",
    "explain the difference between a thread and a process",
    "refactor this function to use a generator",
    # a keyword buried in prose is a topic, not a command
    "i was thinking about the weather station i built last summer and "
    "how the sensors kept failing in the cold, do you reckon it was "
    "condensation or something else entirely",
    "",
]
for text in MUST_NOT:
    p = R.route(text)
    check(p is None, "%r should not route, got %s" % (text[:48],
                                                      R.describe(p)))

# -- polite requests for the thing NOW still route ---------------------
for text in ("can you tell me the weather", "could you check the news"):
    check(tools_of(text), "%r is a real request and should route" % text)

# -- THE ROUTER MUST NEVER PREFETCH A STATE CHANGE ---------------------
# It runs BEFORE the model has decided anything, so anything it fires is
# unreviewed. "install ripgrep" must prefetch a SEARCH; the model then
# proposes the install and he confirms it, which is the path every state
# change has to take.
_MUTATING = {"install", "update", "remove", "upgrade", "delete"}
probe = ["install ripgrep", "install the package neovim", "search for htop",
         "is ripgrep installed", "do i have curl installed",
         "update everything", "remove firefox", "install docker"]
for text in probe:
    p = R.route(text)
    if p is None:
        continue
    for tool, a in p.prefetch:
        act = str(a.get("action", "")).lower()
        check(act not in _MUTATING,
              "%r prefetches a state-changing %s action %r"
              % (text, tool, act))
        check(tool not in ("run", "write_file", "power", "launch",
                           "open_path", "forget", "remember", "note"),
              "%r prefetches %s, which can change the machine"
              % (text, tool))

# every rule in the table, checked the same way
for name, _pat, _b in R._RULES:
    pass
for text in list(EXPECT) + probe:
    p = R.route(text)
    if p is None:
        continue
    for tool, _a in p.prefetch:
        check(tool in ("weather", "system", "disk", "processes", "network",
                       "news", "show", "web_search", "recall", "pkg",
                       "diagnose", "research"),
              "%r prefetches an unexpected tool: %s" % (text, tool))

# -- composite tools must be registered and documented -----------------
for t in ("pkg", "diagnose", "research"):
    check(t in gt.TOOLS, "composite tool %s is not registered" % t)

# -- pkg must never emit a partial upgrade -----------------------------
class _A:
    cfg = dict(gc.DEFAULTS)

    def step(self, *a):
        pass

    def tool_card(self, *a):
        pass


seen_cmds = []
_real_run = gt.tool_run
gt.tool_run = lambda args, ag: seen_cmds.append(args["command"]) or "exit=0"
try:
    for verb in ("search", "info", "installed", "install", "update"):
        gt.tool_pkg({"action": verb, "package": "ripgrep"}, _A())
finally:
    gt.tool_run = _real_run
for c in seen_cmds:
    check("pacman -Sy " not in c + " ",
          "pkg emitted a partial upgrade: %r" % c)
    check(not (c.startswith("pacman -S ") and "-Syu" not in c),
          "pkg emitted a bare pacman -S: %r" % c)
check(seen_cmds, "pkg ran no commands at all")

# -- the router must be able to be turned off --------------------------
check(R.route("what's the weather", enabled=False) is None,
      "router ignored the disable flag")
check("router" in gc.DEFAULTS, "there is no config key to disable the router")

# -- routing must be fast: it runs before every turn -------------------
import time                        # noqa: E402
corpus = list(EXPECT) + MUST_NOT
t0 = time.time()
for _ in range(200):
    for text in corpus:
        R.route(text)
per = (time.time() - t0) / (200 * len(corpus))
check(per < 5e-4, "routing costs %.6fs per message, too slow" % per)

# -- END TO END: the saving is real ------------------------------------
# A routed question must reach an answer in ONE model call. Without the
# router the same question needs two: one to pick the tool, one to
# answer.
class DummyTTS:
    def speak(self, text):
        pass

    def stop(self):
        pass


class CountingOllama:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def alive(self):
        return True

    def resolve_model(self):
        return ("mock:1", "")

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        self.calls += 1
        return self.replies.pop(0) if self.replies else ""


gt.TOOLS["system"] = lambda args, ag: "cpu 12%, ram 4.1/15.0 GiB, up 2d"

# verify=off here ON PURPOSE: this block measures what ROUTING costs.
# The verification pass adds one cheap constrained call on any
# tool-backed answer, which is a real cost but a different one, and it
# has its own file.
cfg = dict(gc.DEFAULTS)
cfg["verify"] = "off"
ag = gt.Agent(cfg, gt.MemoryStore(), DummyTTS())
ag.ollama = CountingOllama(
    ['{"tool":"answer","args":{"text":"The box is fine - 12% CPU."}}'])
got = {}
ag.on_final = lambda s: got.update({"final": s})
ag.run_turn("how's the box doing")
check(ag.ollama.calls == 1,
      "routed turn took %d model calls, expected 1" % ag.ollama.calls)
check(got.get("final") == "The box is fine - 12% CPU.",
      "routed turn did not produce the answer: %r" % got.get("final"))

# the observation really was in front of the model on its first call
seen = [m["content"] for m in ag.history]
check(any("OBSERVATION (system)" in c for c in seen),
      "the prefetched observation never reached the history")
check(any(c.startswith("GUIDANCE:") for c in seen),
      "the router hint never reached the history")

# ...and with the router off, the same question costs two calls
cfg2 = dict(gc.DEFAULTS)
cfg2["verify"] = "off"
cfg2["router"] = False
ag2 = gt.Agent(cfg2, gt.MemoryStore(), DummyTTS())
ag2.ollama = CountingOllama([
    '{"tool":"system","args":{}}',
    '{"tool":"answer","args":{"text":"The box is fine."}}'])
ag2.on_final = lambda s: None
ag2.run_turn("how's the box doing")
check(ag2.ollama.calls == 2,
      "unrouted turn took %d calls, expected 2" % ag2.ollama.calls)

# -- small talk must not run a tool either -----------------------------
ag3 = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), DummyTTS())
ag3.ollama = CountingOllama(
    ['{"tool":"answer","args":{"text":"Hey. What do you need?"}}'])
final3 = {}
ag3.on_final = lambda s: final3.update({"t": s})
ag3.run_turn("hi")
check(ag3.ollama.calls == 1, "a greeting took %d calls" % ag3.ollama.calls)
check(not any("OBSERVATION" in m["content"] for m in ag3.history),
      "a greeting ran a tool")
check(final3.get("t") == "Hey. What do you need?",
      "greeting reply mangled: %r" % final3.get("t"))

print("router checks; failures: %d  (%d rules, %.0fus per route)"
      % (len(FAILS), len(R._RULES), per * 1e6))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
