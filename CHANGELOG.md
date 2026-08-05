# George changelog

## 4.5.0 - the clock was costing 85 seconds a turn

His trace, from the panel added in 4.2.0:

    tool   news    4030ms
    model  step 1  134484ms   471 chars back

134 SECONDS to produce 471 characters. The arithmetic says generation
was maybe 30 of that. The other 100 was PREFILL - and the prompt had
not meaningfully changed.

**`Now: Wednesday 05 August 2026, 00:24 UTC` was inside the system
prompt.** ollama caches the KV prefix of a prompt; any change to the
system message throws the whole cache away. So every turn where the
MINUTE had ticked over re-prefilled all 3187 tokens from scratch.

This is the same bug I "fixed" in 2.7.0. That change made the prompt
stable WITHIN a turn and I never checked that it was stable BETWEEN
turns. It was not, and the cost was ~85 seconds every single time.

- **The clock, uptime and battery are out of the system prompt** and
  into a `volatile_context()` line appended as the LAST message.
  Everything before it is now byte-identical from one turn to the next,
  so the prefix cache survives and only the tail is prefilled. The model
  still gets all of it, just last.
- **The test now pins this properly**: the system prompt must be
  identical across turns even when the clock is moved, AND no clock,
  uptime, battery or load average may appear anywhere in the cached
  prefix at all - checked by pattern, not by memory of what I put there.
  Verified by putting the clock back and watching it fail.

Thank you for the trace. Four sessions of guessing at this from
screenshots, and the panel answered it in one paste.

## 4.4.0 - four minutes for the news

He asked for the news and waited FOUR MINUTES. His screenshots show
"thinking (step 1)" the whole time, which was the clue: it never looped.
A SINGLE step took four minutes. Working out where the time actually
went turned up two regressions of my own.

- **REGRESSION I CAUSED: num_ctx back to 8192.** I raised it to 16384
  two sessions ago for observation headroom. That was the wrong trade on
  a CPU-only laptop: a bigger KV cache costs memory bandwidth on EVERY
  generated token, and GENERATION - not context - is what makes him
  wait. With the news observation now ~440 tokens instead of ~1700, 8192
  leaves plenty of room.
- **The news payload was enormous.** 20 headlines, each with a
  200-character summary and a URL: ~1700 tokens to prefill on every
  step. Now capped at 10 headlines, summaries and URLs on the first five
  only: ~440 tokens. Default news_count 12 -> 8.
- **The answer itself was the biggest cost.** He got a 12-item numbered
  list - about 368 tokens - and at three to six tokens a second on that
  CPU that alone is one to two minutes. The news observation now asks
  for THREE OR FOUR SENTENCES on what matters, not one line per
  headline. The rest are in the sidebar where he can read them.
- **It also LOOKED frozen, and that is its own bug.** `on_stall` only
  fires when NOTHING arrives, but the tokens were arriving - slowly. So
  the label sat on "thinking (step 1)" for four minutes while it was in
  fact working, which is indistinguishable from a hang. It now reports
  "writing - 42s, 90 words so far", updated every 1.5 seconds.
- **Forced termination** (from earlier this session): once tools have
  produced something and a few steps have gone by, the schema is
  constrained to `answer` ONLY, so another loop is unrepresentable
  rather than merely discouraged. Verified against a model that never
  volunteers an answer: 14 calls and no reply before, 4 calls and an
  answer after. It would NOT have helped this particular case - his turn
  never left step 1 - and saying so matters more than claiming the fix.

**New `tests/test_termination.py`** - a model that never answers is made
to answer, the answer-only schema is actually used, it does not fire
before there is anything to answer from, a normal two-step turn is
untouched, it is switchable off, the news observation stays under 900
tokens, and a slow generation reports visible progress.

## 4.3.0 - retrieval over recall

A 4B does not know very much, and the dangerous part is that it does not
know that it does not know: a half-remembered fact feels exactly like a
known one, so it fills the gap with fluent, confident, wrong text. The
fix is not a bigger model. It is somewhere reliable to look, a rule that
says look BEFORE answering, and a router that has already looked by the
time the model writes the sentence.

- **New `lookup` tool.** Wikipedia first - structured, citable, stable,
  no key - then the open web, then an honest "I could not check". Never
  memory. The observation always carries a URL so George can say where
  the answer came from and he can check it.
- **A disambiguation page is not an answer.** "Mercury may refer to:"
  served as fact is exactly how a lookup becomes confident nonsense, so
  those are skipped and the next real article is used. Tested with a
  two-page Mercury fixture.
- **New rule R3: WHEN YOU ARE NOT SURE, LOOK IT UP.** Dates, numbers,
  versions, who did what, how something works. Looking things up is the
  job, not an admission of weakness.
- **Router prefetches the reference** for "who is X", "what is a X",
  "when did X", "tell me about X" - so the evidence is in front of the
  model before it writes anything. Specific tools still win: "what is
  the weather" goes to weather, "what is my ip" to network, "what is
  wrong with my box" to diagnose.
- Cached for an hour, because an encyclopaedia article is not current
  data.

**BUG CAUGHT WHILE WRITING THE ROUTER RULE:** the optional article in
`(a|an|the)?` matched the "a" of "ada", so "who is ada lovelace" looked
up "da lovelace". The article now has to be followed by whitespace.

**Three tests failed on this and all three were right.** The prompt went
over its 3200-token ceiling (trimmed back to 3187 without losing a
single rule - the ceiling exists for a reason and moving it would have
been the lazy fix); the tool-surface test had no valid-args case for the
new tool; and the cache test rejected the 1-hour wiki TTL. That last one
I fixed by SPLITTING the rule rather than weakening it: current data
(weather, news, search) keeps its tight 15-minute ceiling because
serving it stale is the same as being wrong, while reference data does
not need one.

## 4.2.0 - what just happened

Every bug in this project so far was found the same way: he
screenshotted the window and I guessed backwards from what was on it.
The tool cards show WHICH tool ran. They do not show the arguments it
was called with, what actually came back, whether it failed, or how long
it took -- which is precisely the information that would have shortened
five separate bug hunts.

- **New `Trace`**: the agent now records every step of a turn - what he
  typed, the router's decision, each model call with its duration and
  reply size, and each tool with its ARGUMENTS and the head of its
  actual observation, marked failed when it failed.
- **Menu > What just happened, or Ctrl+D.** Monospaced, scrollable, with
  a Copy button - because the point is that it ends up IN a bug report
  rather than in a description of one.
- Failure detection is on the observation text, so "could not open",
  "declined", "not installed" and a crashed tool all show as `[FAILED]`
  at a glance. An unrepairable argument is a failure too, and the trace
  says which key was missing.
- **Bounded at 400 rows, in memory only, and it can never break a
  turn.** It is a debugging aid, not an audit log: it must not eat RAM
  in a long session and must not write his paths to disk. Every `add`
  is wrapped, and the test throws None, objects, bytes and dicts at it
  to prove a broken trace cannot take an answer down with it.

