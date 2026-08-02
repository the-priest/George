"""The reply firewall.

At a plain "hi geroge how are you bro", qwen3:4b emitted eight hundred
words of scratchpad -- "I must respond as George", "let me craft a
response", "Final decision:" -- and George printed ALL OF IT as the
answer.

The loop had exactly one fallback: no JSON found, so treat the whole
raw reply as prose and show it. That fallback is the bug. A small model
does not reliably obey "output only JSON", so the loop cannot assume
that anything which is not JSON is a reply.

The firewall recognises a scratchpad, salvages the answer the model
settled on, and only if that fails asks again for the JSON.
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


# The real thing, from his screenshot.
LEAK = '''We are in a new conversation. The user says: "hi geroge how are you bro"
I must respond as George, the priest's desktop assistant.
Rules:
- Openers I never use: "Certainly!" "I'd be happy to help!" "Great question!"
- I am unhurried and precise, dry wit used sparingly and never at his expense.
Since the user is greeting me and asking how I am, I don't need to use a tool.
I am George, the desktop assistant. I don't have feelings.
I'll craft a response that is brief and informative.
Example: "Hey. I'm running smoothly. What do you need?"
But note: The user said "hi geroge" (typo? but I'll go with it).
However, the rules say: "Do not narrate your plan before doing it"
Also, the response must be in the form of a JSON object with tool: "answer"
Final decision:
Let me stick to: "Hey bro. I'm running smoothly. What do you need?"'''

check(gt.looks_like_scratchpad(LEAK), "the real leak was not detected")
check(gt.salvage_answer(LEAK) == "Hey bro. I'm running smoothly. "
      "What do you need?",
      "wrong salvage: %r" % gt.salvage_answer(LEAK))

# -- other shapes of scratchpad ---------------------------------------
for bad in (
    'The user says "what time is it". I must check the CONTEXT block. '
    'Let me think. Final answer: "It is half four."',
    'Okay, so the user wants the news. The rules say I should use the '
    'news tool. But wait, the observation is already here.',
    'I need to output a JSON object with "tool" and "args". Let me '
    'craft that now.',
):
    check(gt.looks_like_scratchpad(bad),
          "missed a scratchpad: %r" % bad[:60])

# -- ordinary replies must NOT be flagged -----------------------------
for good in (
    "Hey. What do you need?",
    "Disk is the problem, not memory. /home is at 91%.",
    "The user table has three columns; the rules say each row must be "
    "unique, so let me know if you want me to add a constraint.",
    "I think the simplest fix is a unique index on that column.",
    "It's 14 degrees and raining in Dublin.",
    "",
):
    check(not gt.looks_like_scratchpad(good),
          "a normal reply was flagged as scratchpad: %r" % good[:60])

# -- salvage must refuse to return rubbish ----------------------------
check(gt.salvage_answer("no quotes here at all") == "",
      "salvage invented an answer from nothing")
check(gt.salvage_answer('The rules say "I must respond as George"') == "",
      "salvage returned the model quoting its own rules back")
check(gt.salvage_answer('I will use "tool" and "args" keys.') == "",
      "salvage returned protocol vocabulary as a reply")

# -- END TO END: a leaking model must not reach him --------------------
class DummyTTS:
    def speak(self, t):
        pass

    def stop(self):
        pass


class MockOllama:
    def __init__(self, replies):
        self.replies = list(replies)

    def alive(self):
        return True

    def resolve_model(self):
        return ("mock:1", "")

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        return self.replies.pop(0) if self.replies else ""


def run(replies, text="hi"):
    ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), DummyTTS())
    ag.ollama = MockOllama(replies)
    got = {}
    ag.on_final = lambda s: got.update({"t": s})
    ag.run_turn(text)
    return got.get("t", "")


out = run([LEAK])
check(out == "Hey bro. I'm running smoothly. What do you need?",
      "the leak reached him: %r" % out[:90])
check("I must respond" not in out and "Final decision" not in out,
      "scratchpad text survived into the answer")
check(len(out) < 200, "the answer is still a monologue (%d chars)" % len(out))

# an unsalvageable scratchpad must trigger the retry, not a dump
UNSALVAGEABLE = ("The user says something. I must think about this. "
                 "Let me check the rules. The rules say I should reply. "
                 "Final decision: I will reply now.")
out = run([UNSALVAGEABLE,
           '{"tool":"answer","args":{"text":"Hey. What do you need?"}}'])
check(out == "Hey. What do you need?",
      "the json-only retry did not recover: %r" % out[:90])

# and if even the retry fails, he gets a short honest line
out = run([UNSALVAGEABLE, UNSALVAGEABLE])
check(gt.looks_like_scratchpad(out) is False,
      "a doubly-failed turn still showed scratchpad: %r" % out[:90])
check(len(out) < 120, "the give-up message is not short: %r" % out[:120])

# a genuine prose answer with no JSON still goes straight through
out = run(["Hey. Running fine here. What do you need?"])
check(out == "Hey. Running fine here. What do you need?",
      "a normal prose reply was mangled: %r" % out)

print("firewall checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
