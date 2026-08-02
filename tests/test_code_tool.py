"""George said he could not run code, while holding the tools to do it.

Asked for a Python program that prints an ASCII bee, he replied "I can't
print ASCII art or run code directly" and pasted two print statements.
He had `write_file` and `run` the whole time. Nothing told the model
they COMPOSE, and a 4B will not work that out under pressure.

So the composition is a tool now: `code` writes the script AND runs it,
behind one confirmation that shows the source.
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


class Ag:
    cfg = dict(gc.DEFAULTS)
    asked = None

    def __init__(self, allow=True):
        self.allow = allow
        self.cards = []

    def step(self, *a):
        pass

    def tool_card(self, *a):
        self.cards.append(a)

    def confirm(self, title, body):
        Ag.asked = (title, body)
        return self.allow


# -- it actually runs, and returns the REAL output ---------------------
ag = Ag()
BEE = ("print('   __')\n"
       "print('  / _)  bzzz')\n"
       "print(' (  |   hello world')\n")
out = gt.tool_code({"language": "python", "source": BEE}, ag)
check("bzzz" in out and "hello world" in out,
      "the script's real stdout is not in the observation: %r" % out[:200])
check("Exit code 0" in out, "a clean run did not report exit 0")
check("STDOUT" in out, "stdout is not labelled")
check(".py" in out, "the saved path is not reported")

# the source must be shown in the confirmation, not just the filename
check(Ag.asked and "bzzz" in Ag.asked[1],
      "he is asked to approve a script without seeing its source")

# -- exactly ONE confirmation for the whole job ------------------------
count = {"n": 0}


class Counting(Ag):
    def confirm(self, title, body):
        count["n"] += 1
        return True


gt.tool_code({"language": "python", "source": "print(1)"}, Counting())
check(count["n"] == 1,
      "asked %d times for one intention; that trains him to click through"
      % count["n"])

# -- declining must stop it, and must not lose his work ----------------
denied = Ag(allow=False)
out = gt.tool_code({"language": "python", "source": "print('x')"}, denied)
check("declined" in out.lower(), "a refusal was not reported as one")
check("Do not retry" in out, "the model is not told to stop retrying")
check("in your answer" in out,
      "a declined script leaves him with nothing; show him the source")

# -- failures are reported honestly ------------------------------------
out = gt.tool_code({"language": "python",
                    "source": "raise ValueError('nope')"}, Ag())
check("Exit code 1" in out, "a crash did not report a non-zero exit")
check("ValueError" in out and "STDERR" in out,
      "the traceback did not reach the model: %r" % out[:200])

# -- no output is stated, not implied ----------------------------------
out = gt.tool_code({"language": "python", "source": "pass"}, Ag())
check("no output" in out.lower(), "a silent script did not say so")

# -- guardrails --------------------------------------------------------
check("cannot run" in gt.tool_code({"language": "cobol", "source": "x"},
                                   Ag()),
      "an unsupported language was not refused")
check("no source" in gt.tool_code({"language": "python", "source": "  "},
                                  Ag()),
      "an empty script was not refused")

# -- bash works too ----------------------------------------------------
out = gt.tool_code({"language": "bash", "source": "echo bzzz"}, Ag())
check("bzzz" in out, "a bash script did not run: %r" % out[:160])

# -- the model must be TOLD it can do this -----------------------------
ag2 = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(),
               type("T", (), {"speak": lambda s, t: None,
                              "stop": lambda s: None})())
P = ag2.system_message()
check("code" in gt.TOOLS, "the code tool is not registered")
check("Never say you" in P and "cannot run code" in P,
      "the prompt does not forbid claiming it cannot run code")
check("You CAN write files and run programs" in P,
      "the prompt never states the capability positively")
check("code" in gt.tool_schema()["properties"]["tool"]["enum"],
      "the code tool is not in the decoding schema")

print("code-tool checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
