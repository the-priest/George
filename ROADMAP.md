# George — audit and roadmap

Written 2026-08-02 against v3.5.0, after twelve versions of fixing this
thing by running it. Everything below is grounded in something measured
or something that actually broke, not in a general sense of what apps
should have.

**Status:** 1.1, 1.2, 1.3, 2.1 and 2.2 are done as of v3.9.0. Tier 1 is
clear except 1.4 (the error-swallowing audit).

**Standing rule from 1.3:** when something breaks, add the session to
`tests/test_sessions.py` BEFORE fixing it. A scenario is cheap; a repeat
of the same bug is not. Everything else below is
still open, in the order given at the bottom.

**How to read this.** Each item says what is wrong, why it matters, and
roughly what it costs. Items are ordered inside each tier by
value-per-hour, not by how interesting they are. Tier 1 items are things
that are currently wrong or unverified. Tier 2 makes the small model
meaningfully better. Tier 3 is real but can wait. Tier 4 is honest
scepticism about ideas that sound good and are not.

---

## Where the project actually is

| | |
|---|---|
| Source | ~9,000 lines across 10 modules |
| Tests | ~2,300 lines across 21 files |
| Tools | 33 |
| Router rules | 13 |
| Config keys | 51 |
| Read-only allowlist | 245 commands |
| Prompt | ~3,127 tokens of a 16,384 window |

The architecture is sound. The layering — GTK only in `george.py`,
everything else headless and testable — is the single best decision in
here and it is why bugs get caught at all. Do not undo it.

---

## The honest state of verification

This matters more than any feature below, so it goes first.

**Verified by running it:** the safety gate (480 wrapper × command
combos, 4,000 fuzz strings, 76 must-run-free and 78 must-ask), the
Windows layer (253 checks, from Linux), the agent loop against a mock
server, ollama lifecycle with a fake binary, the GTK4 UI under Xvfb, the
markdown renderer (12,000 fuzzed inputs), the router, the reply
firewall, constrained decoding, and the code tool.

**Never verified against reality:**

- **Every one of the last five bug reports came from your screenshots,
  not from the test suite.** The scratchpad leak, the missing sidebar
  icon, the dead vision-pull button, the false "news is on your screen",
  and "I can't run code" — all of them passed every test I had. That is
  the most important line in this document.
- No real ollama has ever run against this code here. Constrained
  decoding is verified to be *sent correctly*; whether qwen3:4b honours
  it is unconfirmed.
- The Windows ctypes calls, SAPI speech, the PyInstaller bundle and
  ollama teardown on a real Windows kernel have never touched a Windows
  machine.
- DuckDuckGo search, live RSS, and wttr.in are blocked in my container.
  Feed *parsing* is covered by fixtures; feed *fetching* is not.

**The gap that produces this:** 27 of 42 tool functions are never named
in any test. The loop around them is well covered. The tools themselves
mostly are not.

---

## Tier 1 — currently wrong, or unverified in a way that bites

### 1.1 Dead default feeds  -- DONE in 3.6.0
`Reuters World` (`reutersagency.com/feed/?best-topics=world`) is gone —
Reuters killed its RSS, and your own screenshot shows the 404. `Irish
Times` uses an `arc/outboundfeeds` URL that commonly 403s to
non-browser agents.

Fix: replace Reuters with a live world feed (Guardian World and AP are
the usual survivors), test every default feed from a machine with
network, and delete anything that does not return valid XML. Add a
one-shot "test all feeds" button to Settings so this never needs a bug
report again.

**Cost: small. Value: high — it is visibly broken right now.**

### 1.2 Tool-level tests  -- DONE in 3.6.0
27 of 42 tools have no test naming them. `tool_web_search`,
`tool_open_page`, `tool_weather`, `tool_system`, `tool_media`,
`tool_clipboard`, `tool_screenshot` and most of the file tools are
exercised only incidentally.

Every bug you have reported lived in a tool, not in the loop. Fix:
a table-driven test that calls every tool with (a) valid args, (b)
missing args, (c) junk args, and asserts it returns a string, does not
raise, and does not claim an effect it did not have. Network tools get a
stubbed `http_get`.

**Cost: medium. Value: highest in this document.** This is the thing
that would have caught four of the last five bugs.

### 1.3 An end-to-end harness with a scripted model  -- DONE in 3.9.0
There is a mock ollama for the loop, but no harness that plays a whole
realistic session — greeting, news, a failed feed, a script, a declined
confirmation — and asserts what reached the screen. Every regression you
found by hand is one of these.

Fix: a fixture format of `(user says, model replies, expected on
screen)` and twenty of them drawn from real sessions. Add one every time
something breaks.

**Cost: medium. Value: very high.**

### 1.4 Error-swallowing audit
75 `except` blocks in `george_core.py`, 43 in `george_platform.py`, 36
in `george.py`. Most are deliberate — the fail-safe layer exists so a
broken tool cannot take the window down. But the same design makes bugs
*silent by construction*, which is why `test_audit.py` treats any
"failed:" line in the log as a test failure.

Fix: classify every `except` as (a) genuinely expected, (b) should log
and continue, (c) should not be caught at all. Category (c) is where
bugs hide.

**Cost: medium. Value: high, and it compounds.**

---

## Tier 2 — makes the 4B meaningfully better

These are the ones that close the gap between a small model and a
useful assistant. Ordered by how much they buy.

