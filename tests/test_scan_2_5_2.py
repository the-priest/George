"""Findings from the 2.5.2 sweep, pinned so they cannot come back.

Every one of these was a silent fault: nothing crashed, nothing logged,
the app just quietly did the wrong thing.
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


# -- 1. "0 = keep forever" has to actually keep forever ----------------
# `float(cfg.get(k) or 24)` turned a configured 0 into 24, so the
# keep-forever branch below it was unreachable and his chats were being
# deleted after a day.
cfg = dict(gc.DEFAULTS)
cfg["chat_retention_hours"] = 0
store = gc.ChatStore(cfg)
store.sessions = [{"id": "a", "ts": 0.0, "title": "ancient", "messages": []}]
store.purge()
check(len(store.sessions) == 1,
      "chat_retention_hours=0 still purged: keep-forever is broken")

cfg2 = dict(gc.DEFAULTS)
cfg2["chat_retention_hours"] = 1
store2 = gc.ChatStore(cfg2)
store2.sessions = [{"id": "a", "ts": 0.0, "title": "ancient", "messages": []}]
store2.purge()
check(not store2.sessions, "a real retention window stopped purging")

# -- 2. espeak pitch 0 is a legitimate setting (the range is 0-99) -----
check(gc.LIMITS.get("voice_pitch", (None,))[0] == 0,
      "voice_pitch lower limit is no longer 0; this test needs revisiting")
import george_voice  # noqa: E402,F401  (import must not explode)
src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "george_voice.py")).read()
check('"voice_pitch", 38) or 38' not in src,
      "voice_pitch is back to `or 38`, which silently ignores a pitch of 0")

# -- 3. no `or <number>` defaulting on a config key whose floor is 0 ---
import re                          # noqa: E402
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
zero_ok = {k for k, (lo, _hi) in gc.LIMITS.items() if lo == 0}
zero_ok |= {k for k, v in gc.DEFAULTS.items()
            if isinstance(v, int) and not isinstance(v, bool) and v == 0}
for fname in ("george.py", "george_core.py", "george_tools.py",
              "george_voice.py", "george_hud.py", "george_theme.py"):
    text = open(os.path.join(root, fname)).read()
    for key in zero_ok:
        pat = r'get\(\s*["\']%s["\'][^)]*\)\s* or \s*[1-9]' % re.escape(key)
        if re.search(pat, text):
            FAILS.append("%s: `or N` defaulting on %s, where 0 is valid"
                         % (fname, key))

# -- 4. history must not grow without bound ----------------------------
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(),
              type("T", (), {"speak": lambda s, t: None,
                             "stop": lambda s: None})())
for i in range(200):
    ag.history.append({"role": "user", "content": "q%d" % i})
    ag.history.append({"role": "user",
                       "content": "OBSERVATION (news):\n" + "x" * 6000})
    ag.history.append({"role": "assistant", "content": "a%d" % i})
ag.trim_history()
check(len(ag.history) <= ag.HISTORY_CAP,
      "history not trimmed: %d entries" % len(ag.history))
check(len(ag.history) > 24,
      "history trimmed below what messages() sends: %d" % len(ag.history))

# -- 5. saved sessions must not carry tool observations ----------------
conv = ag.conversation()
check(not any(m["content"].startswith("OBSERVATION") for m in conv),
      "tool observations are still being persisted to chats.json")
check(any(m["role"] == "assistant" for m in conv),
      "conversation() dropped the actual replies too")
saved = sum(len(m["content"]) for m in conv)
raw = sum(len(m["content"]) for m in ag.history)
check(saved < raw / 4,
      "observation stripping barely helped: %d -> %d bytes" % (raw, saved))

# -- 6. an atomic write must survive unserialisable data ---------------
target = os.path.join(_t, "probe.json")
gc._write_json(target, {"good": 1})
gc._write_json(target, {"bad": object()})       # must not raise
check(os.path.isfile(target), "the good file was destroyed by a bad write")
import json                        # noqa: E402
check(json.load(open(target)) == {"good": 1},
      "a failed write corrupted the previous contents")
check(not os.path.isfile(target + ".tmp"),
      "a failed write left its .tmp file behind")

print("scan checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