**New `tests/test_trace.py`** - boundedness under 2000 inserts, junk
input, turn separation, and end to end: a real turn must be
reconstructable from the trace alone, including the tool's real output
and a failed call being marked as one.

## 4.1.0 - the stop button actually stops

He pressed stop and the model kept going.

**The real fix: `Ollama.abort()` closes the live HTTP response.**
Breaking out of the read loop was never enough on its own -- the
iterator BLOCKS until the next token arrives, so on CPU inference the
button appeared dead for however long the model took to produce one
more. Closing the socket makes the read fail immediately AND makes
ollama notice nobody is listening, so it stops GENERATING instead of
finishing an answer that was cancelled and burning the CPU he is waiting
on. `stop()` now calls it, alongside setting the flag and cutting the
speech off.

A close mid-stream lands as a read error, so `_consume` treats an
exception WITH the stop flag set as the button working and returns the
text received so far. With the flag NOT set it still raises - a genuine
stream failure must not be silently swallowed as if it were a stop, and
that is tested both ways.

`call_tool` also returns immediately when the flag is already set,
rather than starting work that has been cancelled.

**A correction, recorded because it matters more than the fix.** I also
added stop guards in front of `_repair_final` and `_verify_final`,
believing the polish passes added in 3.7.0 were costing two more round
trips after a cancel. I could not construct a test that reaches them:
the action loop breaks on stop before `final_text` is ever assigned, and
the stream check breaks earlier still. They are belt and braces, kept
because they are free, and now labelled as such in the source so nobody
mistakes them for the fix. The test file says the same. A test that
cannot fail is worth nothing, and I ran the guards through three
deliberate breaks before accepting they were unreachable rather than
declaring victory on a green run.

**New `tests/test_stop.py`** - stop mid-stream makes no further model
calls, speaks nothing, and TELLS him it stopped rather than leaving a
dead spinner; no new tool starts after a cancel; the router stops
prefetching partway through a three-tool "brief me"; `stop()` sets the
flag, cuts speech and aborts the stream; `abort()` closes the live
response and is safe with nothing in flight; a stop-closed stream
returns its partial text while a genuine error still raises; and a new
turn clears the flag, because a stopped agent that stays stopped is
worse than one that never stopped.

## 4.0.0 - roadmap 2.5: stop asking the network twice

Version renamed at his request. 3.10.0 was correct semver - the minor is
an integer, so 3.10 follows 3.9 the way Python 3.10 follows 3.9 - but
after ten releases of this the work has earned a round number.

- **Short-lived results cache** for weather, news and search. Asking the
  weather twice in a minute hit wttr.in twice; asking for the news and
  then a follow-up about one of the headlines re-fetched every feed. On
  a laptop that is seconds of waiting for an answer George already had.
  Four identical weather asks now make ONE network call.
- **The shortness of the TTL is the whole design**: long enough that a
  follow-up is instant, short enough that "what's the weather now" is
  never answered from ten minutes ago. Per-kind, because different data
  ages differently - weather 300s (he asks because he is about to walk
  outside), news 420s, search and pages 900s. The test asserts the
  ceiling AND that weather expires no slower than news.
- **A failed lookup is never cached.** Caching an empty news result
  would keep telling him the feeds are down for seven minutes after they
  came back.
- Bounded at 120 entries - this lives for the life of the process - and
  it fails open: a broken cache returns a miss rather than raising.
  Switchable with the `cache` config key.
- `test_no_false_claims` failed on this and was right to: it feeds
  tool_news a different result each scenario while asking the same
  question, which real use never does, so scenario two was being
  answered from scenario one's cache. Fixed with a cache_clear between
  scenarios rather than by weakening the cache.
- **New `tests/test_cache.py`** - the primitives, real expiry with a
  1-second probe TTL, the TTL ceilings, boundedness under 300 inserts,
  different-location and different-topic misses, the do-not-cache-
  failures rule, and the off switch.

## 3.10.0 - roadmap 2.3: repair the arguments, do not retry the turn

Constrained decoding pins the OUTER shape of a tool call and
deliberately leaves `args` free-form, because a schema strict enough for
33 tools would make an unlisted key impossible rather than merely wrong.
So the inside still needed help, and it was getting none: a wrong key
failed the call and the model retried from scratch - a whole round trip
on CPU to fix a typo.

- **Deterministic repair, no extra model call.** A 4B reaches for the
  obvious synonym: `link` for `url`, `q` for `query`, `cmd` for
  `command`, `file` for `path`, `code` for `source`. 29 tools now carry
  an alias table, and a rename is a dict lookup rather than seconds of
  inference.
- **Shape problems too**: a bare string where a dict belongs is lifted
  into the tool's required key (`{"tool":"show","args":"http://x"}`
  works), a nested `args`/`parameters`/`input` wrapper is unwrapped, and
  if the required key is missing but exactly ONE unrecognised value is
  present, that is what it meant.
- **It never invents anything.** With no candidate it changes nothing;
  with two candidates it refuses to guess; a correct call is left byte
  for byte alone; and an alias never overwrites a key that was already
  right. All four are tested, and the "refuses to guess" rule was
  verified by loosening it and watching the test fail.
- **When repair is impossible, the complaint is actionable**: it names
  the missing key, lists what was actually sent, says which synonyms it
  already tried, and shows the exact call shape. The model retries
  informed instead of guessing again.
- **New `tests/test_arg_repair.py`** - 21 real synonym cases, five shape
  cases, four must-not-invent cases, a consistency check that every
  alias points at a real tool, and end to end through `call_tool`.

## 3.9.0 - roadmap 1.3: whole sessions, replayed

Every other test checks a part. This one plays complete turns - what he
typed, what the model said back, what the tools returned - and asserts
WHAT REACHED THE SCREEN. That is the only thing he actually experiences,
and it is the layer where all five of his screenshot bug reports lived
while the rest of the suite stayed green.

- **Nine scenarios, every one drawn from something that really
  happened**: the greeting that cost one call, the scratchpad leak, dead
  feeds reported rather than papered over, the false "news is on his
  screen" being caught and repaired, a slow box getting the
  pre-analysed verdict, George NOT claiming it cannot write code, a
  declined confirmation not being retried forever, a failed `show` not
  becoming a success story, and a canned "Done." repaired into an
  actual answer.
- **Universal contracts checked on every scenario**, whatever the model
  did: he is never shown nothing, scratchpad never reaches the screen,
  raw protocol JSON never reaches the screen, a raw OBSERVATION never
  reaches the screen, the reply is never a wall of text, and what is
  SPOKEN matches what is SHOWN.
- The router is ON by default in these, because that is the real path -
  for "whats the news" and "why is my box so slow" the tool is
  prefetched and the model never chooses it. Scenarios testing the
  model's own tool choice set `"router": False` explicitly.
