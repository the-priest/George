"""Whole sessions, replayed.

Every other test in here checks a part. This one plays complete turns --
what he typed, what the model said back, what the tools returned -- and
asserts WHAT REACHED THE SCREEN. That is the only thing he actually
experiences, and it is the layer where all five of his screenshot bug
reports lived while the rest of the suite stayed green.

Each scenario is drawn from something that really happened. The rule for
this file: **when something breaks, add the session here before fixing
it.** A scenario is cheap; a repeat of the same bug is not.

    SCENARIO = {
        "name":    what this is
        "says":    what he typed
        "replies": what the model produces, in order
        "tools":   stubbed tool results
        "expect":  assertions about what he ended up seeing
    }
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


def answer(text):
    return json.dumps({"tool": "answer", "args": {"text": text}})


def call(tool, **args):
    return json.dumps({"tool": tool, "args": args})


class TTS:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        pass


class Scripted:
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


# The real leak from his screenshot, trimmed but structurally identical.
SCRATCHPAD = '''We are in a new conversation. The user says: "hi geroge"
I must respond as George, the desktop assistant.
The rules say: Answer DIRECTLY when he wants a greeting.
I'll craft a response that is brief and informative.
Example: "Hey. I'm running smoothly. What do you need?"
Final decision:
Let me stick to: "Hey. I'm running smoothly. What do you need?"'''

# What the news tool really returns when the feeds are down.
DEAD_FEEDS = ("2 of 7 feeds failed: Reuters World (HTTP Error 404); "
              "BBC (timed out)\nno headlines were retrieved")


SCENARIOS = [
    {
        "name": "greeting costs one call and runs no tools",
        "says": "hi george",
        "replies": [answer("Hey. What do you need?")],
        "expect": {"final": "Hey. What do you need?",
                   "calls": 1, "tools": []},
    },
    {
        "name": "the scratchpad leak he screenshotted",
        "says": "hi geroge how are you bro",
        "replies": [SCRATCHPAD],
        "expect": {"final": "Hey. I'm running smoothly. What do you need?",
                   "not_in_final": ["I must respond", "Final decision",
                                    "the rules say"]},
    },
    {
        "name": "dead feeds are reported, not papered over",
        "says": "whats the news",
        "tools": {"news": DEAD_FEEDS},
        "replies": [answer("I could not get the news - Reuters returned a "
                           "404 and the BBC feed timed out."),
                    json.dumps({"supported": True})],
        "expect": {"tools": ["news"],
                   "in_final": ["404"],
                   "not_in_final": ["on his screen", "on your screen"]},
    },
    {
        "name": "claiming the screen gets caught and repaired",
        "says": "whats the news",
        "tools": {"news": DEAD_FEEDS},
        "replies": [answer("News articles are now on his screen."),
                    json.dumps({"supported": False,
                                "problem": "nothing was retrieved or "
                                           "opened"}),
                    answer("No luck - the feeds are down, nothing came "
                           "back.")],
        "expect": {"not_in_final": ["on his screen"],
                   "in_final": ["feeds are down"]},
    },
    {
        "name": "a slow box gets the pre-analysed verdict, not three dumps",
        "says": "why is my box so slow",
        "tools": {"diagnose": "VERDICT: the disk is 96% full.\n\n"
                              "VITALS: cpu 7%, disk 96%"},
        "replies": [answer("Your disk is the problem - it is 96% full."),
                    json.dumps({"supported": True})],
        "expect": {"tools": ["diagnose"], "in_final": ["96"]},
    },
    {
        "name": "it does not claim it cannot write code",
        "router": False,
        "says": "write me a python script that prints a bee",
        "tools": {"code": "Saved to /tmp/bee.py and ran it. Exit code 0.\n\n"
                          "STDOUT:\n  __\n / _)  bzzz"},
        "replies": [call("code", language="python", source="print('bzzz')"),
                    answer("Done - it prints:\n  __\n / _)  bzzz"),
                    json.dumps({"supported": True})],
        "expect": {"tools": ["code"], "in_final": ["bzzz"],
                   "not_in_final": ["I can't", "I cannot run",
                                    "unable to run"]},
    },
    {
        "name": "a declined confirmation is reported, not retried forever",
        "router": False,
        "says": "delete my downloads folder",
        "tools": {"run": "He declined. Do not retry it. Ask what he wants "
                         "instead."},
        "replies": [call("run", command="rm -rf ~/Downloads"),
                    answer("You declined that one. What did you want to "
                           "clear instead?"),
                    json.dumps({"supported": True})],
        "expect": {"in_final": ["declined"], "max_calls": 4},
    },
    {
        "name": "a tool that fails does not become a success story",
        "router": False,
        "says": "open the hacker news front page",
        "tools": {"show": "could not open https://news.ycombinator.com on "
                          "screen: xdg-open exited 3"},
        "replies": [call("show", url="https://news.ycombinator.com"),
                    answer("I could not open it - xdg-open failed with "
                           "exit 3. Is a default browser set?"),
                    json.dumps({"supported": True})],
        "expect": {"in_final": ["could not"],
                   "not_in_final": ["opened it", "it is open",
                                    "on your screen"]},
    },
    {
        "name": "a canned 'Done.' after a tool is repaired into an answer",
        "says": "whats the news",
        "tools": {"news": "3 headlines retrieved from 7 feeds.\n"
                          "1. [RTE] Budget talks run late"},
        "replies": [answer("Done."),
                    answer("Three stories - the budget talks are the big "
                           "one."),
                    json.dumps({"supported": True})],
        "expect": {"not_in_final": ["Done."], "in_final": ["budget"]},
    },
]


def run(scenario):
    cfg = dict(gc.DEFAULTS)
    # Router ON by default, because that is the real path he uses -- for
    # "whats the news" and "why is my box so slow" the tool is
    # prefetched and the model never chooses it. Scenarios that script
    # their own tool call set "router": False so the model's own
    # decision is the thing under test.
    cfg["router"] = bool(scenario.get("router", True))
    tts = TTS()
    ag = gt.Agent(cfg, gt.MemoryStore(), tts)
    ag.ollama = Scripted(scenario.get("replies", []))

    ran = []
    saved = {}
    for name, result in (scenario.get("tools") or {}).items():
        saved[name] = gt.TOOLS.get(name)
        gt.TOOLS[name] = (lambda res, nm: (
            lambda args, agent: (ran.append(nm), res)[1]))(result, name)

    seen = {"final": None}
    ag.on_final = lambda s: seen.update({"final": s})
    try:
        ag.run_turn(scenario["says"])
    finally:
        for name, orig in saved.items():
            if orig is not None:
                gt.TOOLS[name] = orig
            else:
                gt.TOOLS.pop(name, None)
    return seen["final"], ran, ag.ollama.calls, tts


for sc in SCENARIOS:
    label = sc["name"]
    try:
        final, ran, calls, tts = run(sc)
    except Exception as exc:
        FAILS.append("%s: raised %r" % (label, exc))
        continue

    exp = sc["expect"]
    final = final or ""

    if "final" in exp:
        check(final == exp["final"],
              "%s: final was %r, wanted %r" % (label, final[:80],
                                               exp["final"]))
    for needle in exp.get("in_final", []):
        check(needle.lower() in final.lower(),
              "%s: %r missing from what he saw: %r"
              % (label, needle, final[:110]))
    for needle in exp.get("not_in_final", []):
        check(needle.lower() not in final.lower(),
              "%s: %r reached him: %r" % (label, needle, final[:110]))
    if "tools" in exp:
        check(ran == exp["tools"],
              "%s: ran %s, expected %s" % (label, ran, exp["tools"]))
    if "calls" in exp:
        check(calls == exp["calls"],
              "%s: %d model calls, expected %d" % (label, calls,
                                                   exp["calls"]))
    if "max_calls" in exp:
        check(calls <= exp["max_calls"],
              "%s: %d model calls, more than %d" % (label, calls,
                                                    exp["max_calls"]))

    # Universal contracts, checked for EVERY scenario. These are the
    # things that must never be true no matter what the model did.
    check(final.strip(), "%s: he was shown nothing at all" % label)
    check(not gt.looks_like_scratchpad(final),
          "%s: scratchpad reached the screen: %r" % (label, final[:110]))
    check('"tool"' not in final and '"args"' not in final,
          "%s: raw protocol JSON reached the screen: %r"
          % (label, final[:110]))
    check("OBSERVATION" not in final,
          "%s: a raw observation reached the screen" % label)
    check(len(final) < 2000,
          "%s: the reply is a wall of text (%d chars)" % (label, len(final)))
    check(tts.spoken and tts.spoken[-1] == final,
          "%s: what was spoken differs from what was shown" % label)

print("session checks; failures: %d  (%d scenarios)"
      % (len(FAILS), len(SCENARIOS)))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
