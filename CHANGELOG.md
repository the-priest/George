# George changelog

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