- **Verified by breaking three safety layers in turn** - the reply
  firewall, the verification pass, and the canned-answer repair. Each
  break was caught, and named in plain English rather than as an
  assertion number.

**Standing rule:** when something breaks, add the session here BEFORE
fixing it. A scenario is cheap; a repeat of the same bug is not.

## 3.8.0 - roadmap 2.2: labelled fields, not prose to re-parse

A tool that returns a sentence makes the model dig the numbers back out
of it before it can use them, and every re-derivation is a chance to
garble one.

- **`tool_system` was the worst offender.** It returned "memory: 0.3 /
  3.9 GiB (7%)" and DELIBERATELY DROPPED `cpu_pct`, `mem_pct` and
  `disk_pct` - the three clean numbers it already had - so the model had
  to parse a percentage back out of a string. It now returns
  `cpu_percent: 0`, `memory_percent: 7`, `disk_percent: 96` as bare
  numbers on their own lines, plus everything else it knows, plus a
  SUMMARY verdict worked out in Python ("under load - disk at 96%"), and
  an instruction not to recompute anything.
- New `fields_block()` renders labelled values, then a human line, then
  the instruction. Empty, None and "?" values are DROPPED rather than
  printed - a small model will read "battery: None" straight back to him
  as a fact.
- `tool_weather` restructured the same way, and its dead prose return -
  unreachable since the fields version went in above it - removed.
- This also makes 2.1 sharper: the verification pass now compares a
  claim against `disk_percent: 96` instead of against a paragraph.

**BUG MY OWN NEW TEST CAUGHT:** `w.get("desc", "conditions unclear")`
returns None when the key EXISTS with value None - the default only
covers a MISSING key. A partial upstream response rendered "None in
None, NoneC" and the model would have read it back as fact. Same family
as the falsy-zero bug that has now bitten four times. Fixed here and in
the two siblings the sweep found (search result titles, the diagnose
vitals line).

## 3.7.0 - roadmap 2.1: George checks his own answers

A 4B is poor at being right first time and noticeably BETTER at judging
whether a specific sentence is supported by text sitting in front of it.
That asymmetry is the biggest lever in this project, and it attacks the
failure he has hit most: George stating things that are not so.

- **After a tool-backed answer, one cheap constrained call** asks
  whether every factual claim is supported by the observations, with a
  two-field verdict schema. If not, one repair using only what the
  evidence shows - and if the evidence does not answer him, it says so
  rather than filling the gap.
- The canonical case is his own screenshot: the news tool returned zero
  headlines because Reuters 404'd, and George said "News articles are
  now on his screen." The evidence said the opposite in plain text.
  `tests/test_verify.py` runs exactly that turn and asserts the repair.
- **Deliberate limits.** Only when a tool actually ran - with no
  observations there is nothing to check against, and asking a small
  model to audit its own opinion just produces a second opinion. One
  repair, never a loop. And it FAILS OPEN: an unparseable verdict, a
  crashing checker or an empty repair all ship the original answer. A
  verification layer that can eat replies is worse than none. All four
  of those are tested.
- **The honest cost:** one extra call on tool-backed turns only, with a
  tiny constrained output. Chat and greetings are untouched. Settings >
  Interface > Check answers turns it off.
- Two tests failed on this and were RIGHT to - they were counting model
  calls from before verification existed. They measure routing and loop
  cost, so they now pin `verify: off` to isolate what they are actually
  measuring, rather than having their numbers quietly absorb another
  feature's cost.
- Verified by breaking it two ways: removing the call, and forcing the
  verdict to always say "supported". Both caught.

## 3.6.0 - roadmap 1.1 and 1.2

**1.1 - dead feeds.** Reuters killed its public RSS; his own screenshot
caught the 404. Replaced with Guardian World and AP, both of which
answer a plain urllib request without a browser User-Agent - which
several outlets now refuse. Irish Times kept with a note: its Arc
endpoint 403s often enough to be a nuisance, but the failure is
reported by name now rather than silently shrinking the story count.

**New: a "Test these feeds" button** in Settings > News feeds. It
fetches every feed in the editor and reports which answer and which do
not, with the reasons on hover. Feeds rot, I cannot reach them from
where this was written, and he should not need to file a bug to find
out which one died.

**1.2 - the tool-test gap, the biggest item in the audit.** 27 of 42
tool functions were never named in any test. The loop around them was
well covered; the tools themselves were not - and every bug he reported
by screenshot lived in a tool and passed the entire suite.

New `tests/test_tools_surface.py` calls EVERY registered tool with valid
args, no args, nulls, wrong types, unexpected keys and a 5000-character
string - 33 tools x 6 arg shapes - and asserts the contract the loop
depends on: always returns a string, never raises, never claims an
effect it did not have, never performs a side effect without asking, and
reports a refusal clearly enough that the model stops retrying.

It also AST-walks george_tools for every `ag.<attr>` the tools touch and
fails if the stand-in agent is missing one - otherwise the file silently
stops covering a tool the day it starts using a new callback. That check
found two of my own stubs with the wrong signature immediately
(`take_screenshot` returns (ok, path), not a path).

Verified by reintroducing three real past bugs: it caught the news
screen-claim and the missing confirmation on `code`.

**One real fix it surfaced:** `tool_weather` indexed its result dict
directly (`w["feels_c"]`) - a dict built in another module from a
third-party JSON shape. It fills every key today; a KeyError the day it
did not would have been swallowed by the loop's guard and looked like
the weather tool simply doing nothing. Now `.get` with defaults.

## 3.5.0 - it can write and run code, and now it knows that

He asked for a Python program that prints an ASCII bee. George replied
"I can't print ASCII art or run code directly" and pasted two print
statements - while holding `write_file` and `run`, which together do
exactly what he asked. He can do it in a terminal in ten seconds; being
told it is impossible is worse than being refused.

- **New `code` tool: write a script AND run it, in one call.** The tools
  were always there. Nothing told the model they COMPOSE, and a 4B will
  not work that out under pressure - so the composition is a tool of its
  own. python, bash, sh and node. The script is saved under
  `~/.local/share/george/scripts` and kept, and the real stdout, stderr
  and exit code come back.
- **One confirmation, showing the actual source.** That is the honest
  unit of consent: he is approving one intention, not two mechanical
  steps. Asking twice for one intention just trains him to click
  through without reading. Declining says so, tells the model not to
  retry, and tells it to show him the source in the answer so his
  request is not simply lost.
- **New rule R2, stated positively AND negatively**: you CAN write files
  and run programs; never claim you cannot run code, print ASCII art or
  produce a file - saying so is false and he knows it is false. If a
  tool refuses or he declines, that is a different thing, and you say
  THAT instead.
- Failures are honest: a crash reports the exit code and the traceback,
  a 60-second overrun says it was stopped and where the script is, a
  missing interpreter says so, and a script that printed nothing says it
  printed nothing rather than leaving the model to invent output.
