"""The stop button has to actually stop things.

He pressed it and George carried on. Two reasons, and one of them was
introduced by the polish passes added in 3.7.0:

  1. `_repair_final` and `_verify_final` each make their OWN model call,
     and neither checked the stop flag. So stopping mid-answer still
     cost two more full round trips before anything happened -- on CPU,
     long enough for the button to look broken.
  2. Breaking out of the read loop is not enough on its own. The
     iterator blocks until the NEXT token arrives, so stop appeared dead
     for however long the model took to produce one more. Closing the
     socket makes the read fail immediately AND makes ollama notice
     nobody is listening, so it stops GENERATING instead of finishing an
     answer that was cancelled.

The contract: after stop(), no NEW model call may start, no NEW tool may
run, and nothing further may be spoken.
"""
import json
import os
import sys
import tempfile
import threading

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


class TTS:
    def __init__(self):
        self.spoken = []
        self.stopped = 0

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        self.stopped += 1


class Ollama:
    """Counts calls, and can trip the stop flag partway through."""

    def __init__(self, replies, stop_on=None, event=None):
        self.replies = list(replies)
        self.calls = 0
        self.stop_on = stop_on
        self.event = event
        self.aborted = 0

    def alive(self):
        return True

    def resolve_model(self):
        return ("mock:1", "")

    def abort(self):
        self.aborted += 1

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        self.calls += 1
        if self.stop_on == self.calls and self.event is not None:
            self.event.set()
        return self.replies.pop(0) if self.replies else ""


def answer(t):
    return json.dumps({"tool": "answer", "args": {"text": t}})


# -- 1. stop mid-stream: nothing further runs, and he is TOLD ---------
# The loop breaks the moment chat_stream returns with the flag set, so
# no answer is delivered at all -- he cancelled it. What matters is that
# NOTHING further runs and he is not left staring at a spinner.
gt.TOOLS["news"] = lambda args, ag: "3 headlines retrieved from 7 feeds."
cfg = dict(gc.DEFAULTS)
cfg["router"] = False
tts = TTS()
ag = gt.Agent(cfg, gt.MemoryStore(), tts)
ag.ollama = Ollama([json.dumps({"tool": "news", "args": {}}),
                    answer("Here are the headlines."),
                    json.dumps({"supported": False, "problem": "x"}),
                    answer("must never be reached")],
                   stop_on=2, event=ag.stop_event)
steps = []
finals = []
ag.on_step = lambda t: steps.append(t)
ag.on_final = lambda s: finals.append(s)
ag.run_turn("news")
check(ag.ollama.calls == 2,
      "stop mid-stream still made %d model calls" % ag.ollama.calls)
check(not tts.spoken, "a cancelled turn was read aloud: %r" % tts.spoken)
check(any("stop" in str(x).lower() for x in steps),
      "he was never told it stopped; the spinner just dies: %r" % steps)

# -- 1b. stop landing DURING A TOOL must skip the polish passes --------
# This is the reachable case for the guards in front of _repair_final
# and _verify_final: the model emitted a tool AND an answer in one
# reply, the tool ran, and he pressed stop while it was running. Both
# passes make their own model call, so running them here means the
# button does nothing for two more round trips.
holder = {}


def slow_tool(args, agent):
    holder["agent"].stop_event.set()      # he presses stop mid-tool
    return "3 headlines retrieved from 7 feeds."


gt.TOOLS["news"] = slow_tool
cfg = dict(gc.DEFAULTS)
cfg["router"] = False
tts = TTS()
ag = gt.Agent(cfg, gt.MemoryStore(), tts)
holder["agent"] = ag
ag.ollama = Ollama([json.dumps({"tool": "news", "args": {}}) + "\n"
                    + answer("Done."),
                    json.dumps({"supported": False, "problem": "x"}),
                    answer("must never be reached")])
ag.on_final = lambda s: None
ag.run_turn("news")
check(ag.ollama.calls == 1,
      "stop during a tool still ran the polish passes: %d model calls"
      % ag.ollama.calls)
