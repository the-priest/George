"""The verification pass.

A 4B is poor at being right first time and noticeably BETTER at judging
whether a specific sentence is supported by text sitting in front of it.
That asymmetry is the biggest unexploited lever in this project, and it
attacks the failure he has hit most: George stating things that are not
so.

The canonical case is his own screenshot. The news tool returned zero
headlines because the Reuters feed 404'd, and George said "News articles
are now on his screen." Nothing was on his screen, and the evidence said
so plainly.

After a tool-backed answer, one cheap constrained call asks whether the
answer is supported by the observations. If not, one repair. Never a
loop, never on chat, and it FAILS OPEN -- a verification layer that can
eat replies is worse than none.
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


class DummyTTS:
    def speak(self, t):
        pass

    def stop(self):
        pass


class ScriptedOllama:
    """Replies in order. Records what each call was asked to do."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def alive(self):
        return True

    def resolve_model(self):
        return ("mock:1", "")

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        self.calls.append({"schema": schema,
                           "text": "\n".join(m["content"] for m in messages)})
        return self.replies.pop(0) if self.replies else ""


def agent(replies, **over):
    cfg = dict(gc.DEFAULTS)
    cfg.update(over)
    ag = gt.Agent(cfg, gt.MemoryStore(), DummyTTS())
    ag.ollama = ScriptedOllama(replies)
    got = {}
    ag.on_final = lambda s: got.update({"t": s})
    return ag, got


# The news tool, returning what it really returns when every feed fails.
EMPTY_NEWS = ("2 of 7 feeds failed: Reuters World (HTTP Error 404); "
              "BBC (timed out)\nno headlines were retrieved")
gt.TOOLS["news"] = lambda args, ag: EMPTY_NEWS

LIE = "News articles are now on his screen."
TRUTH = ("I could not get the news - Reuters returned a 404 and the BBC "
         "feed timed out. Nothing was retrieved.")


# -- 1. THE REPORTED CASE: an unsupported claim is caught and repaired --
ag, got = agent([
    '{"tool":"news","args":{}}',                     # step 1: run the tool
    '{"tool":"answer","args":{"text":"%s"}}' % LIE,  # step 2: the lie
    json.dumps({"supported": False,
                "problem": "the evidence says nothing was retrieved and "
                           "nothing was opened"}),   # the check
    '{"tool":"answer","args":{"text":"%s"}}' % TRUTH,  # the repair
], router=False)
ag.run_turn("what's the news?")
check(got.get("t") == TRUTH,
      "the unsupported claim was not repaired: %r" % got.get("t"))
check("on his screen" not in (got.get("t") or ""),
      "the screen claim survived verification")

# the check must have been constrained, and must have SEEN the evidence.
# Guarded: if verification never ran at all, say so rather than dying on
# an index error.
if len(ag.ollama.calls) < 3:
    FAILS.append("verification never ran: only %d model calls"
                 % len(ag.ollama.calls))
else:
    verdict_call = ag.ollama.calls[2]
    check(verdict_call["schema"] == gt.VERDICT_SCHEMA,
          "the verification call was not constrained to a verdict schema")
    check("404" in verdict_call["text"],
          "the checker was not shown the evidence")
    check(LIE in verdict_call["text"],
          "the checker was not shown the answer it is judging")

# -- 2. a supported answer must pass through untouched -----------------
ag, got = agent([
    '{"tool":"news","args":{}}',
    '{"tool":"answer","args":{"text":"%s"}}' % TRUTH,
    json.dumps({"supported": True}),
], router=False)
ag.run_turn("what's the news?")
check(got.get("t") == TRUTH,
      "a supported answer was altered: %r" % got.get("t"))
check(len(ag.ollama.calls) == 3,
      "a supported answer cost %d calls, expected 3" % len(ag.ollama.calls))

# -- 3. NO tool ran: nothing to verify against, so do not try ----------
ag, got = agent([
    '{"tool":"answer","args":{"text":"Hey. What do you need?"}}',
])
ag.run_turn("hi")
check(got.get("t") == "Hey. What do you need?",
      "a greeting was mangled: %r" % got.get("t"))
check(len(ag.ollama.calls) == 1,
      "a greeting triggered verification (%d calls); with no evidence "
      "this is just a second opinion" % len(ag.ollama.calls))

# -- 4. IT MUST FAIL OPEN ----------------------------------------------
# unparseable verdict -> ship the original
ag, got = agent([
    '{"tool":"news","args":{}}',
    '{"tool":"answer","args":{"text":"%s"}}' % TRUTH,
    "I think probably yes?",
], router=False)
ag.run_turn("news")
check(got.get("t") == TRUTH,
      "an unparseable verdict ate the answer: %r" % got.get("t"))


# the checker raising -> ship the original
class Exploding(ScriptedOllama):
    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        if schema is gt.VERDICT_SCHEMA:
            raise RuntimeError("model died")
        return super().chat_stream(messages, on_token, stop, on_stall,
                                   model, schema)


cfg = dict(gc.DEFAULTS)
cfg["router"] = False
ag = gt.Agent(cfg, gt.MemoryStore(), DummyTTS())
ag.ollama = Exploding(['{"tool":"news","args":{}}',
                       '{"tool":"answer","args":{"text":"%s"}}' % TRUTH])
got = {}
ag.on_final = lambda s: got.update({"t": s})
ag.run_turn("news")
check(got.get("t") == TRUTH,
      "a crashing verifier ate the answer: %r" % got.get("t"))

# a failed REPAIR must ship the original, not nothing
ag, got = agent([
    '{"tool":"news","args":{}}',
    '{"tool":"answer","args":{"text":"%s"}}' % LIE,
    json.dumps({"supported": False, "problem": "unsupported"}),
    "",                                   # repair produces nothing
], router=False)
ag.run_turn("news")
check(got.get("t") == LIE,
      "a failed repair lost the answer entirely: %r" % got.get("t"))

# -- 5. exactly ONE repair, never a loop -------------------------------
ag, got = agent([
    '{"tool":"news","args":{}}',
    '{"tool":"answer","args":{"text":"%s"}}' % LIE,
    json.dumps({"supported": False, "problem": "no"}),
    '{"tool":"answer","args":{"text":"Still wrong."}}',
    json.dumps({"supported": False, "problem": "still no"}),
], router=False)
ag.run_turn("news")
check(len(ag.ollama.calls) == 4,
      "verification looped: %d calls, expected 4" % len(ag.ollama.calls))
check(got.get("t") == "Still wrong.",
      "the single repair did not take: %r" % got.get("t"))

# -- 6. it must be switchable ------------------------------------------
ag, got = agent([
    '{"tool":"news","args":{}}',
    '{"tool":"answer","args":{"text":"%s"}}' % LIE,
], router=False, verify="off")
ag.run_turn("news")
check(len(ag.ollama.calls) == 2,
      "verify=off still ran the check (%d calls)" % len(ag.ollama.calls))
check(gc.CHOICES.get("verify") == ("on", "off"),
      "verify has no validated choices")
check(gc.coerce_config({"verify": "maybe"})["verify"] == "on",
      "a bad verify value is not repaired")

print("verification checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