- 33 tools. Prompt ~3127 tokens, still under the 3200 ceiling.
- **New `tests/test_code_tool.py`** - real execution and real output,
  exactly one confirmation, the source shown in it, declining handled
  without losing his work, crashes and empty output reported honestly,
  and the prompt actually containing the capability claim.

## 3.4.0 - constrained decoding: misbehaviour becomes unrepresentable

Everything so far has tried to PERSUADE a 4B model to emit the protocol
correctly - worked examples, blunt rules, a firewall to catch it when it
fails anyway. Persuasion has a ceiling, and 3.3.0 was written because
that ceiling had been hit.

- **ollama accepts a JSON Schema in `format`, and masks the sampler
  against it.** Only tokens that keep the output valid can be produced.
  The model does not TRY to emit our protocol; it becomes incapable of
  emitting anything else. There is no token path from a constrained
  decode to "We are in a new conversation. The user says...". This is
  the difference between asking a small model to behave and making
  misbehaviour structurally impossible.
- The schema pins the shape that matters - one object, a tool name from
  the real registry (all 33 including `answer`), an args object - and
  deliberately leaves `args` free-form. A schema strict enough for all
  32 tools would be enormous, and would make an unlisted key impossible
  rather than merely wrong.
- On by default (`structured: auto`), switchable off, and it degrades:
  an ollama too old for `format` gets a 400, and George drops the field
  and retries once.
- **THAT FALLBACK PATH WAS ALREADY BROKEN and nobody had noticed.** The
  same handler covers the `think` field, and it retried, got a good
  response, and then FELL THROUGH to raise anyway - `resp` was assigned
  and immediately discarded. So on any ollama old enough to reject
  `think`, George failed every request. Fixed by extracting the stream
  consumer into `_consume` so the retry has something to return.
- Newer ollama returns a reasoning model's scratchpad in a separate
  `thinking` field. `_consume` now says explicitly that it is never
  appended - concatenating it is another route to deliberation on
  screen.
- Six test mocks were pinning the old `chat_stream` signature and failed
  when it grew a `schema` parameter. Signature drift in the tests, not a
  regression - fixed the mocks.
- **New `tests/test_structured.py`**: the schema covers every registered
  tool, is sent when it should be, is not sent when off or absent, and
  the old-ollama retry returns the response instead of raising.

## 3.3.0 - the reply firewall, and two things I broke

- **George printed the model's private scratchpad as the answer.** At a
  plain "hi geroge how are you bro", qwen3:4b emitted eight hundred
  words of itself deliberating - "I must respond as George", "let me
  craft a response", "Final decision:" - and all of it went on screen.

  The loop had exactly ONE fallback: no JSON found, so treat the whole
  raw reply as prose and show it. That fallback is the bug. A 4B model
  does not reliably obey "output only JSON", so the loop cannot assume
  that anything which is not JSON is a reply.

  New firewall in front of that path. It recognises a scratchpad by the
  phrases that only appear when a model talks to ITSELF (two or more
  needed, so ordinary prose containing one is not misread), then
  salvages the answer it settled on - a deliberating model quotes its
  candidates, and the last quoted line that reads like speech is almost
  always the one it chose. On his actual leak it recovers exactly "Hey
  bro. I'm running smoothly. What do you need?" out of 800 words. If
  nothing is recoverable it asks once more for the JSON alone, and only
  if THAT fails does he get a short honest line. Raw deliberation never
  reaches the screen.

- **I broke the HUD toggle in 3.0.0.** The stateful toggle swapped its
  icon to `sidebar-hide-symbolic` when the sidebar was shown - and that
  icon DOES NOT EXIST in Adwaita. A missing icon name is not an error in
  GTK; the button just renders empty, which is indistinguishable from
  the button being gone. New `_icon_or()` helper takes a list and
  returns the first name the running theme actually has.

- **Pulling a vision model looked like a dead button.** The progress
  callback was discarded (`lambda m, f: None`) and the only feedback was
  a toast on the main window - behind the modal Settings dialog, where
  he could not see it. Progress now lands on the row itself, and the
  callback signature is right: ModelManager.pull reports (status,
  0..1), not bytes.

- **New `tests/test_firewall.py`** - his real leak verbatim, three other
  scratchpad shapes, six ordinary replies that must NOT be flagged
  (including one that legitimately contains "the rules say" and one
  that says "I think the simplest"), salvage refusing to return the
  model quoting its own rules, and end to end: leak in, one clean
  sentence out.

## 3.2.0 - room to think, and three tools that do a whole job

- **num_ctx is 16384, up from 8192.** The system prompt is ~2.8k and a
  multi-tool turn spends ~1.5k per observation, so 8192 left room for
  three observations before George started forgetting the start of his
  own turn. 16384 buys nine. Cost is ~1.2 GB more KV cache (fp16,
  qwen3:4b) and proportionally more prefill on CPU. Deliberately NOT
  32768: that is ~4.8 GB of cache and roughly double the wait before the
  first token, to buy headroom a desktop assistant turn does not use.
  Also fixed a stale `num_ctx` fallback of 8192 in the request builder
  that would have drifted from DEFAULTS.

- **Three composite tools.** Each collapses a sequence the model
  otherwise had to plan, execute and stitch together itself. A round
  trip saved is a whole prompt re-read plus a whole generation, and on
  CPU that is seconds.

  - `diagnose` - vitals, disks and top processes by CPU and memory in
    one call, WITH the verdict already worked out. The model used to
    call three tools and then judge which number was the anomaly; that
    judgement is arithmetic, so it happens in Python now and the model
    is handed the conclusion. "Why is it slow" routes straight here.
  - `research` - search AND read the top result in one call. A snippet
    is rarely enough to answer from, so the model used to search, judge,
    open_page, then answer: three round trips.
  - `pkg` - packages in the right dialect for this machine, without the
    model knowing pacman from apt. This is a SAFETY tool as much as a
    convenience: `pacman -Sy foo` is a partial upgrade and breaks Arch,
    and rather than trust a 4B model to remember that under pressure the
    correct command is built in Python from the detected manager. It
    cannot emit a bare `-S` or a partial upgrade, and the test pins
    that.

- **BUG CAUGHT IN MY OWN NEW CODE, before it shipped:** the router
  matched "install ripgrep" and prefetched `pkg` with action=install -
  a state-changing action fired BEFORE the model had decided anything.
  The router runs unreviewed by definition, so it must never take an
  action that changes the machine. It now prefetches a SEARCH; the model
  proposes the install and he confirms it, which is the path every state
  change has to take. `tests/test_router.py` now asserts this across
  every rule, not just the pkg one.

- 13 router rules, ~11 microseconds per message. 32 tools.

## 3.1.0 - a prompt the model can actually follow

