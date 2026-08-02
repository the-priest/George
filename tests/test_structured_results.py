"""Tools return labelled fields, not prose to re-parse.

tool_system was the worst offender: it returned "memory: 0.3 / 3.9 GiB
(7%)" and DELIBERATELY DROPPED cpu_pct, mem_pct and disk_pct -- the
three clean numbers it already had -- forcing the model to dig a
percentage back out of a sentence before it could use one. Every
re-derivation is a chance to garble a number, and garbled numbers are
exactly what he has been catching by eye.

Labelled fields also make the verification pass cheaper and sharper: it
compares a claim against `disk_percent: 96` rather than against a
paragraph.
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

    def step(self, *a):
        pass

    def tool_card(self, *a):
        pass

    def show_vitals(self, st):
        pass

    def show_weather(self, w):
        pass


# -- fields_block itself ------------------------------------------------
out = gt.fields_block([("a", 1), ("b", "two")], "a summary", "a note")
check("FIELDS" in out and "  a: 1" in out and "  b: two" in out,
      "fields are not rendered as labelled lines: %r" % out)
check("SUMMARY: a summary" in out, "the summary line is missing")
check(out.rstrip().endswith("a note"), "the instruction is not last")

# empty and unknown values must be DROPPED, not printed. A small model
# will read "battery: None" back to him as a fact.
out = gt.fields_block([("present", 5), ("missing", None),
                       ("blank", ""), ("unknown", "?")])
check("missing" not in out and "blank" not in out and "unknown" not in out,
      "empty values leaked into the fields: %r" % out)
check("present: 5" in out, "a real value was dropped")
check(gt.fields_block([]) == "(nothing to report)",
      "an empty block does not say so")

# -- system: the percentages must be THERE, and machine-readable --------
st = gt.tool_system({}, Ag())
for key in ("cpu_percent", "memory_percent", "disk_percent"):
    check(key in st, "tool_system no longer exposes %s" % key)
import re                          # noqa: E402
for key in ("cpu_percent", "memory_percent", "disk_percent"):
    m = re.search(r"^\s*%s: (\S+)$" % key, st, re.M)
    check(m is not None, "%s is not on its own labelled line" % key)
    if m:
        check(m.group(1).isdigit(),
              "%s is not a bare number: %r" % (key, m.group(1)))
check("SUMMARY:" in st, "tool_system gives no summary verdict")
check("Do not recompute" in st,
      "the model is not told to quote the numbers rather than re-derive")

# the verdict has to actually reflect the numbers
disk = int(re.search(r"disk_percent: (\d+)", st).group(1))
summary = re.search(r"SUMMARY: (.+)", st).group(1)
if disk >= 90:
    check("disk" in summary, "a full disk is not named in the summary")
else:
    check("out of range" in summary or "disk" not in summary,
          "a healthy disk was flagged: %r" % summary)

# -- weather: same shape ------------------------------------------------
gt.weather = lambda loc: {"place": "Dublin", "desc": "rain", "temp_c": 14,
                          "feels_c": 12, "wind_kph": 23, "humidity": 80,
                          "min_c": 9, "max_c": 16}
w = gt.tool_weather({"location": "Dublin"}, Ag())
check("temp_c: 14" in w, "the temperature is not a labelled field: %r" % w)
check("feels_like_c: 12" in w, "feels-like is missing")
check("SUMMARY:" in w, "weather gives no summary")

# a PARTIAL weather dict must not produce None fields
gt.weather = lambda loc: {"place": "Dublin", "temp_c": 14}
w = gt.tool_weather({}, Ag())
check("None" not in w, "a missing weather field rendered as None: %r" % w)
check("temp_c: 14" in w, "a present field was lost in the partial case")

# -- no tool may ever emit a literal None or an empty label -------------
gt.weather = lambda loc: {"place": None, "temp_c": None, "desc": None}
w = gt.tool_weather({}, Ag())
check(": None" not in w, "None leaked into a field: %r" % w)

print("structured-result checks; failures: %d" % len(FAILS))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
