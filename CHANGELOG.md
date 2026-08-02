# George changelog

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