The prompt had grown by accretion. Every fix across a dozen versions
appended another bullet to a flat RULES list until it was a wall of
overlapping instructions with no structure. A 4B model reads that the
way you would read a contract.

- **Rewritten as six numbered sections** - how a turn works, when to use
  a tool and when not to, the tool catalogue, hard rules, commands for
  this machine, context. Each rule stated once, in one place, numbered
  R1-R8 so it can be pointed at.
- **Four worked examples of the actual JSON protocol.** A small model
  copies patterns far more reliably than it follows prose, so the prompt
  now SHOWS a no-tool answer, a tool-then-answer round trip, what to do
  when an observation is already waiting, and what to say when a tool
  FAILED - including, explicitly, that you do not claim it worked.
- **The router contract is explained to the model.** It now knows that
  an OBSERVATION followed by a GUIDANCE line means the obvious tool was
  already run to save it a step, that it must not call that tool again,
  and that it should go straight to `answer`.
- **The tool catalogue is grouped by JOB, not listed flat.** Nine
  headings: looking things up, putting something in front of him, this
  machine, doing things to the machine, files, eyes and clipboard,
  memory, odds and ends, ending the turn. A model scanning 29 flat names
  picks by string similarity; grouped under a heading that matches his
  words, it picks by intent. The "putting something in front of him"
  group exists specifically so the difference between `news` and `show`
  is structural rather than something it has to infer.
- **Command reference for this box** as a table rather than a sentence:
  pacman install vs query vs ownership vs AUR, and the Debian, Fedora
  and Windows equivalents.
- **Fixed: "You are George, his's desktop assistant."** The name
  fallback was the bare word "his", so every user who had not set a name
  got a broken first sentence. The possessive is applied where the name
  is resolved now.
- ~2475 tokens, up from ~1809, and built once per turn so it is
  prefilled once rather than on every step. Local tokens are free; a
  small model's attention is not, so there is a hard ceiling in the
  tests at 3200.
- **New `tests/test_prompt.py`**: every registered tool documented and
  no phantom ones, all four worked examples present, the router
  convention explained, each hard-won rule still there by exact phrase,
  the catalogue grouped, ASCII-clean, building under every persona, and
  the token ceiling.
- Two of those checks were wrong on first run and I fixed the TEST, not
  the prompt: `}}` is legitimate when it closes nested JSON, and the
  on-screen group is called "putting something in front of him".

## 3.0.0 - the router

George's loop made the model decide things the words had already
decided. "What's the weather?" cost two full model round trips: one to
pick the weather tool, one to write the answer. On CPU inference that is
most of the latency of a turn, spent on a choice that was never in
doubt.

- **New `george_intent.py` - a tenth module, and the first one that is
  pure architecture.** It recognises the obvious requests and RUNS THE
  TOOLS BEFORE the model is called at all. The model then gets one call
  with the observations already in hand and only has to write the reply.
  One round trip instead of two or three. Verified end to end: the same
  question takes 1 model call with the router on and 2 with it off.
- **Eleven rules**, covering weather, this machine, disk, what is eating
  the box, network, news, time, explicit web searches, opening a URL,
  memory recall - and `brief me` / `what did I miss` / `how's
  everything`, which fire system + weather + news together in one shot.
  Small talk routes to no tools at all.