check(not tts.spoken,
      "a turn cancelled mid-tool was still read aloud: %r" % tts.spoken)

# -- 2. no NEW tool may start after stop -------------------------------
ran = []
gt.TOOLS["probe"] = lambda args, agent: ran.append(1) or "ran"
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), TTS())
ag.stop_event.set()
out = ag.call_tool("probe", {})
check(not ran, "a tool started after stop was pressed")
check("stopped" in out.lower(),
      "the loop was not told the tool was skipped: %r" % out)

# -- 3. the router must not keep prefetching after stop ----------------
ran2 = []
gt.TOOLS["system"] = lambda args, agent: ran2.append(1) or "vitals"
gt.TOOLS["weather"] = lambda args, agent: ran2.append(2) or "wx"
gt.TOOLS["news"] = lambda args, agent: ran2.append(3) or "news"
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), TTS())
ag.ollama = Ollama([answer("hi")])
ag.on_final = lambda s: None
# Pressing stop BEFORE a turn cannot work: run_turn clears the flag, by
# design, or a stopped agent would stay stopped forever (see 7). So trip
# it from inside the FIRST prefetched tool -- "brief me" fires three --
# and assert the other two never run.
gt.TOOLS["system"] = lambda args, agent: (ran2.append(1),
                                          agent.stop_event.set())[0] or "v"
ag.run_turn("brief me")
check(len(ran2) == 1,
      "the router ran %d prefetch tools after stop was pressed during "
      "the first one" % len(ran2))

# -- 4. stop() must set the flag, cut speech, AND abort the stream -----
tts = TTS()
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), tts)
ag.ollama = Ollama([])
ag.stop()
check(ag.stop_event.is_set(), "stop() did not set the flag")
check(tts.stopped == 1, "stop() did not cut the speech off")
check(ag.ollama.aborted == 1,
      "stop() did not abort the live stream; ollama keeps generating an "
      "answer that was cancelled")


# a stop() that is called twice, or with nothing running, must be safe
ag.stop()
check(True, "unreachable")


# -- 5. Ollama.abort must close the live response ----------------------
class FakeResp:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1

    def __iter__(self):
        return iter(())


cli = gc.Ollama(dict(gc.DEFAULTS))
check(cli.abort() is None, "abort raised with nothing in flight")
r = FakeResp()
cli._live = r
cli.abort()
check(r.closed >= 1, "abort did not close the live response")


# -- 6. a close mid-stream is the button working, not a crash ----------
class Exploding:
    def __init__(self, stop):
        self.stop = stop
        self.n = 0

    def close(self):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        self.n += 1
        if self.n == 1:
            return json.dumps({"message": {"content": "partial"}}).encode()
        self.stop.set()
        raise OSError("socket closed")


ev = threading.Event()
cli = gc.Ollama(dict(gc.DEFAULTS))
try:
    out = cli._consume(Exploding(ev), lambda t: None, ev, None, 90)
except Exception as exc:
    out = None
    FAILS.append("a stop-closed stream raised instead of returning: %r"
                 % exc)
check(out == "partial",
      "the text received before stop was lost: %r" % out)

# ...but a real error with NO stop pressed must still raise
ev2 = threading.Event()


class RealError(Exploding):
    def __next__(self):
        raise OSError("genuine failure")


cli = gc.Ollama(dict(gc.DEFAULTS))
raised = False
try:
    cli._consume(RealError(ev2), lambda t: None, ev2, None, 90)
except Exception:
    raised = True
check(raised,
      "a genuine stream error was swallowed as if it were a stop")

# -- 7. a new turn must clear the flag ---------------------------------
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(), TTS())
ag.ollama = Ollama([answer("fresh")])
ag.on_final = lambda s: None
ag.stop()
ag.run_turn("hello again")
check(not ag.stop_event.is_set(),
      "a stopped agent stays stopped forever; the next turn cannot run")

print("stop checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
