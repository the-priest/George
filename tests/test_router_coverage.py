"""What the router covers, and the one rule that keeps being broken.

THE RULE: normalised text decides WHICH rule fires. It must never
supply a VALUE that ends up in a tool argument.

normalise() lowercases and strips everything outside [\\w\\s:/.@-]. That
is right for matching and wrong for every payload, and it has now
produced the same bug three separate times:

    open https://youtu.be/dQw4w9WgXcQ?t=43  ->  .../dqw4w9wgxcq   (404)
    open ~/projects                         ->  "open projects"   (no ~)
    calculate 15*23                         ->  "15 23"           (no *)

So anything carried out of the message is taken from the RAW string,
and this file checks that for every payload-carrying route at once.

Coverage matters for a second reason: a routed question reaches an
answer in ONE model call instead of two. On his CPU that is tens of
seconds per turn, so every rule here is a speed fix as much as a
correctness one.
"""
import os
import sys
import tempfile
import time

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


def plan_of(text):
    return R.route(text)


def args_of(text):
    p = R.route(text)
    return (p.prefetch[0][1] if p and p.prefetch else None)


# =====================================================================
# NO PAYLOAD MAY BE DAMAGED BY NORMALISATION
#
# Every case here contains a character normalise() destroys: an
# uppercase letter, a `~`, a `*`, a `?`, a `&`, or a `+`.
# =====================================================================
PAYLOADS = [
    # (message, tool, arg key, exact expected value)
    ("open https://youtu.be/dQw4w9WgXcQ?t=43",
     "show", "url", "https://youtu.be/dQw4w9WgXcQ?t=43"),
    ("show me https://example.com/a?b=1&c=2#F",
     "show", "url", "https://example.com/a?b=1&c=2#F"),
    ("read https://example.com/Some_Article",
     "open_page", "url", "https://example.com/Some_Article"),
    ("summarise https://example.com/X?y=Z",
     "open_page", "url", "https://example.com/X?y=Z"),
    ("open ~/projects/GeorgeAI",
     "open_path", "path", "~/projects/GeorgeAI"),
    ("open /etc/hosts", "open_path", "path", "/etc/hosts"),
    ("whats in ~/Downloads", "list_dir", "path", "~/Downloads"),
    ("list the files in /var/log", "list_dir", "path", "/var/log"),
    ("calculate 15*23", "calc", "expression", "15*23"),
    ("whats 2+2*3", "calc", "expression", "2+2*3"),
    ("calc (3+4)*2", "calc", "expression", "(3+4)*2"),
    ("what is 2^10", "calc", "expression", "2**10"),
    ("find files called Config.JSON", "find", "pattern", "Config.JSON"),
]
for text, tool, key, want in PAYLOADS:
    p = plan_of(text)
    check(p is not None and p.prefetch,
          "%r did not route at all" % text)
    if not p or not p.prefetch:
        continue
    got_tool, got_args = p.prefetch[0]
    check(got_tool == tool,
          "%r routed to %s, wanted %s" % (text, got_tool, tool))
    check(got_args.get(key) == want,
          "%r: %s=%r, wanted %r  (normalisation damaged the payload)"
          % (text, key, got_args.get(key), want))


# =====================================================================
# READING A PAGE IS NOT OPENING IT
# =====================================================================
check(plan_of("read https://example.com").prefetch[0][0] == "open_page",
      "'read <url>' should fetch the text, not open a browser")
check(plan_of("open https://example.com").prefetch[0][0] == "show",
      "'open <url>' should put it on screen, not silently fetch it")
check("not say it was" in plan_of("read https://example.com").hint or
      "Nothing was put on his screen" in plan_of("read https://example.com").hint,
      "the read hint must stop George claiming it is on screen")


# =====================================================================
# THE CLOCK RULE MAY ONLY ANSWER ABOUT HIS CLOCK
#
# The CONTEXT block carries HIS local time. "what's the time in tokyo"
# matched the clock rule and was answered with Irish time, confidently
# and wrongly.
# =====================================================================
p = plan_of("what time is it")
check(p is not None and p.name == "clock" and not p.prefetch,
      "his own clock should still be answered from context")
for text in ("whats the time in tokyo", "what time is it in new york",
             "what is the time in california"):
    p = plan_of(text)
    check(p is None or p.name != "clock",
          "%r was answered from his local clock" % text)


# =====================================================================
# THE ROUTER STILL MAY NOT PREFETCH A STATE CHANGE
#
# It runs BEFORE the model has decided anything, so anything it fires
# is unreviewed. Widening coverage must not widen this.
# =====================================================================
READ_ONLY_PREFETCH = {
    "weather", "system", "disk", "processes", "network", "news",
    "web_search", "recall", "pkg", "diagnose", "research", "lookup",
    "list_dir", "calc", "find", "open_page",
    # show/open_path put something on his screen and nothing else; they
    # are the whole point of "open this" and change no state.
    "show", "open_path",
}
PROBE = [
    "play some music", "pause the music", "next track", "mute it",
    "turn the volume up", "lock the screen", "suspend the laptop",
    "shut down the machine", "reboot", "launch firefox",
    "remember that i hate mondays", "forget everything",
    "make a note about the bins", "set a timer for 10 minutes",
    "delete ~/Downloads", "rm -rf /", "write a file to ~/test.txt",
    "install ripgrep", "update everything", "remove firefox",
    "run make clean", "take a screenshot",
] + [t for t, _tool, _k, _v in PAYLOADS]
for text in PROBE:
    p = plan_of(text)
    for tool, a in (p.prefetch if p else []):
        check(tool in READ_ONLY_PREFETCH,
              "%r prefetches %s, which is not on the read-only list"
              % (text, tool))
        check(str(a.get("action", "")).lower() not in
              ("install", "update", "upgrade", "remove", "delete"),
              "%r prefetches a state-changing action" % text)

