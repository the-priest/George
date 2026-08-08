"""The system prompt.

It had grown by accretion: every fix appended another bullet to a flat
RULES list until it was a wall of overlapping instructions with no
structure. A 4B model reads that the way you would read a contract.

It is now six numbered sections with WORKED EXAMPLES of the exact JSON
protocol, because a small model copies patterns far more reliably than
it follows prose. This file pins the things that must be in it, the
things that must not, and a ceiling so it cannot creep back into a wall
of text.
"""
import os
import re
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


def agent(**cfg_over):
    cfg = dict(gc.DEFAULTS)
    cfg.update(cfg_over)
    return gt.Agent(cfg, gt.MemoryStore(),
                    type("T", (), {"speak": lambda s, t: None,
                                   "stop": lambda s: None})())


ag = agent()
P = ag.system_message()

# -- nothing unsubstituted, nothing malformed --------------------------
check("{name}" not in P and "{tools}" not in P and "{extra}" not in P,
      "an unsubstituted placeholder is left in the prompt")
# NOTE: "}}" is legitimate here -- it closes nested JSON like
# {"tool":"answer","args":{"text":"..."}}. What must NOT survive is a
# doubled brace with no JSON around it, which is what a missed .format
# escape looks like.
check(not re.search(r"\{\{\s*[\"a-z]", P),
      "an escaped opening brace leaked through .format into the prompt")
check("his's" not in P and "the user'ss" not in P,
      "the possessive is malformed with no user_name set")
check("the user's desktop assistant" in P,
      "an unset name does not produce a clean sentence")

ag2 = agent(user_name="Luka")
check("Luka's desktop assistant" in ag2.system_message(),
      "a set name is not used in the prompt")

# -- the JSON protocol must be shown, not just described ---------------
check(P.count('{"tool": "answer", "args": {"text":') >= 2,
      "the answer object is not shown as a literal example")
check('{"tool": "disk", "args": {}}' in P,
      "no worked example of calling a tool")
check("WORKED EXAMPLE" in P, "the worked examples are gone")
for label in ("EXAMPLE A", "EXAMPLE B", "EXAMPLE C", "EXAMPLE D"):
    check(label in P, "%s is missing" % label)

# -- the router contract has to be explained to the model --------------
check("GUIDANCE" in P and "OBSERVATION" in P,
      "the prompt never explains the prefetched-observation convention")
check("Do NOT call it again" in P,
      "the model is not told to skip a tool that was already run")

# -- the hard-won rules, each still present ----------------------------
must_say = {
    "never claim the screen":
        "NEVER tell him something is on his screen",
    "not every message needs a tool":
        "Not every message needs a tool",
    "no invented output":
        "Never invent tool output",
    "no canned answers":
        'Never answer with "Done."',
    "spoken aloud":
        "READ ON SCREEN AND SPOKEN ALOUD",
}
for name, needle in must_say.items():
    check(needle in P, "the prompt lost: %s (%r)" % (name, needle))

# -- the shell rules belong to FULL mode, where shell tools exist -------
# `run`, `code` and hand-written pacman lines are not advertised in
# simple mode, so the warnings about them are checked where they apply.
# They must not be lost from full mode: a bare `pacman -S` is a partial
# upgrade and breaks an Arch box.
FULL = agent(mode="full").system_message()
full_must_say = {
    "one command at a time": "One shell command at a time",
    "pacman not bare -S": "never a bare -S",
    "AUR needs paru": "pacman CANNOT install AUR packages",
    "code can be run": "You CAN write files and run programs",
}
for name, needle in full_must_say.items():
    check(needle in FULL, "full mode lost: %s (%r)" % (name, needle))

# -- and simple mode must not CONTRADICT its own tool list -------------
# It used to say "You CAN write files and run programs" while `code` and
# `run` were absent from the list above it. A 4B resolves that badly.
check("You CAN write files and run programs" not in P,
      "simple mode offers to run programs it was not given the tools for")
check("`code`" not in P.split("HARD RULES")[-1] or "switched off" in P,
      "simple mode still points at the code tool")
check("Settings" in P,
      "simple mode does not tell him where to turn the rest back on")

# -- every registered tool must be documented, and vice versa ----------
documented = set(re.findall(r"^(\w+)\s+\{", gt.TOOL_SPEC, re.M))
undocumented = sorted(t for t in gt.TOOLS if t not in documented)
check(not undocumented,
      "tools the model is never told about: %s" % undocumented)
phantom = sorted(t for t in documented
                 if t not in gt.TOOLS and t != "answer")
check(not phantom, "tools documented but not registered: %s" % phantom)
check("answer" in documented, "the answer action is not documented")

# -- the catalogue must be grouped, not a flat list --------------------
groups = re.findall(r"^--- (.+?) ---$", gt.TOOL_SPEC, re.M)
check(len(groups) >= 6,
      "the tool catalogue is not grouped by job: %s" % groups)
check(any("IN FRONT OF HIM" in g.upper() or "SCREEN" in g.upper()
          for g in groups),
      "no group makes clear which tools put things on screen: %s" % groups)

# -- size ceiling: local tokens are free, a small model's attention is not
tokens = len(P) // 4
check(tokens < 3200,
      "the prompt has grown to ~%d tokens; a 4B model starts losing the "
      "middle of it" % tokens)
check(tokens > 900, "the prompt is suspiciously small: ~%d tokens" % tokens)

# -- it must be ASCII: the sheet and the log both assume it ------------
try:
    P.encode("ascii")
except UnicodeEncodeError as exc:
    FAILS.append("non-ascii in the prompt: %s" % exc)

# -- and it must build under every persona -----------------------------
for persona in gt.PERSONAS:
    p = agent(persona=persona).system_message()
    check("HOW A TURN WORKS" in p,
          "persona %r produces a prompt with no protocol section" % persona)

print("prompt checks; failures: %d  (~%d tokens, %d tools, %d groups)"
      % (len(FAILS), tokens, len(gt.TOOLS), len(groups)))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
