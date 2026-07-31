# George

**Basilisk's brother.** Same GTK4/libadwaita shell, same agent loop, same
live action feed, same voice stack — with the security arsenal taken out
and a Jarvis put in its place.

Everything runs on your box. Local Ollama, no API key, no cloud call, no
telemetry. There is no key field in this program because there is nothing
to put in it.

## Install

```
curl -fsSL https://raw.githubusercontent.com/the-priest/George/main/install.sh | bash
```

That one line does the lot: detects your distro, installs the GTK4 stack,
installs Ollama (the CUDA or ROCm build if it sees your GPU), pulls
`deepseek-r1:7b`, drops George in `~/.local/share/george` with a launcher,
a desktop entry and an icon.

Tuned for CachyOS, works on Arch, Debian/Ubuntu, Fedora/RHEL, openSUSE,
Alpine and Void.

| flag | does |
|---|---|
| `--yes` | no prompts (does **not** auto-approve piping remote scripts to a shell) |
| `--model <tag>` | pull something other than `deepseek-r1:7b` |
| `--no-model` | skip the multi-GB download, grab it later in-app |
| `--no-deps` | you already have GTK4 and PyGObject |
| `--deps-only` | dependencies, nothing else |
| `--allow-remote-ollama` | permit the official `ollama.com/install.sh` fallback |
| `--uninstall` | remove it (asks separately before touching your data) |

Then:

```
george
```

or launch it from your application menu. Trailing words are sent as your
first message: `george what's on the news`.

## The engine comes up and goes down with the app

George starts `ollama serve` when it launches and stops it when you close
the window — but **only the daemon it started itself**. If Ollama was
already running, or systemd owns it, George uses it and leaves it alone on
exit. Killing a service you started is not George's to do.

The **ENGINE** card in the sidebar always says which of those it is.

## Models, from inside the app

Menu → **Models**:

- everything on the box, with sizes, one click to make it active, one to delete
- a suggested list (deepseek-r1 7b/8b/14b, qwen2.5, llama3.1, mistral, gemma2, phi4, coder, llava)
- a box to pull any tag from `ollama.com/library`, with a live progress bar

## What it does

| tool | |
|---|---|
| `web_search` `open_page` | DuckDuckGo, then reads the page |
| `news` `show` | pulls your feeds into the HUD, opens the story **on your screen** |
| `weather` `system` | wttr.in; cpu, memory, disk, battery, temp |
| `run` `launch` | one shell command; start any app |
| `media` `clipboard` `screenshot` | playerctl/wpctl, wl-clipboard, grim |
| `read_file` `list_dir` `note` | sandboxed to your home |
| `remember` `recall` `forget` | facts it keeps for good |
| `calc` `timer` `say` | arithmetic, desktop notifications, speech |

"Show me the news" pulls the feeds, paints the headlines in the sidebar and
opens the story in your browser. That is the difference between an
assistant and a chatbot.

## Saved chats

Listed in the sidebar, click to reload, bin icon to delete, and the whole
lot auto-deletes after 24h (change or disable it in Settings → Interface).

## Voice

Piper if you have a `.onnx` voice on disk, espeak-ng otherwise, `spd-say`
as a last resort. Replies are read aloud by default; every message has its
own play button.

Push-to-talk uses whisper.cpp (`whisper-cli`) or `faster-whisper` if either
is installed. If neither is, the mic button is disabled and tells you why
rather than pretending.

## Safety

George is not a security tool and will say so if you ask it to be one.
What it does enforce:

- **Destructive commands are refused structurally**, at the execution
  primitive, with no override and no setting. The decision is a pure
  function of the command string — it never touches the filesystem,
  because anything that can create a file could otherwise move the
  boundary. It sees through `sh -c`, `sudo -u root`, `env`, `timeout`,
  `nice`, `setsid`, `$(...)`, backticks, `${IFS}` and `;`/`&&`/`|` chains,
  and if it cannot parse a command it refuses it.
- Anything that is not read-only asks for one click. Flip
  **Behaviour → Run commands without asking** if you want it autonomous.
- `curl … | bash` is refused.
- File reads and writes stay under the sandbox root (default `~`).
- `calc` is an AST whitelist, not `eval`.

## Layout

```
george.py         GTK4 shell, HUD, dialogs, model manager
george_core.py    config, safety gate, stores, HTTP, system, ollama lifecycle
george_tools.py   tool registry, prompt, action parsing, agent loop
george_voice.py   piper/espeak TTS, whisper STT
install.sh        the one-liner
```

Only `george.py` imports GTK. The whole core runs headless, which is what
makes it testable without a display.

## Verification

- 480 wrapper × command combinations through the destructive gate: 0 missed, 0 false positives
- 4000 random strings through every parser and gate: 0 crashes
- agent loop end to end against a mock Ollama: streaming, tool dispatch, observation feedback, stop-mid-stream, step ceiling
- daemon lifecycle: starts, a second instance does not steal ownership, shutdown leaves no process-group leftovers, idempotent, useful message when the binary is missing
- the real GTK4 UI launched headless under Xvfb: window builds, a full turn runs, every dialog opens, theme reload and font scaling applied — 0 errors
- `install.sh`: `bash -n` and `shellcheck -S warning` clean; install, launch-from-installed-path, and uninstall all exercised

## Config

`~/.config/george/config.json` — everything in it is also in Settings.
Memory, notes, chats and logs live in `~/.local/share/george/`.

## What is on screen

The left column is a HUD, not a menu. The **core** at the top is a real
gauge: the ring fills with CPU load, spins faster while George is
working, breathes while he is talking, and goes red when the engine is
down. Under it: ring gauges for cpu, ram and disk, a CPU history
sparkline, the weather, the engine state, headlines, and saved chats.

Accent colour, density, font scale and animation are all in
Settings > Interface. Turning animation off stops every cairo timer.

## Eyes

`see` reads your screen with a local vision model. Ambient mode (the
header button, or Ctrl+W) lets him watch and chip in on his own - off
until you turn it on, and obvious while it is running.

Screenshots go to the ollama on your machine, are never uploaded, and
are deleted as soon as they have been read.

## Keyboard

| key | does |
| --- | --- |
| Enter | send |
| Shift+Enter | newline |
| Esc | stop the current turn |
| Ctrl+K | jump to the box |
| Ctrl+N | new conversation |
| Ctrl+M | push to talk |
| Ctrl+H | recent chats |
| Ctrl+, | settings |
| F5 | refresh news |
| F9 | toggle the HUD |
| Ctrl+W | let George watch the screen |

## Personas

`jarvis` (default), `plain`, `blunt` - Settings > Behaviour. It changes
how he talks, never what he is allowed to do.

## If the HUD looks flat

That means pycairo is missing and the instruments fell back to plain
widgets. Install `python-cairo` (Arch), `python3-gi-cairo` (Debian),
`python3-cairo` (Fedora/openSUSE) and restart.