# and every tool the router can name must actually exist
for text in PROBE + ["whats the weather", "brief me", "any news"]:
    p = plan_of(text)
    for tool, _a in (p.prefetch if p else []):
        check(tool in gt.TOOLS, "the router names a tool that does not "
                                "exist: %s" % tool)


# =====================================================================
# THE ARGUMENTS MUST SURVIVE THE TOOL'S OWN REPAIR PASS
#
# A route that produces an argument call_tool then throws away is a
# route that silently costs a round trip instead of saving one.
# =====================================================================
for text, tool, key, want in PAYLOADS:
    args = args_of(text) or {}
    fixed, _notes = gt.repair_args(tool, dict(args))
    check(fixed.get(key) == want,
          "repair_args(%s) dropped or changed %s: %r -> %r"
          % (tool, key, want, fixed.get(key)))
    complaint = gt.missing_arg_message(tool, fixed)
    check(not complaint,
          "the router's own arguments for %r are rejected: %s"
          % (text, complaint))


# =====================================================================
# AND IT MUST NOT FIRE ON PROSE THAT MERELY CONTAINS THE WORDS
# =====================================================================
MUST_NOT = [
    "calculate the risk of this migration",
    "calculate my mortgage over 30 years",
    "open a bank account",
    "read me a bedtime story",
    "find me a good restaurant",
    "can you explain how tcp works",
    "how would you check the weather",
    "write me a python script that parses json",
    "i was thinking about the weather station i built last summer and "
    "how the sensors kept failing in the cold, do you reckon it was "
    "condensation or something else entirely",
    "",
]
for text in MUST_NOT:
    p = plan_of(text)
    check(p is None, "%r should not route, got %s"
          % (text[:44], R.describe(p)))


# =====================================================================
# COVERAGE AND COST
#
# Each hit is one model round trip removed from the turn. On CPU
# inference that is the difference between a reply and a wait, so the
# hit rate is pinned: it may go up, it may not quietly go down.
# =====================================================================
CORPUS = [
    "whats the weather", "weather in galway", "do i need a coat",
    "hows the box doing", "how much ram do i have", "uptime",
    "disk space", "am i running out of space", "whats eating the cpu",
    "why is my box so slow", "is everything ok",
    "is ripgrep installed", "install ripgrep",
    "am i online", "whats my ip",
    "whats on the news", "any news", "brief me", "what did i miss",
    "search the web for pacman hooks", "google rust async",
    "open https://news.ycombinator.com", "show me www.rte.ie",
    "read https://example.com/article",
    "open my downloads folder", "open ~/projects",
    "whats in my downloads", "list the files in my home directory",
    "calculate 15*23", "work out 1024/16",
    "find files called config.json",
    "what do you know about me", "what did i ask you to remember",
    "what time is it", "who is ada lovelace",
]
routed = [q for q in CORPUS if R.route(q) is not None]
rate = 100.0 * len(routed) / len(CORPUS)
check(rate >= 90.0,
      "router coverage fell to %.0f%% on the common corpus; misses: %s"
      % (rate, [q for q in CORPUS if R.route(q) is None]))

t0 = time.time()
for _ in range(200):
    for q in CORPUS + MUST_NOT:
        R.route(q)
per = (time.time() - t0) / (200 * len(CORPUS + MUST_NOT))
check(per < 5e-4, "routing costs %.6fs per message, too slow" % per)


# =====================================================================
# END TO END: a newly covered question really does cost one call
# =====================================================================
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


cfg = dict(gc.DEFAULTS)
cfg["verify"] = "off"          # measuring routing, not verification
ag = gt.Agent(cfg, gt.MemoryStore(), DummyTTS())
ag.ollama = CountingOllama(
    ['{"tool":"answer","args":{"text":"345."}}'])
got = {}
ag.on_final = lambda s: got.update({"final": s})
ag.run_turn("calculate 15*23")
check(ag.ollama.calls == 1,
      "a routed sum took %d model calls, expected 1" % ag.ollama.calls)
check(any("OBSERVATION (calc)" in m["content"] for m in ag.history),
      "the calc observation never reached the model")
check(any("345" in m["content"] for m in ag.history),
      "calc did not actually compute 15*23 before the model was called")

print("router coverage checks; failures: %d  (%.0f%% of %d routed, "
      "%.0fus each)" % (len(FAILS), rate, len(CORPUS), per * 1e6))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
