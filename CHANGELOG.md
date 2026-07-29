# Changelog

## 1.0.0

First release. George is Basilisk's shell with the security arsenal
removed and a Jarvis in its place.

- GTK4/libadwaita window: HUD sidebar (clock, live system vitals, engine
  status, weather, news, saved chats), chat transcript, live per-step
  action feed, send button that becomes stop mid-turn
- 20 tools: web search and page reading, RSS news that lands on your
  screen, weather, system vitals, one shell command at a time, app
  launching, media and volume, clipboard, screenshots, sandboxed file
  reads, notes, long-term memory, arithmetic, timers, speech
- Local Ollama only. `deepseek-r1:7b` by default, no API key anywhere in
  the program
- Ollama starts with the app and stops with it, but only if George was
  the one that started it; systemd-owned and already-running daemons are
  used and left alone
- Model manager in-app: list, switch, delete, and pull anything from
  ollama.com/library with a progress bar
- Saved chats in the sidebar, click to reload, auto-delete after 24h
- Piper/espeak speech out, whisper.cpp/faster-whisper push-to-talk, all
  local
- Structural destructive-command gate with no override; `curl | bash`
  refused; file access sandboxed to home
- `install.sh`: one-line install tuned for CachyOS, works across
  pacman/apt/dnf/zypper/apk/xbps, picks the CUDA or ROCm ollama build on
  Arch, `--uninstall` included
