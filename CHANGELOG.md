# George changelog

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
