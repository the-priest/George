"""He asked for the news. George said "News articles are now on his
screen." Nothing was on his screen.

Three separate things were manufacturing that claim:

  1. tool_news returned the literal sentence "Headlines are now on his
     screen in the News panel" REGARDLESS of what happened. The model
     read it as fact and repeated it. That tool opens nothing -- it
     fills a card in the sidebar, which may well be scrolled out of
     view.
  2. fetch_news swallowed every feed failure into the log, so a caller
     that got one headline back could not tell a quiet news day from six
     of seven feeds refusing. Hence "1 headlines", unexplained.
  3. open_in_browser returned "opened X on screen" the instant Popen did
     not raise -- which only proves the binary exists. No DISPLAY, no
     registered browser, or a broken xdg-open all still reported
     success.

The rule these all violate: never claim an effect that was not
verified.
"""
import os
import sys
import tempfile

_t = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = _t + "/cfg"
os.environ["XDG_DATA_HOME"] = _t + "/data"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_core as gc           # noqa: E402
import george_tools as gt          # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


class FakeAgent:
    cfg = dict(gc.DEFAULTS)

    def step(self, *_a):
        pass

    def tool_card(self, *_a):
        pass

    def show_news(self, *_a):
        pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# -- 1. the sentence itself must be gone from the source ---------------
src = open(os.path.join(ROOT, "george_tools.py")).read()
check("Headlines are now on his screen" not in src,
      "tool_news still hands the model the claim that it put news on screen")

# -- 2. a healthy fetch must not claim the screen ----------------------
ITEMS = [{"source": "RTE", "title": "A headline", "url": "https://x.invalid",
          "summary": "s"}] * 5


def fake_detailed(items, failures, tried):
    # Clear the results cache: this file feeds tool_news a DIFFERENT
    # result each scenario while asking the same question, which real
    # use never does. Without this, scenario two is answered from
    # scenario one's cache.
    gc.cache_clear()

    def _f(_feeds, per_feed=5, topic=""):
        return list(items), list(failures), tried
    return _f


gt.fetch_news_detailed = fake_detailed(ITEMS, [], 7)
out = gt.tool_news({}, FakeAgent())
low = out.lower()
check("nothing has been opened" in low,
      "news observation does not state that nothing was opened")
check("sidebar" in low, "news observation does not say where the news went")

# -- 3. failures must be reported, with names --------------------------
gt.fetch_news_detailed = fake_detailed(
    ITEMS[:1], ["Reuters World (HTTP Error 404)", "BBC (timed out)"], 7)
out = gt.tool_news({}, FakeAgent())
check("Reuters World" in out,
      "a failed feed is not named in the observation: %s" % out[:200])
check("2 of 7 feeds failed" in out,
      "the failure count is not reported: %s" % out[:200])
check("very few" in out,
      "a 1-headline result is not flagged as suspicious")

# -- 4. a total failure must not produce a cheerful answer -------------
gt.fetch_news_detailed = fake_detailed([], ["RTE (403)", "BBC (403)"], 7)
out = gt.tool_news({}, FakeAgent()).lower()
check("do not say anything is on his screen" in out,
      "an empty news result does not warn the model off claiming success")
check("403" in out, "the reason for the empty result is not passed on")

# -- 5. fetch_news_detailed actually reports failures ------------------
import george_core                 # noqa: E402
george_core.http_get = lambda url, timeout=15: (_ for _ in ()).throw(
    OSError("refused"))
items, failures, tried = gc.fetch_news_detailed(
    [["A", "http://a.invalid"], ["B", "http://b.invalid"]], 3, "")
check(items == [], "items should be empty when every feed fails")
check(len(failures) == 2 and tried == 2,
      "failures not reported: %r tried=%s" % (failures, tried))
check("refused" in failures[0], "the failure reason is lost: %r" % failures)

# -- 6. open_in_browser must not claim success without a display -------
saved = {k: os.environ.pop(k, None) for k in ("DISPLAY", "WAYLAND_DISPLAY")}
try:
    if not gc.IS_WINDOWS:
        msg = gc.open_in_browser("https://example.invalid", {"browser": ""})
        check("opened" not in msg.split("could not")[0].lower()
              or "no graphical session" in msg,
              "claimed success with no display: %r" % msg)
        check("no graphical session" in msg,
              "did not explain that there is no display: %r" % msg)
    # a browser that does not exist must be reported, not assumed
    os.environ["DISPLAY"] = ":0"
    msg = gc.open_in_browser("https://example.invalid",
                             {"browser": "definitely-not-a-real-browser"})
    check("not installed" in msg,
          "a missing browser was not reported: %r" % msg)
finally:
    os.environ.pop("DISPLAY", None)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v

# -- 7. the prompt must forbid unverified claims -----------------------
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(),
              type("T", (), {"speak": lambda s, t: None,
                             "stop": lambda s: None})())
prompt = ag.system_message().lower()
check("never tell him something is on his screen" in prompt,
      "the prompt does not forbid unverified claims about the screen")

print("screen-claim checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
