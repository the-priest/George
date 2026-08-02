"""Why a turn crawled.

The system prompt was rebuilt on EVERY step of the agent loop -- up to
fourteen times per answer. That cost twice:

  * it shelled out each time (system_status, lspci for the GPU, several
    shutil.which for the package manager) on a laptop already saturated
    doing CPU inference;
  * worse, the text CHANGED between steps, because it carried the clock
    minute, uptime and battery level. Ollama caches the KV prefix of a
    prompt; any change to the system message invalidates it, so every
    step re-prefilled all ~1800 tokens from scratch instead of reusing
    them.

The prompt is now built once per turn and is byte-identical for every
step of that turn.
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


ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(),
              type("T", (), {"speak": lambda s, t: None,
                             "stop": lambda s: None})())

# -- the prompt must be identical across every step of one turn --------
ag.refresh_prompt()
first = ag.system_message()
same = all(ag.system_message() == first for _ in range(14))
check(same, "the system prompt changes between steps of a turn")

# ...even across a minute boundary, which is what actually broke it
real = time.strftime
try:
    time.strftime = lambda f, *a: "SOME OTHER TIME"
    check(ag.system_message() == first,
          "the prompt changes when the clock ticks mid-turn")
finally:
    time.strftime = real

# -- and it must be cheap to fetch after the first build ---------------
ag.refresh_prompt()
t0 = time.time()
ag.system_message()
build = time.time() - t0
t0 = time.time()
for _ in range(200):
    ag.system_message()
cached = (time.time() - t0) / 200
check(cached < build / 10 or cached < 1e-5,
      "cached prompt fetch is not cheap: %.6fs vs %.6fs" % (cached, build))

# -- refresh_prompt must actually rebuild ------------------------------
ag.refresh_prompt()
check(ag._prompt_cache is None, "refresh_prompt did not clear the cache")
check(ag.system_message() is not None, "prompt did not rebuild after refresh")

# -- machine_summary is cached and carries no live values --------------
summary = gc.machine_summary()
check(gc.machine_summary() is summary or gc.machine_summary() == summary,
      "machine_summary is not stable")
check("battery" not in summary.lower(),
      "battery is in the cached machine line; it changes and would go stale")
t0 = time.time()
for _ in range(500):
    gc.machine_summary()
check((time.time() - t0) / 500 < 1e-5, "machine_summary is not cached")

# -- the default model must suit a tool loop ---------------------------
check(not gc.model_advice(gc.DEFAULTS["model"]),
      "the DEFAULT model is one we warn about: %s"
      % gc.model_advice(gc.DEFAULTS["model"]))
for bad in ("qwen2.5-coder:7b", "deepseek-r1:7b", "llava:7b"):
    check(gc.model_advice(bad), "no warning for %s in a tool loop" % bad)

print("speed checks; failures: %d  (prompt ~%d tokens)"
      % (len(FAILS), len(first) // 4))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