- **Deliberately conservative.** A miss costs one extra round trip -
  exactly what happened before. A WRONG prefetch wastes a tool run and
  pollutes the context, which is worse. So capability questions ("can
  you explain how TCP works"), instructions about future behaviour
  ("next time just tell me the headline"), real work ("write me a
  script"), and keywords buried in prose all decline to route. The
  "must not route" cases are tested as hard as the ones that must.
- **Contractions are folded before punctuation is stripped.** Otherwise
  "how's" becomes "how s" - two tokens - and every rule written as
  `how'?s` silently stops matching. That was costing the router most of
  its hit rate before it ever shipped.
- **A URL beats a keyword inside it.** "open https://news.ycombinator.com"
  matched the news rule, because the word "news" is in the hostname, and
  pulled RSS feeds instead of opening the page he named. The show rule
  is first on purpose.
- Routing costs ~11 microseconds per message. It runs before every turn,
  so that is tested too.
- Switchable: Settings > Interface > Fast routing, and the `router`
  config key. Off means the model decides everything itself, exactly as
  before.
- `george_intent.py` added to install.sh REQUIRED_FILES and the
  PyInstaller spec. install.ps1 takes the whole zipball, so it needed
  nothing.
- **New `tests/test_router.py`**: every rule, every must-not-route case,
  the URL-versus-keyword precedence, the disable flag, the routing cost,
  and the end-to-end round-trip saving with a counting mock.

## 2.7.0 - why it was slow, and why it seemed dumb

- **The system prompt was rebuilt on every step of the loop** - up to
  fourteen times per answer - and that is the single biggest reason a
  turn crawled. It cost twice over. It shelled out each time
  (system_status twice, lspci for the GPU, several shutil.which for the
  package manager) on a laptop already saturated doing CPU inference.
  And, worse, the text CHANGED between steps, because it carried the
  clock minute, uptime and battery level. Ollama caches the KV prefix of
  a prompt; ANY change to the system message throws that away, so every
  single step re-prefilled all ~1800 tokens from scratch instead of
  reusing them. The prompt is now built once per turn and is
  byte-identical for every step of it.
- **`machine_summary()` is cached for the life of the process.** Distro,
  kernel, arch, desktop, CPU, cores, GPU and package manager cannot
  change while George is running. Battery came out of that line
  entirely - it changes constantly, and anything that changes does not
  belong in a cached prefix. RAM is now the TOTAL, not live usage, which
  would have frozen at startup and been quietly wrong an hour later.
- **The default model is now `qwen3:4b`, not `deepseek-r1:7b`.** George
  drives a tool loop: what matters is clean JSON on the first try and
  speed, not prose. A reasoner thinks before every one of up to 14
  steps, so a single answer pays that cost over and over. Changed in
  DEFAULTS, install.sh, install.ps1 and the README.
- **George now tells him when the model is the problem.** Running a
  code-completion, reasoning, vision or embedding model in a tool loop
  feels exactly like "slow and dumb", and it is fixable in one click.
  The ENGINE card shows a "wrong model for this job" row naming the
  reason, and opens the Models dialog when clicked.
- Two tests were asserting the old default model rather than catching a
  regression - the fallback path was working correctly. Fixed the tests.
- **New `tests/test_speed.py`**: the prompt must be identical across all
  fourteen steps of a turn AND across a minute boundary, cached fetches
  must be effectively free, `machine_summary` must be cached and carry
  no live values, and the DEFAULT model must not be one we warn about.

## 2.6.0 - George stops claiming things he did not do

He asked for the news. George said "News articles are now on his
screen." Nothing was on his screen. Three things were manufacturing
that claim.

- **The news tool handed the model the lie.** `tool_news` returned the
  literal sentence "Headlines are now on his screen in the News panel"
  regardless of what happened, and the model - correctly - repeated what
  its tool told it. That tool opens NOTHING. It fills a card in the
  sidebar, which is frequently scrolled out of view. The observation now
  states facts only: how many headlines, from how many feeds, that they
  are in the sidebar NEWS card, and explicitly that nothing has been
  opened on his screen. If he wanted it in front of him, it tells the
  model to follow up with `show` and claim nothing until `show` reports
  success.
- **Feed failures were invisible.** `fetch_news` swallowed every error
  into the log, so one headline coming back was indistinguishable from a
  quiet news day - which is why "1 headlines" arrived with no
  explanation. New `fetch_news_detailed` returns the failures and the
  count tried. George now names the dead feeds in his answer, and the
  sidebar shows "N feeds failed - check Settings > News feeds" with the
  reasons on hover. The feed list has been editable in Settings all
  along; now he can see which entries to fix.
- **`open_in_browser` claimed success without checking.** It returned
  "opened X on screen" the moment `Popen` did not raise, which only
  proves the binary exists. It now refuses outright when there is no
  DISPLAY or WAYLAND_DISPLAY, reports a missing browser by name, and
  otherwise waits briefly and reports the exit code and stderr if the
  handler died. A handler that works stays alive; one that is going to
  fail fails immediately.
- **New prompt rule, stated bluntly**: never say something is on his
  screen, open, running, installed or done unless a tool came back and
  said so - and being wrong about what you just did is worse than doing
  nothing. Filling a sidebar card is not putting something on his
  screen.
- **Sharper Arch/CachyOS commands.** Never a bare `-S`, never `-Sy` on
  its own (a partial upgrade breaks the system), `pacman -Q/-Qi/-Ss/-Si`
  for queries, and AUR packages need `paru`/`yay` - pacman cannot
  install them and should not be offered as if it can. CachyOS is Arch
  underneath, but ships its own kernel and repos and should not be told
  to replace either.
- **New `tests/test_no_false_claims.py`** covering all three sources,
  including that the offending sentence is gone from the source, that a
  failed feed is named, and that a missing browser or absent display is
  reported rather than assumed away.

## 2.5.3 - formatting stopped falling off replies

- **Overlapping tags were stripping the formatting off whole replies.**
  `md_to_pango` ran its bold, italic, code and link passes
  independently, so the tags they produced could OVERLAP rather than
  nest: `` `*`* `` became `<tt><span><i></span></tt></i>` and `******`
  became `<b><i></b></i>`. Pango rejects overlapping tags, and
  `safe_markup` falls back to plain text on a rejection - for the WHOLE
  label. So one confusing fragment anywhere in an answer stripped the
  bold, the links and the code styling out of ALL of it. Fuzzing put it
  at roughly 1 input in 8.

  Code spans and links are now pulled out behind placeholders before
  the emphasis passes run, and put back afterwards, so overlap is
  impossible by construction. 12,000 fuzzed inputs, zero rejections
  (was 999 in 8,000).
- **A code span is literal now**, which is what markdown says it is:
  `` `a *b* c` `` shows the asterisks instead of italicising b.
- **`faint` was scraping the contrast floor.** It carries timestamps,
  eyebrow labels and the hero subtitle - all SMALL text, which WCAG
  holds to 4.5:1, not the 3:1 large-text floor. At `#5a6b7d` it sat at
  3.35:1 on a card. Now `#72859a`: clears 4.5:1 on void, plate and card
  alike, still clearly subordinate to `dim`.
- **New `tests/test_design.py`** - a design linter that runs without a
  display: WCAG contrast for every text colour against every surface it
  can land on, across all six accents; the type scale staying monotonic
  and distinct at font scales from 0.75 to 2.0; and the sheet building
  clean, ASCII-only, with no unsubstituted tokens and none of the CSS
  GTK cannot parse.
- **New `tests/test_markup.py`** - the two minimal overlap cases, the
  realistic combinations, escaping, and the fuzz that found it.

## 2.5.2 - sweep

A systematic pass over the tree: the falsy-zero idiom, unbounded state,
timer lifecycle, and write durability. Nothing here crashed - that is
why none of it had been noticed.

- **"0 = keep forever" was deleting his chats after a day.** `ChatStore.purge`
  read the setting as `float(cfg.get(k) or 24)`, so a configured 0 became
  24 and the `if hours <= 0: return` branch directly below it was
  unreachable. The Settings row promising "keep forever" was doing the
  opposite. This is the third time `or <default>` has bitten a value
  where 0 is legitimate (watch_min_gap in 2.2.0, dedupe threshold in
  2.5.1), so there is now a test that greps the tree for the pattern
  against every config key whose floor is 0.
- **espeak pitch 0 was ignored** for the same reason - the range is
  0-99 and 0 is falsy.
- **Conversation history grew without bound.** A 14-step turn appends up
  to ~30 entries, several of them 6 KB tool observations, and
  `messages()` only ever sends the last 24. Capped at 80.
- **Every tool observation was being written to disk on every turn.**
  `_save_session` persisted the raw history - including all those 6 KB
  observations - and rewrote the entire chats file each time, while the
  history dialog skipped them on restore. They were stored and never
  read. Sessions now save the conversation only: a 900-entry test
  history went from 162 KB to 212 bytes.
- **Turning animations on while the HUD was hidden started a 15fps
  redraw on an invisible widget** - and "unmap" could not fire again to
  stop it, because it was already unmapped. A battery leak on a laptop.
  `_on_map` now checks `get_mapped()`.
- **`_write_json` only caught `OSError`.** `json.dump` raises `TypeError`
  on anything it cannot serialise, which escaped the function entirely
  and left a half-written `.tmp` behind. Now caught, the temp file is
  cleaned up, the real file is untouched, and the write is fsynced
  before the rename.
- **New `tests/test_scan_2_5_2.py`** pinning all of the above.

Checked and found clean: no bare `except:` anywhere, no mutable default
arguments, every `subprocess` call carries a timeout (verified by AST
walk, not grep), every repeating GLib timer returns a proper bool, no
dangling tool aliases, TOOL_SPEC and the registry agree exactly, and
LIMITS/CHOICES keys all exist in DEFAULTS. Fuzzed 3000 garbage configs
through `coerce_config` + the theme builder, 6000 random strings through
the action parsers, and 8000 through the safety gate: zero crashes.

## 2.5.1 - George answers you instead of reporting to you

He said "hi" and got back "Done." - or a line of raw news output. Two
faults, stacked.

- **Any reply of 20 characters or fewer was thrown away.** The
  final-answer "repair" treated short as canned, and replaced the reply
  with the first line of the last tool observation. But a good answer to
  "hi" is short BY DESIGN. "Hey. What is up?", "Yes, it's fine.",
  "It's 14 degrees." and "No." were all discarded and swapped for
  machine output. Length is not evidence of anything; the canned check
  now matches actual status phrases ("done", "ok", "task complete") and
  nothing else.
- **Nothing in the prompt said a tool was optional.** The model reached
  for `news` or `system` to say hello, which is what manufactured the
  observation the first fault then pasted in. The prompt now states
  plainly that greetings, thanks, chit-chat, opinions, explanations and
  follow-ups are answered directly on the first step, and that `answer`
  is a reply in his own words - never "Done." or "Already on screen."
- **The repair asks again instead of pasting.** When the model really
  does emit a status grunt after a tool ran, George now asks it for the
  reply it should have given, and only falls back to the observation if
  that comes up empty too. Raw tool output was never an answer.
- **Repair only fires when a tool actually ran.** A bare "ok" in the
  middle of a conversation is a fine thing to say and is left alone.
- **Dropped the path redaction.** It rewrote his own local paths to
  `[REDACTED_PATH]` in George's spoken replies. This is his machine.
- **Fixed a falsy-zero bug** in the dedupe threshold: `cfg.get(k, 2) or 2`
  turned a configured 0 into 2, because 0 is both legitimate and falsy.
- **New `tests/test_greeting_reply.py`** - pins short real answers
  surviving intact, greetings not costing a tool call, canned grunts
  being repaired by retry rather than paste, and the prompt actually
  containing the permission. Verified by restoring the old logic and
  watching it fail with 7 errors, including the reported case.

## 2.5.0 - the theme actually reaches the screen

The look was already written. It was being thrown away at startup.

- **The whole stylesheet was being wiped by one line.**
  `Gtk.CssProvider.load_from_data` REPLACES a provider's contents - it
  does not append. `_install_css` loaded the theme, then loaded a second
  little rule to put the bundled `george.png` behind the chat, and that
  second call deleted all 29 KB of the first one. With a `george.png` in
  the folder - which every checkout has - George fell back to stock
  Adwaita: no message bubbles, no HUD card backgrounds, a grey headerbar.
  There was no error, because nothing had gone wrong as far as GTK was
  concerned. The sheet is now built once, concatenated once, and loaded
  once.
- **Bubbles never aligned.** `_row` set `halign` on the bubble, but a
  `Gtk.Box` only hands spare width to a child that asks for it, so there
  was nothing for `halign` to align within and every message - his and
  George's - stacked up on the left. His bubbles now sit right where
  they belong.
- **Bubble geometry.** Widths are capped in code (GTK CSS has no
  max-width), so a one-line question is a one-line bubble instead of a
  full-width slab. Rounder corners, one square corner as the tail, the
  accent fill on his side and a surface panel on George's.
- **Per-message buttons fade in on hover.** A long transcript is no
  longer a column of little play/copy icons competing with the text.
- **The HUD toggle is a toggle.** It shows whether the HUD is up, the
  icon and tooltip follow the state, F9 and the button no longer drift
  apart, and hiding then showing restores the width he had dragged it to
  rather than snapping back to the default.
- **Sidebar.** A hairline under every card title so a stack of panels
  reads as a stack of panels; ENGINE moved above WEATHER because it is
  the card you need when something is wrong; values too long for the
  column keep their full text in a tooltip instead of ellipsising into
  nothing.
- **The composer has a placeholder.** `Gtk.TextView` has none of its
  own, so it is an overlay label that hides on the first keystroke.
- **New setting: Artwork behind the chat.** The wallpaper that caused
  all this is now a real, switchable feature rather than a side effect.
- **Regression tests.** `tests/test_ui.py` now reads the live provider
  back and fails if the sheet has lost its bubbles, cards, composer or
  headerbar, or if it comes back suspiciously small; and it pins the
  `hexpand`/`halign` pair that makes alignment possible at all.
  Verified by reintroducing both bugs and watching it fail.

## 2.4.0 - Windows

George now runs natively on Windows, and the repo builds a real `.exe`
with GTK4 inside it.

- **New `george_platform.py`.** One module knows which OS this is;
  everything else stays written once. GTK-free like the core, so it is
  testable headless, and its pure parts take an explicit `windows=`
  argument so Windows behaviour can be checked from Linux.
- **The safety tables are merged, not switched.** The Windows
  destructive patterns are live on Linux and vice versa. For a gate,
  over-scanning is the safe direction - and it means the gate that ships
  is the gate the suite actually exercises, whichever box runs it.
  Newly refused: `format C:`, `diskpart`, `vssadmin delete shadows`,
  `reg delete HKLM\...`, `Remove-Item -Recurse C:\`, `Clear-Disk`,
  `Format-Volume`, `takeown /f C:\`, `icacls C:\ /reset`, `bcdedit /set`,
  `sc delete`, `wevtutil cl`, `cipher /w`, `net user ... /delete`.
- **Three new ways past a gate, closed.** Commands are split with BOTH
  POSIX and Windows quoting rules and every reading has to be clean,
  because `del C:\Windows` is two different argvs depending on who
  splits it. `de^l /s /q C:\` is checked with the carets stripped as
  well as as typed. `powershell -EncodedCommand <base64>` is decoded and
  recursed into; if it will not decode, it is refused.
- **The Windows read-only allowlist**: dir, systeminfo, tasklist,
  ipconfig, netstat, where, findstr, tree, certutil -hashfile, the
  query-only forms of reg/sc/netsh/wmic/powercfg/winget, and a named set
  of read-only PowerShell cmdlets. `Format-` is deliberately not
  prefix-matched: Format-Table is harmless and Format-Volume wipes a
  disk.
- **Facts come from ctypes and the registry, not `wmic`** - which
  Windows 11 24H2 removed. CPU load from GetSystemTimes, RAM from
  GlobalMemoryStatusEx, uptime from GetTickCount64, battery from
  GetSystemPowerStatus, GPU from EnumDisplayDevices, and the real
  Windows 11 build number rather than the ProductName that still says
  10.
- **Voice works on a bare Windows install.** SAPI via System.Speech is
  the default engine there - it is already present on every Windows and
  sounds like a person; piper is still preferred when its model is on
  disk. Playback goes through `winsound`, so a 60ms UI blip no longer
  costs a process launch.
- Clipboard through the real Win32 clipboard with a PowerShell fallback;
  screen capture across the whole virtual desktop; volume and media keys
  through WM_APPCOMMAND, the same messages a keyboard's media keys send.
- **Every subprocess gets CREATE_NO_WINDOW.** Miss one and a windowed
  build flashes a black console box every time George checks the time.
- **Ollama teardown kills the process tree.** `ollama serve` forks a
  runner that holds the model and the port; killing the wrapper alone
  left 11434 taken. George still only stops a daemon he started - if the
  Ollama tray app is running, he uses it and leaves it alone.
- `--version` attaches to the terminal that launched it, and falls back
  to a dialog only when double-clicked, because a windowed build has no
  stdout at all.
- **New `tests/test_windows.py`**: 251 checks that run on Linux -
  destructive gate, auto-run gate, parsers, dispatch, and an AST audit
  that hasattr-checks every `osx.*` attribute in every module. pyflakes
  does not resolve those, so without it a renamed function would only
  surface at run time, on Windows, where nobody is watching. It found
  three real gate bugs on its first run.
- **New `.github/workflows/windows.yml`**: MSYS2 UCRT64 -> PyInstaller
  -> Inno Setup installer plus a portable zip, with a smoke test that
  runs the built exe and fails the build if the typelibs, schemas,
  pixbuf loaders or icon theme are missing from the bundle.
- **New `install.ps1`**: `irm ... | iex`, the Windows counterpart to
  install.sh. Same shape, same manners - the uninstall asks separately
  before touching your notes and that question is never answered by
  `GEORGE_YES`.

## 2.3.0

- **He knows what he is running on.** The system prompt now carries a
  real machine line - distro, kernel, arch, desktop and session type,
  CPU, cores, RAM, GPU, package manager, shell, battery - instead of
  just the distro name. He stops answering about "a Linux machine" in
  the abstract and stops asking what distro you use.
- **Read-only commands run without asking.** The allowlist went from 40
  names to about 130: package queries for every distro, git read verbs,
  service status, network inspection, hardware listings, checksums,
  text tools. He can answer questions about the box without a click per
  `uname -a`.
- Widening that list meant closing the ways a read-only tool stops being
  read-only. None of these auto-run any more, whatever the command name
  says: anything containing a redirect, a command substitution or a
  background `&`; anything run through sudo, doas, pkexec or su;
  `sed -i`, `find -delete/-exec`, `awk system()`, `curl -o/-X/-d`,
  `journalctl --vacuum`, `ip link set`, `nmcli up/down`, git verbs that
  write, and `wget` at all - writing a file is its default, not a flag.
- New `tests/test_autorun.py`: 76 commands that must run free and 78
  that must ask, both directions pinned.

## 2.2.0 - eyes, ears and a voice

- **He can see.** New `see` tool: grabs the screen and hands it to a
  local vision model. The image goes to your own ollama, is never
  uploaded, and is deleted the moment it has been read.
- **Ambient mode.** Header button (Ctrl+W) or Settings > Eyes. George
  looks every couple of minutes and chips in - `advice`, `banter` or
  `quiet` (urgent things only). Off by default, and while it is on the
  button stays lit and the core reads WATCHING, so it is never
  ambiguous whether he is looking.
  Restraint is the design: a remark has to clear three bars - the model
  has something to say at all, it is not repeating itself, and the rate
  caps allow it. Defaults to at most one remark every 4 minutes, 8 an
  hour.
- **A real voice, installed for you.** install.sh now fetches piper and
  an `en_GB-alan-medium` voice instead of leaving you on espeak.
  `--voice <name>` for any other piper voice, `--no-voice` to skip.
- **The mic works out of the box.** install.sh installs whisper.cpp and
  the base.en model, so push-to-talk works after a fresh install.
- **A vision model too** - `moondream` by default (1.7 GB, quick on a
  laptop). `--vision <model>` or `--no-vision`.
- **Sound.** Short tones on send, reply, listen, stop, error and when he
  notices something. Synthesised at runtime from the stdlib - no audio
  files shipped. Toggle in Settings > Interface.
- Unprompted remarks get their own bubble style so they never look like
  an answer to something you asked.

## 2.1.0

- **Fixed the icon.** The app registers as `com.thepriest.george` but the
  installer wrote `org.thepriest.george.desktop`, so nothing matched and
  the window fell back to the generic python icon. The desktop file,
  icon file, `Icon=`, `StartupWMClass`, `g_set_prgname()` and the window
  icon name are now all the same string. A stale `org.*` pair from an
  earlier install is removed on upgrade.
- The icon also resolves when running from a checkout that was never
  installed - the app registers its own icon search path.
- **Reasoning trace is now a setting, defaulting to off.** A reasoning
  model thinks before every step and a turn can take fourteen of them,
  so the thinking cost was being paid fourteen times per answer. Sent as
  ollama's top-level `think` field, with a retry that drops the field if
  the server is too old to know it.
- Refreshed the suggested models around what this app actually needs:
  clean JSON on the first try, not prose quality.

## 2.0.0

Rebuilt the interface, gave him a voice worth listening to, and made the
whole thing hold together when something goes wrong.

### Look
- New `george_theme.py`: the stylesheet is built from config, so accent,
  font scale and density change the whole app in one reload. Six accents
  (cyan default). 16px radii, gradient surfaces, pill buttons, hover and
  focus transitions, rounded scrollbars.
- New `george_hud.py`: the instruments, drawn in cairo because GTK's CSS
  has no `@keyframes`. Reactor core (rotating sweep, 48-tick ring lit to
  real CPU load, breathing disc), ring gauges for cpu/ram/disk, a CPU
  sparkline, and a pulsing state dot in the header.
- The core is a state display, not decoration: slow at idle, fast while
  thinking, breathing while speaking, flat red when the engine is down.
- Replies render as rich text: bold, lists, links, and fenced code as its
  own card with a copy button.
- Empty state with clickable suggestion chips instead of a blank pane.
- Keyboard: Enter, Shift+Enter, Esc, Ctrl+K/N/M/H, Ctrl+comma, F5, F9.
- pycairo is optional. Without it every instrument degrades to a plain
  widget instead of taking the window down.

### Voice
- Three personas: jarvis (default), plain, blunt.
- Speech normalisation: GiB reads as gigabytes, 87% as 87 percent, code
  fences as "code on screen", URLs as "the link on screen".
- Piper picks a voice matching your locale instead of the first file it
  finds. espeak gets a configurable pitch and an en-gb voice.

### Can do
- Eight new tools: write_file, find, processes, network, disk, volume,
  power, open_path. 28 in total.
- Writes are sandbox-checked before the file is opened, and confirmed
  unless you turn that off. `power` has no code path that skips the
  confirmation.

### Holds together
- Config coercion: every value is forced to the shape of its default and
  clamped to a sane range. A hand-edited config cannot hand a widget a
  string. Invalid JSON is kept as `config.json.broken` rather than wiped.
- Tool watchdog: a wedged tool is abandoned and the turn carries on. Time
  spent reading a confirmation dialog does not count against it.
- Crash handlers on the main thread and workers, log rotation at 2 MB,
  HTTP retries, and every UI callback wrapped so an exception cannot kill
  a GLib idle handler silently.
- Ollama errors say what to do about them; a model tag that is not pulled
  falls back to one that is; a stalled stream says so.
- install.sh installs the two new modules and pycairo, and no longer
  leaks a bash error when uninstalling without a terminal.

## 1.0.0
First release. GTK4 shell, local Ollama brain, 20 tools, voice in and
out, ollama lifecycle tied to the app, in-app model manager, one-line
installer.
