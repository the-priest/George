"""Retrieval over recall.

A 4B does not know very much, and the dangerous part is that it does not
know that it does not know -- a half-remembered fact FEELS exactly like
a known one, so it fills the gap with fluent, confident, wrong text.

The fix is not a bigger model. It is somewhere reliable to look, a rule
that says look BEFORE answering, and a router that has already looked by
the time the model writes the sentence.

Wikipedia first (structured, citable, stable), the open web second, and
an honest "I could not check" third. Never memory.
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
import george_intent as R         # noqa: E402
import george_tools as gt         # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


class Ag:
    def __init__(self, **over):
        self.cfg = dict(gc.DEFAULTS)
        self.cfg.update(over)
        self.cards = []

    def step(self, *a):
        pass

    def tool_card(self, *a):
        self.cards.append(a)


LOVELACE = {
    "title": "Ada Lovelace",
    "description": "English mathematician (1815-1852)",
    "extract": ("Augusta Ada King, Countess of Lovelace was an English "
                "mathematician and writer, chiefly known for her work on "
                "Charles Babbage's proposed mechanical general-purpose "
                "computer, the Analytical Engine."),
    "url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
    "kind": "standard",
}

# -- the happy path: a citable article ---------------------------------
gc.cache_clear()
gt.wiki_search = lambda t, n=5: [{"title": "Ada Lovelace", "snippet": "s"}]
gt.wiki_summary = lambda t: dict(LOVELACE)
out = gt.tool_lookup({"term": "ada lovelace"}, Ag())
check("Analytical Engine" in out, "the article text is missing: %r" % out[:120])
check("en.wikipedia.org/wiki/Ada_Lovelace" in out,
      "the citable URL is missing")
check("Wikipedia" in out, "the source is not named")
check("do NOT fill the gap from memory" in out,
      "the model is not warned off answering from memory")

# -- a DISAMBIGUATION page is not an answer ----------------------------
# Treating one as an answer is how a lookup becomes confident nonsense.
gc.cache_clear()
pages = {"Mercury": {"title": "Mercury", "extract": "Mercury may refer to:",
                     "url": "u", "kind": "disambiguation", "description": ""},
         "Mercury (planet)": {"title": "Mercury (planet)",
                              "extract": "Mercury is the first planet from "
                                         "the Sun and the smallest in the "
                                         "Solar System.",
                              "url": "u2", "kind": "standard",
                              "description": "planet"}}
gt.wiki_search = lambda t, n=5: [{"title": "Mercury", "snippet": ""},
                                 {"title": "Mercury (planet)", "snippet": ""}]
gt.wiki_summary = lambda t: dict(pages[t])
out = gt.tool_lookup({"term": "mercury"}, Ag())
check("may refer to" not in out,
      "a disambiguation page was served as an answer: %r" % out[:120])
check("first planet" in out, "the real article was skipped: %r" % out[:120])

# -- nothing on Wikipedia: fall to the web, never to memory ------------
gc.cache_clear()
gt.wiki_search = lambda t, n=5: []
gt.web_search = lambda q, n=5: [{"title": "R", "url": "http://x.invalid",
                                 "snippet": "s"}]
out = gt.tool_lookup({"term": "some obscure thing"}, Ag())
check("Web results" in out, "the web fallback did not run: %r" % out[:120])
check("name the source" in out, "the web fallback does not require a source")

# -- nothing anywhere: say so, do not guess ----------------------------
gc.cache_clear()
gt.web_search = lambda q, n=5: []
out = gt.tool_lookup({"term": "zzz nothing"}, Ag())
check("not answer from memory" in out or "do not answer from memory" in out,
      "an empty lookup does not forbid answering from memory: %r"
      % out[:150])

# -- both sources failing is reported, not swallowed -------------------
gc.cache_clear()


def boom(*a, **k):
    raise OSError("network down")


gt.wiki_search = boom
gt.web_search = boom
out = gt.tool_lookup({"term": "anything"}, Ag())
check("could not" in out.lower(), "a total failure was not reported")
check("rather than answering from memory" in out,
      "a failed lookup does not warn the model off memory")

# -- caching: reference material barely changes ------------------------
gc.cache_clear()
calls = {"n": 0}


def counted(t, n=5):
    calls["n"] += 1
    return [{"title": "Ada Lovelace", "snippet": ""}]


gt.wiki_search = counted
gt.wiki_summary = lambda t: dict(LOVELACE)
for _ in range(3):
    gt.tool_lookup({"term": "ada lovelace"}, Ag())
check(calls["n"] == 1, "three identical lookups hit the API %d times"
      % calls["n"])
check(gc.CACHE_TTL.get("wiki", 0) >= 900,
      "reference material is cached too briefly to help")

# -- the router must prefetch for factual questions --------------------
FACTUAL = {
    "who is ada lovelace": "ada lovelace",
    "who is alan turing": "alan turing",
    "what is a mutex": "mutex",
    "what is the fermi paradox": "fermi paradox",
    "whats a semaphore": "semaphore",
    # the leading article is correctly stripped, which is the better
    # search term anyway
    "when did the berlin wall fall": "berlin wall fall",
    "tell me about rust": "rust",
}
for text, term in FACTUAL.items():
    plan = R.route(text)
    check(plan is not None and plan.prefetch
          and plan.prefetch[0][0] == "lookup",
          "%r did not route to lookup: %s" % (text, R.describe(plan)))
    if plan and plan.prefetch:
        got = plan.prefetch[0][1].get("term", "")
        check(got == term, "%r -> term %r, wanted %r" % (text, got, term))

# an optional article must not eat the first letter of the subject
check(R.route("who is ada lovelace").prefetch[0][1]["term"] == "ada lovelace",
      "the article regex ate the 'a' of 'ada' again")

# -- the specific tools must still win over the general one ------------
for text, tool in (("what is the weather", "weather"),
                   ("whats the news", "news"),
                   ("what is my ip", "network"),
                   ("what is wrong with my box", "diagnose")):
    plan = R.route(text)
    check(plan and plan.prefetch and plan.prefetch[0][0] == tool,
          "%r should route to %s, got %s" % (text, tool,
                                             R.describe(plan)))

# -- the model must be TOLD to look things up --------------------------
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(),
              type("T", (), {"speak": lambda s, t: None,
                             "stop": lambda s: None})())
P = ag.system_message()
check("WHEN YOU ARE NOT SURE, LOOK IT UP" in P,
      "the prompt does not tell it to look things up")
check("lookup" in gt.TOOLS, "the lookup tool is not registered")
check("lookup" in gt.tool_schema()["properties"]["tool"]["enum"],
      "lookup is not in the decoding schema")
check("lookup" in P, "lookup is not in the tool catalogue")

print("lookup checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
