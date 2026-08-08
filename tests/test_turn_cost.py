"""What a turn actually COSTS, in the only two units that matter.

On a laptop with no real GPU, waiting is made of exactly two things:

    PREFILL     tokens the model must read before it writes anything
    GENERATION  tokens it then writes, at three to six a second

Everything else is noise. So this measures both, per turn, for the
things he actually does - reading the news and talking - and fails if
either goes up. It is a budget, not a benchmark: the numbers are pinned
so an innocent-looking addition to the prompt cannot quietly cost him
thirty seconds a question.

PREFILL IS NOT ALL EQUAL. ollama caches the KV of a prompt PREFIX, so
the system message is paid once per session and then reused - as long
as it is byte-identical, which test_speed.py pins. What is paid on
EVERY model call is the tail: the conversation, the observations, and
anything appended this turn. Both are reported; `fresh` is the honest
worst case (first question of a session, or after the model unloads),
`cached` is the steady state.
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
import george_tools as gt         # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def toks(text):
    """Close enough. Four characters a token is within 10% for English
    prose and the comparison between runs is what matters."""
    return len(text) // 4


class DummyTTS:
    def speak(self, text):
        pass

    def stop(self):
        pass


class Meter:
    """Stands in for ollama and records what it was asked to read."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def alive(self):
        return True

    def resolve_model(self):
        return ("mock:1", "")

    def abort(self):
        pass

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        system = "".join(m["content"] for m in messages
                         if m["role"] == "system")
        tail = "".join(m["content"] for m in messages
                       if m["role"] != "system")
        reply = self.replies.pop(0) if self.replies else \
            '{"tool":"answer","args":{"text":"ok"}}'
        self.calls.append({"system": toks(system), "tail": toks(tail),
                           "out": toks(reply), "sys_text": system})
        for piece in (reply[i:i + 40] for i in range(0, len(reply), 40)):
            on_token(piece)
        return reply


NEWS = "\n".join(
    "%d. Some headline about a thing that happened today - %s"
    % (i, "detail " * 8) for i in range(1, 9))


def measure(text, replies, tools=None, cfg=None):
    conf = dict(gc.DEFAULTS)
    conf.update(cfg or {})
    ag = gt.Agent(conf, gt.MemoryStore(), DummyTTS())
    ag.ollama = Meter(replies)
    ag.on_final = lambda s: None
    ag.on_error = lambda s: None
    saved = {}
    for name, fn in (tools or {}).items():
        saved[name] = gt.TOOLS.get(name)
        gt.TOOLS[name] = fn
    try:
        ag.run_turn(text)
    finally:
        for name, fn in saved.items():
            if fn is not None:
                gt.TOOLS[name] = fn
    calls = ag.ollama.calls
    return {
        "calls": len(calls),
        # first call pays for the system prompt; later ones reuse it
        "fresh": sum(c["system"] for c in calls[:1]) +
                 sum(c["tail"] for c in calls),
        "cached": sum(c["tail"] for c in calls),
        # A call whose system message DIFFERS from the first one throws
        # ollama's cached prefix away and re-prefills the lot. Sending
        # the SAME system message again is free - that is the whole
        # point of the cache.
        "system_resends": sum(1 for c in calls[1:]
                              if c["sys_text"] != calls[0]["sys_text"]),
        "out": sum(c["out"] for c in calls),
    }


REPORT = []

# ---- 1. pure conversation: no tool has any business running ----------
chat = measure("hi", ['{"tool":"answer","args":{"text":"Hey. What do you '
                      'need?"}}'])
REPORT.append(("greeting", chat))
check(chat["calls"] == 1,
      "a greeting took %d model calls" % chat["calls"])

talk = measure("what do you make of all this ai hype then",
               ['{"tool":"answer","args":{"text":"Plenty of it is real and '
                'plenty is not. The interesting part is..."}}'])
REPORT.append(("open conversation", talk))
check(talk["calls"] == 1,
      "open conversation took %d model calls" % talk["calls"])

# ---- 2. the news, which is what he actually wants it for -------------
news = measure("whats the news",
               ['{"tool":"answer","args":{"text":"Three things worth '
                'knowing today. First..."}}'],
               tools={"news": lambda a, ag: NEWS})
REPORT.append(("the news", news))
check(news["calls"] <= 2,
      "the news took %d model calls" % news["calls"])

# ---- 3. the weather --------------------------------------------------
weather = measure("whats the weather",
                  ['{"tool":"answer","args":{"text":"14 and raining. '
                   'Take a coat."}}'],
                  tools={"weather": lambda a, ag: "place: Dublin\ntemp_c: 14"})
REPORT.append(("the weather", weather))

# =====================================================================
# THE BUDGETS
#
# Set from what the code does today, with a little headroom. They exist
# so that a future addition to the prompt has to be a DECISION rather
# than an accident: if a change pushes past one of these, either it is
# worth the seconds or it is not, but nobody gets to find out by
# accident on his laptop.
# =====================================================================
BUDGET = {
    # a greeting must never pay for a tool it does not use
    "greeting":          {"calls": 1, "cached": 60},
    "open conversation": {"calls": 1, "cached": 80},
    "the news":          {"calls": 2, "cached": 900},
    "the weather":       {"calls": 2, "cached": 400},
}
for name, got in REPORT:
    want = BUDGET.get(name)
    if not want:
        continue
    check(got["calls"] <= want["calls"],
          "%s: %d model calls, budget is %d"
          % (name, got["calls"], want["calls"]))
    check(got["cached"] <= want["cached"],
          "%s: %d tokens of per-call prefill, budget is %d"
          % (name, got["cached"], want["cached"]))

# ---- 4. the system prompt is paid once, not once per call ------------
for name, got in REPORT:
    check(got["system_resends"] == 0,
          "%s re-sends a different system message %d times; each one "
          "throws away ollama's cached prefix and re-prefills the lot"
          % (name, got["system_resends"]))

# ---- 5. and the prompt itself has a ceiling --------------------------
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), DummyTTS())
ag.refresh_prompt()
prompt_tokens = toks(ag.system_message())
spec_tokens = toks(gt.TOOL_SPEC)
check(prompt_tokens <= 3300,
      "the system prompt is %d tokens; every session pays that once "
      "before George says a word" % prompt_tokens)

print("turn cost (tokens, ~4 chars each)")
print("  %-20s %6s %8s %8s %8s" %
      ("", "calls", "fresh", "cached", "written"))
for name, got in REPORT:
    print("  %-20s %6d %8d %8d %8d"
          % (name, got["calls"], got["fresh"], got["cached"], got["out"]))
print("  system prompt %d tokens (tool spec %d of it), paid once a session"
      % (prompt_tokens, spec_tokens))
print("turn-cost checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
