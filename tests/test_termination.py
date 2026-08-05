"""The loop must terminate, and it must not be slow about it.

He asked for the news, got 20 headlines, and George "kept thinking and
thinking like it's stuck". It was not stuck -- it was doing exactly what
it was allowed to do. Nothing forced it to stop, so it could take all 14
steps, and each step re-prefilled the system prompt PLUS the whole
1700-token news observation. On CPU that is minutes of visible
"thinking" for an answer it already had after step one.

Two fixes, and the first is the important one:

  1. Once tools have produced something and a couple of steps have gone
     by, the schema is constrained to `answer` ONLY. Constrained
     decoding masks the sampler, so another tool call is not
     discouraged -- it is unrepresentable. Termination becomes a
     guarantee rather than a hope.
  2. The news observation was far bigger than it needed to be: 20
     headlines with 200-character summaries AND a URL each. He asked
     for the news, not a wire feed.
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


class TTS:
    def speak(self, t):
        pass

    def stop(self):
        pass


class Looper:
    """A model that NEVER volunteers an answer -- the worst case, and
    the one he actually hit."""

    def __init__(self):
        self.calls = 0
        self.schemas = []

    def alive(self):
        return True

    def resolve_model(self):
        return ("mock:1", "")

    def abort(self):
        pass

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        self.calls += 1
        self.schemas.append(schema)
        allowed = ((schema or {}).get("properties", {})
                   .get("tool", {}).get("enum") or [])
        if allowed == ["answer"]:
            # Under the answer-only schema it CANNOT emit a tool call.
            return json.dumps({"tool": "answer",
                               "args": {"text": "Three stories up top."}})
        return json.dumps({"tool": "news", "args": {}})


gt.TOOLS["news"] = lambda a, ag: "20 headlines retrieved from 7 feeds."

# -- 1. a model that never answers must still be made to answer --------
cfg = dict(gc.DEFAULTS)
cfg["router"] = False
cfg["verify"] = "off"
ag = gt.Agent(cfg, gt.MemoryStore(), TTS())
ag.ollama = Looper()
got = {}
ag.on_final = lambda s: got.update({"t": s})
ag.run_turn("whats the news")

check(got.get("t") == "Three stories up top.",
      "the loop never produced an answer: %r" % got.get("t"))
check(ag.ollama.calls <= cfg["force_answer_after"] + 2,
      "a looping model took %d model calls; forced termination is not "
      "working" % ag.ollama.calls)
check(ag.ollama.calls < cfg["max_steps"],
      "it ran to max_steps (%d); that is the behaviour he saw"
      % ag.ollama.calls)

# the forcing must be done with the SCHEMA, not just a polite request
forced = [s for s in ag.ollama.schemas
          if ((s or {}).get("properties", {}).get("tool", {}).get("enum")
              == ["answer"])]
check(forced,
      "the answer-only schema was never used; asking a 4B nicely to stop "
      "is not a mechanism")

# -- 2. it must NOT fire before there is anything to answer from -------
ag2 = gt.Agent(cfg, gt.MemoryStore(), TTS())
ag2.ollama = Looper()
ag2.on_final = lambda s: None
ag2.run_turn("hello")
early = [s for s in ag2.ollama.schemas[:1]
         if ((s or {}).get("properties", {}).get("tool", {}).get("enum")
             == ["answer"])]
check(not early,
      "the first step was already constrained to answer-only; the model "
      "must be allowed to pick a tool at least once")

# -- 3. a well-behaved turn is untouched -------------------------------
class Good(Looper):
    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        self.calls += 1
        self.schemas.append(schema)
        if self.calls == 1:
            return json.dumps({"tool": "news", "args": {}})
        return json.dumps({"tool": "answer",
                           "args": {"text": "Three stories."}})


ag3 = gt.Agent(cfg, gt.MemoryStore(), TTS())
ag3.ollama = Good()
final3 = {}
ag3.on_final = lambda s: final3.update({"t": s})
ag3.run_turn("whats the news")
check(ag3.ollama.calls == 2,
      "a normal two-step turn took %d calls" % ag3.ollama.calls)
check(final3.get("t") == "Three stories.",
      "a normal turn was disturbed: %r" % final3.get("t"))

# -- 4. it must be switchable off --------------------------------------
cfg_off = dict(cfg)
cfg_off["force_answer_after"] = 0
ag4 = gt.Agent(cfg_off, gt.MemoryStore(), TTS())
ag4.ollama = Looper()
ag4.on_final = lambda s: None
ag4.run_turn("whats the news")
check(ag4.ollama.calls >= cfg["max_steps"],
      "force_answer_after=0 should let it run; got %d calls"
      % ag4.ollama.calls)
check(gc.LIMITS.get("force_answer_after") == (0, 12),
      "force_answer_after has no clamped range")

# -- 5. the observation must not be enormous ---------------------------
items = [{"source": "RTE",
          "title": "A fairly typical headline of about this length here",
          "url": "https://www.rte.ie/news/2026/0802/some-slug-here/",
          "summary": "x" * 200} for _ in range(20)]
gt.fetch_news_detailed = lambda f, per_feed=5, topic="": (items, [], 7)


class Ag:
    cfg = dict(gc.DEFAULTS)

    def step(self, *a):
        pass

    def tool_card(self, *a):
        pass

    def show_news(self, i):
        pass


out = gt.tool_news({"count": 20}, Ag())
tokens = len(out) // 4
check(tokens < 900,
      "20 headlines produce ~%d tokens; the loop re-prefills that on "
      "every step" % tokens)
# 20 headlines is not more useful than 10 -- it is more tokens to read
# AND more to write about, and writing is the slow part on CPU. He
# waited four minutes for a 12-item list he never asked for.
n = out.count("RTE")
check(n <= 10, "%d headlines reached the model; the cap is 10" % n)
check(n >= 3, "the cap went too far: only %d headlines" % n)
check(out.count("https://") <= 6,
      "a URL for every headline is what made this enormous")
check("THREE OR FOUR SENTENCES" in out,
      "the model is not told to summarise instead of listing everything")

# -- 6. a slow generation must SHOW progress, not look frozen ----------
# on_stall only fires when NOTHING arrives. On CPU the tokens do arrive,
# slowly, so the label sat on "thinking (step 1)" for four minutes while
# it was working -- indistinguishable from a hang.
import time as _time                # noqa: E402


class Slow:
    def alive(self):
        return True

    def resolve_model(self):
        return ("mock:1", "")

    def abort(self):
        pass

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        for _ in range(3):
            on_token("word " * 30)
            _time.sleep(1.6)
        return json.dumps({"tool": "answer", "args": {"text": "done"}})


cfg_slow = dict(gc.DEFAULTS)
cfg_slow["router"] = False
cfg_slow["verify"] = "off"
ag5 = gt.Agent(cfg_slow, gt.MemoryStore(), TTS())
ag5.ollama = Slow()
steps = []
ag5.on_step = lambda t: steps.append(t)
ag5.on_final = lambda s: None
ag5.run_turn("hello")
progress = [x for x in steps if "writing" in x]
check(len(progress) >= 2,
      "a slow generation reported no progress; it is indistinguishable "
      "from a hang: %r" % steps)
check(any("s," in x for x in progress),
      "the progress line does not show elapsed time: %r" % progress)
check(any("word" in x for x in progress),
      "the progress line does not show how much has been written: %r"
      % progress)

print("termination checks; failures: %d  (news = ~%d tokens, force after "
      "%d steps)" % (len(FAILS), tokens, gc.DEFAULTS["force_answer_after"]))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
