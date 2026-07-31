# George changelog

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
