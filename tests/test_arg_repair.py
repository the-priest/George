"""Repairing tool arguments instead of retrying blind.

Constrained decoding pins the OUTER shape -- one object, a real tool
name, an args object -- and deliberately leaves `args` free-form,
because a schema strict enough for 33 tools would make an unlisted key
impossible rather than merely wrong.

So the inside still needs help. A 4B reaches for the obvious synonym:
`link` for `url`, `q` for `query`, `cmd` for `command`. It flattens. It
nests. It passes a bare string where a dict belongs.

All of that is repairable WITHOUT ASKING THE MODEL AGAIN, and that is
the whole point: a round trip on CPU is seconds, a rename is a dict
lookup. What cannot be repaired gets an error naming the exact key that
was wanted, so the retry is informed rather than another guess.

The hard rule: repair NEVER invents a value. It renames keys and lifts
a bare value into the slot the tool wants. Nothing else.
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


def fixed(tool, args):
    return gt.repair_args(tool, args)[0]


# -- the synonyms a small model actually reaches for --------------------
CASES = [
    ("show", {"link": "http://x"}, {"url": "http://x"}),
    ("show", {"address": "http://x"}, {"url": "http://x"}),
    ("open_page", {"website": "http://x"}, {"url": "http://x"}),
    ("web_search", {"q": "cats"}, {"query": "cats"}),
    ("web_search", {"keywords": "cats"}, {"query": "cats"}),
    ("research", {"topic": "cats"}, {"query": "cats"}),
    ("run", {"cmd": "ls"}, {"command": "ls"}),
    ("run", {"exec": "ls"}, {"command": "ls"}),
    ("code", {"lang": "python", "code": "print(1)"},
     {"language": "python", "source": "print(1)"}),
    ("read_file", {"file": "/tmp/a"}, {"path": "/tmp/a"}),
    ("write_file", {"file": "/tmp/a", "content": "hi"},
     {"path": "/tmp/a", "text": "hi"}),
    ("list_dir", {"folder": "/tmp"}, {"path": "/tmp"}),
    ("find", {"glob": "*.py", "dir": "/tmp"},
     {"pattern": "*.py", "path": "/tmp"}),
    ("calc", {"equation": "2+2"}, {"expression": "2+2"}),
    ("timer", {"duration": 60}, {"seconds": 60}),
    ("launch", {"program": "gedit"}, {"app": "gedit"}),
    ("weather", {"city": "Dublin"}, {"location": "Dublin"}),
    ("answer", {"message": "hi"}, {"text": "hi"}),
    ("say", {"content": "hi"}, {"text": "hi"}),
    ("pkg", {"verb": "search", "name": "ripgrep"},
     {"action": "search", "package": "ripgrep"}),
    ("see", {"prompt": "what is this"}, {"question": "what is this"}),
]
for tool, given, want in CASES:
    got = fixed(tool, given)
    check(got == want, "%s: %r -> %r, wanted %r" % (tool, given, got, want))

# -- shape problems, not just naming ------------------------------------
check(fixed("show", "http://y") == {"url": "http://y"},
      "a bare string was not lifted into the required key")
check(fixed("calc", 42) == {"expression": 42},
      "a bare number was not lifted")
check(fixed("open_page", {"args": {"url": "http://z"}}) ==
      {"url": "http://z"}, "a nested args object was not unwrapped")
check(fixed("run", {"parameters": {"command": "ls"}}) == {"command": "ls"},
      "a nested parameters object was not unwrapped")
check(fixed("show", {"somekey": "http://w"}) == {"url": "http://w"},
      "the only candidate value was not used for the required key")

# -- IT MUST NOT INVENT ANYTHING ----------------------------------------
check(fixed("show", {}) == {}, "repair invented args out of nothing")
check(fixed("run", {"a": "x", "b": "y"}) == {"a": "x", "b": "y"},
      "repair guessed between two candidates")
check("url" not in fixed("show", {"count": 3, "depth": 2}),
      "repair guessed a url from unrelated numbers")
# a correct call must be left completely alone
for tool, args in (("show", {"url": "http://x"}),
                   ("run", {"command": "ls"}),
                   ("code", {"language": "python", "source": "x"})):
    check(fixed(tool, args) == args,
          "%s: a correct call was altered: %r" % (tool, fixed(tool, args)))
# an alias must never clobber a key that is already right
check(fixed("show", {"url": "right", "link": "wrong"})["url"] == "right",
      "an alias overwrote the correct key")

# -- the complaint must be actionable -----------------------------------
msg = gt.missing_arg_message("show", {"foo": 1, "bar": 2})
check("url" in msg, "the complaint does not name the required key")
check("foo" in msg and "bar" in msg,
      "the complaint does not say what was actually sent")
check('"tool": "show"' in msg,
      "the complaint does not show the correct call shape")
check(gt.missing_arg_message("show", {"url": "x"}) == "",
      "a valid call produced a complaint")
check(gt.missing_arg_message("system", {}) == "",
      "a tool with no required arg produced a complaint")

# -- every alias must point at a key the tool actually uses -------------
for tool, aliases in gt.ARG_ALIASES.items():
    check(tool in gt.TOOLS or tool == "answer",
          "ARG_ALIASES has an entry for unknown tool %r" % tool)
    for wrong, right in aliases.items():
        check(wrong != right, "%s: alias %r points at itself" % (tool, wrong))
for tool, want in gt.ARG_REQUIRED.items():
    check(tool in gt.TOOLS or tool == "answer",
          "ARG_REQUIRED has an entry for unknown tool %r" % tool)


# -- END TO END: a wrong key must not cost a round trip -----------------
class Ag:
    cfg = dict(gc.DEFAULTS)
    confirm_elapsed = 0.0

    def step(self, *a):
        pass

    def tool_card(self, *a):
        pass


seen = {}
gt.TOOLS["show"] = lambda args, ag: seen.update(args) or "opened"
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(),
              type("T", (), {"speak": lambda s, t: None,
                             "stop": lambda s: None})())
out = ag.call_tool("show", {"link": "http://example.invalid"})
check(seen.get("url") == "http://example.invalid",
      "call_tool did not repair the argument before running: %r" % seen)
check(out == "opened", "the repaired call did not run: %r" % out)

# and an unrepairable one must come back with the precise complaint
out = ag.call_tool("show", {"foo": 1, "bar": 2})
check("needs `url`" in out,
      "an unrepairable call did not name the missing key: %r" % out[:120])

print("arg-repair checks; failures: %d  (%d tools aliased, %d required)"
      % (len(FAILS), len(gt.ARG_ALIASES), len(gt.ARG_REQUIRED)))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
