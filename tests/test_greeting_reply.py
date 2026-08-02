"""He said "hi" and George said "Done." -- or worse, read a line of raw
tool output at him.

Two separate faults produced that:

  1. The final-answer "repair" replaced ANY answer of 20 characters or
     fewer with the first line of the last tool observation.  A good
     reply to "hi" is short by design, so every greeting got thrown
     away and swapped for machine output.
  2. Nothing in the prompt said a tool was optional, so the model
     reached for one to say hello, which is what produced an
     observation for fault 1 to paste in.

This file pins the behaviour both ways: short real answers survive
untouched, and a genuine canned grunt after a tool ran gets repaired by
ASKING AGAIN rather than by pasting the observation.
"""
import os
import sys
import tempfile

_t = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = _t + "/cfg"
os.environ["XDG_DATA_HOME"] = _t + "/data"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_core as gc            # noqa: E402
import george_tools as gt           # noqa: E402
from george_tools import Agent      # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


class DummyTTS:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        pass


class MockOllama:
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


def agent_with(replies):
    cfg = dict(gc.DEFAULTS)
    ag = Agent(cfg, gt.MemoryStore(), DummyTTS())
    ag.ollama = MockOllama(replies)
    got = {"final": None}
    ag.on_final = lambda s: got.update({"final": s})
    return ag, got


# -- 1. a short, real answer to a greeting must survive untouched -------
for reply in ("Hey. What do you need?", "Hi Luka.", "Morning.",
              "Yes, it's fine.", "It's 14 degrees."):
    ag, got = agent_with(['{"tool":"answer","args":{"text":%s}}'
                          % gt.json.dumps(reply)])
    ag.run_turn("hi")
    check(got["final"] == reply,
          "short greeting mangled: %r -> %r" % (reply, got["final"]))

# -- 1b. THE REPORTED CASE, end to end: the model reaches for a tool on a
#        greeting (7B models do), then gives a short but perfectly good
#        reply.  That reply must reach him intact.  Under the old
#        length-based rule it was replaced by the news observation.
gt.TOOLS["news"] = lambda args, ag: (
    "Headlines are now on his screen in the News panel.\n"
    "1. Budget talks run into a second night")
ag, got = agent_with([
    '{"tool":"news","args":{"topic":"today"}}',
    '{"tool":"answer","args":{"text":"Hey. What is up?"}}',
])
ag.run_turn("hi")
check(got["final"] == "Hey. What is up?",
      "greeting reply replaced by tool output: %r" % got["final"])
check("Headlines are now" not in (got["final"] or ""),
      "observation text pasted over a greeting")

# -- 2. a greeting that answers straight away must not run a tool ------
ag, got = agent_with(['{"tool":"answer","args":{"text":"Hey. What do you need?"}}'])
ag.run_turn("hi")
check(ag.ollama.calls == 1,
      "a direct answer took %d model calls, expected 1" % ag.ollama.calls)

# -- 3. a canned grunt with NO tool run is left alone -------------------
ag, got = agent_with(['{"tool":"answer","args":{"text":"Ok."}}'])
ag.run_turn("thanks")
check(got["final"] == "Ok.",
      "canned reply with no tool was rewritten: %r" % got["final"])

# -- 4. a canned grunt AFTER a tool ran is repaired by asking again,
#       and the repair must be prose -- never the raw observation -------
gt.TOOLS["news"] = lambda args, ag: (
    "Headlines are now on his screen in the News panel.\n"
    "1. Budget talks run into a second night")
ag, got = agent_with([
    '{"tool":"news","args":{"topic":"today"}}',
    '{"tool":"answer","args":{"text":"Done."}}',
    "Three stories up top - the budget talks are the big one.",
])
ag.run_turn("what's the news?")
check(got["final"] == "Three stories up top - the budget talks are the big one.",
      "canned final not repaired by retry: %r" % got["final"])
check("OBSERVATION" not in (got["final"] or ""),
      "raw observation leaked into the reply")

# -- 5. if the retry is ALSO useless, fall back rather than ship "Done."
ag, got = agent_with([
    '{"tool":"news","args":{"topic":"today"}}',
    '{"tool":"answer","args":{"text":"Done."}}',
    "ok",
])
ag.run_turn("what's the news?")
check(got["final"] not in ("Done.", "ok"),
      "no fallback when the retry was also canned: %r" % got["final"])
check("[REDACTED_PATH]" not in (got["final"] or ""),
      "path redaction is still mangling his own local paths")

# -- 6. the canned matcher itself ---------------------------------------
for grunt in ("Done.", "done", "OK!", "  All done  ", "Task complete."):
    check(Agent._is_canned(grunt), "not detected as canned: %r" % grunt)
for real in ("Hi.", "It's 14 degrees.", "Yes, it's fine.", "No.",
             "Done deal - I moved it to the archive."):
    check(not Agent._is_canned(real), "wrongly detected as canned: %r" % real)

# -- 7. the prompt has to actually say a tool is optional ---------------
ag, _ = agent_with([])
prompt = ag.system_message().lower()
check("not every message needs a tool" in prompt,
      "prompt never tells the model it can answer directly")

print("greeting/final checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
