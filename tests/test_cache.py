"""A short-lived results cache.

Asking the weather twice in a minute hit wttr.in twice. Asking for the
news and then a follow-up about one of the headlines re-fetched every
feed. On a laptop that is seconds of waiting for an answer George
already had, and it makes him look slow for no reason.

The whole design is the SHORTNESS of the TTL: long enough that a
follow-up is instant, short enough that "what's the weather now" is
never answered from ten minutes ago.
"""
import os
import sys
import tempfile
import time

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
    def __init__(self, **over):
        self.cfg = dict(gc.DEFAULTS)
        self.cfg.update(over)

    def step(self, *a):
        pass

    def tool_card(self, *a):
        pass

    def show_weather(self, w):
        pass

    def show_news(self, items):
        pass


# -- the primitives ----------------------------------------------------
gc.cache_clear()
gc.cache_put("weather", "dublin", {"temp_c": 14})
check(gc.cache_get("weather", "dublin") == {"temp_c": 14},
      "a stored value did not come back")
check(gc.cache_get("weather", "cork") is None,
      "a miss returned something")
check(gc.cache_age("weather", "dublin") == 0, "age is wrong on a fresh put")

# expiry is the point of the whole thing
gc.CACHE_TTL["probe"] = 1
gc.cache_put("probe", "k", "v")
check(gc.cache_get("probe", "k") == "v", "a fresh probe value expired early")
time.sleep(1.2)
check(gc.cache_get("probe", "k") is None,
      "an expired value was still served - stale data is worse than slow")

# every TTL must be short enough to be honest about "now"
for kind, ttl in gc.CACHE_TTL.items():
    if kind == "probe":
        continue
    check(ttl <= 900,
          "%s is cached for %ds; that is too long to call it current"
          % (kind, ttl))
    check(ttl >= 60, "%s TTL of %ds is too short to help" % (kind, ttl))
check(gc.CACHE_TTL["weather"] <= gc.CACHE_TTL["news"],
      "weather should expire at least as fast as news - he asks because "
      "he is about to walk outside")

# it must be bounded: this lives for the life of the process
gc.cache_clear()
for i in range(300):
    gc.cache_put("search", "q%d" % i, [i])
check(len(gc._CACHE) <= 120,
      "the cache grew to %d entries unbounded" % len(gc._CACHE))

# a broken cache must never break a lookup
check(gc.cache_get("nope", "nothing") is None, "an unknown kind raised")

# -- weather through the tool ------------------------------------------
calls = {"n": 0}


def fake_weather(loc):
    calls["n"] += 1
    return {"place": "Dublin", "desc": "rain", "temp_c": 14}


gt.weather = fake_weather
gc.cache_clear()
for _ in range(4):
    gt.tool_weather({"location": "Dublin"}, Ag())
check(calls["n"] == 1,
      "four identical weather asks made %d network calls" % calls["n"])

# a DIFFERENT place must not be served the cached one
gt.tool_weather({"location": "Galway"}, Ag())
check(calls["n"] == 2, "a different location was served from cache")

# and the setting must actually turn it off
before = calls["n"]
for _ in range(3):
    gt.tool_weather({"location": "Dublin"}, Ag(cache=False))
check(calls["n"] == before + 3,
      "cache=False still served from cache (%d calls)" % (calls["n"] - before))

# -- news: a FAILURE must not be cached ---------------------------------
# Caching a total failure would keep telling him the feeds are down for
# seven minutes after they came back.
news_calls = {"n": 0}
EMPTY = ([], ["RTE (404)"], 7)
FULL = ([{"source": "RTE", "title": "T", "url": "u", "summary": ""}], [], 7)
state = {"result": EMPTY}


def fake_news(feeds, per_feed=5, topic=""):
    news_calls["n"] += 1
    return state["result"]


gt.fetch_news_detailed = fake_news
gc.cache_clear()
gt.tool_news({}, Ag())
gt.tool_news({}, Ag())
check(news_calls["n"] == 2,
      "an empty news result was cached; the feeds coming back would not "
      "be noticed for minutes")

state["result"] = FULL
gt.tool_news({}, Ag())
gt.tool_news({}, Ag())
check(news_calls["n"] == 3,
      "a good news result was not cached (%d fetches)" % news_calls["n"])

# a different topic is a different question
gt.tool_news({"topic": "tech"}, Ag())
check(news_calls["n"] == 4, "a different topic was served from cache")

# -- search -------------------------------------------------------------
s_calls = {"n": 0}
gt.web_search = lambda q, n=6: (s_calls.update(n=s_calls["n"] + 1)
                                or [{"title": "R", "url": "u",
                                     "snippet": "s"}])
gc.cache_clear()
for _ in range(3):
    gt.tool_web_search({"query": "cats"}, Ag())
check(s_calls["n"] == 1,
      "three identical searches made %d calls" % s_calls["n"])
gt.tool_web_search({"query": "dogs"}, Ag())
check(s_calls["n"] == 2, "a different query was served from cache")

check("cache" in gc.DEFAULTS, "there is no config key for the cache")

print("cache checks; failures: %d  (TTLs: %s)"
      % (len(FAILS), ", ".join("%s=%ds" % (k, v)
                               for k, v in sorted(gc.CACHE_TTL.items())
                               if k != "probe")))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