### 2.1 Verification pass on factual claims  -- DONE in 3.7.0
A 4B is poor at being right first time and **decent at checking a
concrete claim against evidence in front of it**. That asymmetry is the
biggest unexploited lever here.

Fix: after an answer that follows tool observations, one cheap
constrained call — "does the observation support this sentence?
yes/no + the unsupported part". On no, repair once. Cost is one extra
round trip only on tool-backed answers, and it directly attacks the
failure class you have hit most: confidently stating things that are
not so.

**Cost: medium. Value: highest in Tier 2.** This is the closest thing to
a genuine "make it punch above its weight" change.

### 2.2 Structured tool results  -- DONE in 3.8.0
`system` and friends return prose the model must re-parse into a
sentence. Every re-parse is a chance to garble a number.

Fix: tools return labelled fields (`cpu_pct: 7`, `mem_used_gib: 10.0`)
plus a one-line human summary. The model quotes fields rather than
re-deriving them. Cuts tokens and errors together.

**Cost: medium. Value: high.**

### 2.3 Argument repair instead of blind retry
A malformed tool call currently fails and the model tries again from
scratch. With a per-tool arg schema, a wrong key can be *corrected*
(`url` vs `link`, `q` vs `query`) or asked about precisely.

Constrained decoding already pins the outer shape; this extends it
inward. Note the deliberate current choice: `args` is free-form because
a schema strict enough for 33 tools would make an unlisted key
impossible rather than merely wrong. Per-tool schemas would be applied
*after* generation, as repair, not as a decoding constraint.

**Cost: medium. Value: high.**

### 2.4 More router rules, driven by logs
13 rules cover the obvious cases. The router logs every decision,
including misses.

Fix: after a week of use, read the log for turns that took three or more
model calls and write a rule for each recurring shape. This is the one
item on the list that gets *better with your usage data* rather than my
guessing.

**Cost: small per rule. Value: compounding.**

### 2.5 A results cache
Weather, news and search for the same query inside a few minutes hit the
network again every time. A 5–10 minute TTL cache makes repeated
questions instant and makes George usable offline for a short while.

**Cost: small. Value: medium-high, very visible.**

### 2.6 Retrieval over the conversation
History is capped at 80 entries and the last 24 are sent. Anything older
is simply gone. A small embedding index over past sessions (nomic-embed
is already in the curated list) would let "what did we decide about the
router" work.

**Cost: large. Value: high, but only once daily use makes the loss felt.**

---

## Tier 3 — real, but can wait

- **Session-scoped scratch memory.** `remember`/`recall` are permanent;
  there is nothing for "for the next ten minutes, the file I mean is
  X".
- **A tool-call trace panel.** The tool cards show what ran; there is no
  way to see the args, the raw observation, or the timing. This is a
  debugging aid for *you*, and would have shortened several of the last
  five bug hunts.
- **Streaming tool output.** A 60-second script shows nothing until it
  finishes.
- **Feed and search source weighting.** All feeds are equal; Hacker News
  drowns RTE on volume.
- **Voice interruption.** Speaking cannot currently be cut off
  mid-sentence by talking over it.
- **Per-tool timeouts.** One global watchdog covers tools with wildly
  different honest durations.
- **Screenshot-based UI regression tests.** I have measured the UI in
  pixels; nothing pins it. Worth it only once the design settles.

---

## Tier 4 — things that sound good and I would not do

Said plainly so they do not get built by accident.

- **Fine-tuning a local model on your usage.** Enormous effort, needs
  data you do not have, and the failure modes you have hit were
  *scaffolding* bugs, not weight bugs. Nothing in twelve versions would
  have been fixed by a better-tuned 4B.
- **A multi-agent setup (planner/executor/critic).** Three 4B calls cost
  three times the latency on CPU and multiply the number of places a
  small model can go wrong. The verification pass in 2.1 is the useful
  1% of this idea.
- **Bigger prompts.** Already at 3,127 tokens with a 3,200 ceiling for a
  reason. A 4B loses the middle of a long prompt regardless of what
  tokens cost. Clarity and structure buy more than volume, every time.
- **A plugin API.** Nobody else writes tools for this yet. Build it when
  someone asks.
- **Rewriting from scratch.** The safety gate alone has had six real
  bypasses closed in it, found by fuzzing and by running it. That
  knowledge lives in the code and the tests, not in anyone's head, and a
  rewrite discards it for something that looks more ambitious and is
  much less reliable.

---

## Suggested order

1. **1.1 feeds** — visibly broken, cheap.
2. **1.2 tool tests** — the missing net under everything else.
3. **2.1 verification pass** — the biggest single quality win available.
4. **2.2 structured results** — makes 2.1 cheaper and more reliable.
5. **1.3 session harness** — locks in everything fixed so far.
6. **2.5 cache**, then **2.3 arg repair**, then **2.4 router rules from
   your logs**.
7. **1.4 error audit** as background work between the rest.

---

## The principle worth keeping

Two things have produced every real improvement here:

**Anything you can make structurally impossible, do not ask for
politely.** Constrained decoding beat three versions of prompt
engineering. The `pkg` tool means a partial upgrade cannot be emitted
rather than being discouraged. The router means the model does not
choose when the words already chose.

**And: a small model cannot be trusted to decide, or to format.** The
router covers the first. The firewall and constrained decoding cover the
second. Every future addition should be checked against which of those
two it is actually helping — and if it is neither, it probably belongs
in Tier 4.
