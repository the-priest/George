"""The trace: what George actually did.

Every bug in this project so far was found by him screenshotting the
window and me guessing backwards from what was on it. The tool cards
show WHICH tool ran. They do not show the arguments, what came back, or
how long it took -- which is precisely the information that would have
shortened five separate bug hunts.

Constraints that matter as much as the feature:
  * bounded, so a long session cannot eat memory
  * in memory only, so it cannot leak his paths to disk
  * and it must NEVER break a turn, whatever is thrown at it
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


# -- bounded ------------------------------------------------------------
tr = gt.Trace()
tr.new_turn()
for i in range(2000):
    tr.add("tool", "t%d" % i, "x" * 50)
check(len(tr.rows()) <= tr.LIMIT,
      "the trace grew to %d rows unbounded" % len(tr.rows()))

# -- it must never break a turn ----------------------------------------
tr = gt.Trace()
tr.new_turn()
for junk in (None, object(), 12345, b"bytes", ["a"], {"k": "v"}):
    try:
        tr.add("tool", junk, junk, ms=junk, ok=junk)
    except Exception as exc:
        FAILS.append("tracing raised on %r: %r" % (type(junk), exc))
check(tr.summary(), "the trace could not render after junk input")

# -- turns are separable ------------------------------------------------
tr = gt.Trace()
tr.new_turn()
tr.add("tool", "a")
tr.new_turn()
tr.add("tool", "b")
check(len(tr.rows(1)) == 1 and tr.rows(1)[0].name == "a",
      "turn 1 rows are wrong")
check(len(tr.rows(2)) == 1 and tr.rows(2)[0].name == "b",
      "turn 2 rows are wrong")
check(len(tr.rows()) == 2, "rows() with no turn should return everything")


# -- END TO END: a real turn must be reconstructable from the trace ----
class TTS:
    def speak(self, t):
        pass

    def stop(self):
        pass


class Mock:
    def __init__(self, replies):
        self.replies = list(replies)

    def alive(self):
        return True

    def resolve_model(self):
        return ("qwen3:4b", "")

    def abort(self):
        pass

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        return self.replies.pop(0) if self.replies else ""


gt.TOOLS["news"] = lambda a, ag: "5 headlines retrieved from 7 feeds."
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), TTS())
ag.ollama = Mock([json.dumps({"tool": "answer",
                              "args": {"text": "Three stories up top."}}),
                  json.dumps({"supported": True})])
ag.on_final = lambda s: None
ag.run_turn("whats the news")

rows = ag.trace.rows()
kinds = [r.kind for r in rows]
check("note" in kinds, "what he typed was not recorded")
check("route" in kinds, "the router decision was not recorded")
check("tool" in kinds, "the tool call was not recorded")
check("model" in kinds, "the model call was not recorded")

# the ARGUMENTS and the RESULT are the whole point
tool_rows = [r for r in rows if r.kind == "tool"]
check(tool_rows, "no tool row at all")
check(any("headlines retrieved" in r.detail for r in tool_rows),
      "the tool's actual output is not in the trace: %r"
      % [r.detail[:60] for r in tool_rows])
check(any(r.ms >= 0 for r in rows), "no timings recorded")

summary = ag.trace.summary()
check("whats the news" in summary, "the summary omits what he asked")
check("news" in summary, "the summary omits the tool")
check("total" in summary, "the summary has no total")

# -- a FAILED tool must be marked as failed ----------------------------
gt.TOOLS["show"] = lambda a, ag: ("could not open https://x on screen: "
                                  "xdg-open exited 3")
ag2 = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), TTS())
ag2.ollama = Mock([])
ag2.trace.new_turn()
ag2.call_tool("show", {"url": "https://x"})
rows = [r for r in ag2.trace.rows() if r.kind == "tool"]
check(rows and not rows[-1].ok,
      "a failed tool was recorded as a success")
check("FAILED" in ag2.trace.summary(),
      "the summary does not flag the failure")

# an unrepairable argument is a failure too, and must say what was wrong
ag3 = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), TTS())
ag3.trace.new_turn()
ag3.call_tool("show", {"foo": 1, "bar": 2})
rows = [r for r in ag3.trace.rows() if r.kind == "tool"]
check(rows and not rows[-1].ok, "a bad-args call was not marked failed")
check("url" in rows[-1].detail,
      "the trace does not say which argument was missing")

# -- the summary is safe to hand to someone ----------------------------
check("nothing recorded yet" in gt.Trace().summary(),
      "an empty trace does not say so")

print("trace checks; failures: %d  (cap %d rows)" % (len(FAILS),
                                                     gt.Trace.LIMIT))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
