#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_core.py -- plumbing for George.

Config, logging, the destructive-command gate, persistent stores, HTTP,
the system layer, and everything that talks to Ollama (including
starting the daemon with the app and stopping it again on the way out).

Deliberately GTK-free: every line in here runs headless, which is what
makes it testable without a display.
"""

from __future__ import annotations

import ast
import html as _html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple


APP_ID = "com.thepriest.george"
APP_NAME = "George"
VERSION = "1.0.0"

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "george")
DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")),
    "george")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
MEMORY_PATH = os.path.join(DATA_DIR, "memory.json")
NOTES_PATH = os.path.join(DATA_DIR, "notes.md")
CHATS_PATH = os.path.join(DATA_DIR, "chats.json")
LOG_PATH = os.path.join(DATA_DIR, "george.log")

DEFAULT_FEEDS = [
    ["RTE", "https://www.rte.ie/feeds/rss/?index=/news/"],
    ["BBC", "https://feeds.bbci.co.uk/news/rss.xml"],
    ["Irish Times", "https://www.irishtimes.com/arc/outboundfeeds/feed-irish-news/"],
    ["Reuters World", "https://www.reutersagency.com/feed/?best-topics=world"],
    ["Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"],
    ["Hacker News", "https://hnrss.org/frontpage"],
    ["The Register", "https://www.theregister.com/headlines.atom"],
]

DEFAULTS: Dict[str, Any] = {
    "ollama_url": "http://localhost:11434",
    "model": "deepseek-r1:7b",
    "temperature": 0.6,
    "num_ctx": 8192,
    "keep_alive": "30m",
    "max_steps": 14,
    "request_timeout": 300,

    "voice_enabled": True,
    "voice_engine": "auto",          # auto | piper | espeak | none
    "voice_speed": 1.0,
    "piper_model": "",
    "stt_enabled": True,

    "auto_run_commands": False,      # False = confirm every shell command
    "allow_writes": False,           # file writes outside the notes file
    "sandbox_root": HOME,

    "font_scale": 1.0,
    "transcript_live_rows": 40,      # widgets kept in the view
    "chat_retention_hours": 24,

    "feeds": DEFAULT_FEEDS,
    "news_count": 12,
    "location": "",                  # blank = wttr.in geo-IP guess
    "browser": "",                   # blank = xdg-open
    "user_name": "",
}


def _ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def load_config() -> Dict[str, Any]:
    _ensure_dirs()
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        if isinstance(disk, dict):
            for k, v in disk.items():
                if k in DEFAULTS:
                    cfg[k] = v
    except (OSError, ValueError):
        pass
    if os.environ.get("GEORGE_OLLAMA"):
        cfg["ollama_url"] = os.environ["GEORGE_OLLAMA"].rstrip("/")
    if os.environ.get("GEORGE_MODEL"):
        cfg["model"] = os.environ["GEORGE_MODEL"]
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    _ensure_dirs()
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, sort_keys=True)
        os.replace(tmp, CONFIG_PATH)
    except OSError as exc:
        log("config save failed: %s" % exc)


_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        try:
            _ensure_dirs()
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    if os.environ.get("GEORGE_DEBUG"):
        sys.stderr.write(line + "\n")


# =====================================================================
# SAFETY  --  structural destructive-command gate
#
# Lifted in spirit from basilisk_safety.py.  Two rules learned the hard
# way over there and kept here:
#   1. The decision is a PURE FUNCTION OF THE COMMAND STRING.  It never
#      touches the filesystem.  The moment an authorisation check reads
#      mutable disk state, anything that can create a file moves the
#      boundary -- and the agent creates files.
#   2. It FAILS CLOSED.  If the string cannot be parsed, it is refused.
# =====================================================================

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "busybox"}
_WRAPPERS = {"sudo", "doas", "pkexec", "env", "nice", "ionice", "setsid",
             "nohup", "time", "timeout", "stdbuf", "unbuffer", "command",
             "exec", "xargs", "watch", "script", "chrt", "firejail"}

_CRITICAL_DIRS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64",
    "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/usr",
    "/var", "/home", "/efi", "/boot/efi",
}

_DISK_SINKS = re.compile(
    r"^/dev/(sd[a-z]|nvme\d+n\d+|vd[a-z]|hd[a-z]|mmcblk\d+|loop\d+|disk|"
    r"sr\d+)", re.I)

_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")

_HARD_NO = (
    re.compile(r"\bmkfs(\.\w+)?\b", re.I),
    re.compile(r"\bfdisk\b|\bsfdisk\b|\bparted\b|\bwipefs\b", re.I),
    re.compile(r"\bshred\b", re.I),
    re.compile(r"\bdd\b[^|;&]*\bof=/dev/", re.I),
    re.compile(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)", re.I),
    re.compile(r"\bchown\b[^|;&]*\s+-\w*R\w*\s+[^|;&]*\s+/(\s|$)", re.I),
    re.compile(r"\bchmod\b[^|;&]*\s+-\w*R\w*\s+[0-7]{3,4}\s+/(\s|$)", re.I),
    re.compile(r"\bsystemctl\b\s+(mask|isolate)\b", re.I),
    re.compile(r"\buserdel\b|\bgroupdel\b|\bpasswd\b\s+-d", re.I),
    re.compile(r"\biptables\b\s+-F|\bnft\b\s+flush\b", re.I),
    re.compile(r"\bcrypsetup\b|\bcryptsetup\b\s+(erase|luksErase)", re.I),
    re.compile(r"\bhistory\b\s+-c", re.I),
)

# curl|bash and friends: piping the network straight into a shell.
_PIPE_TO_SHELL = re.compile(
    r"\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?"
    r"(sh|bash|zsh|dash|python3?|perl|ruby|node)\b", re.I)


def _normalise(command: str) -> str:
    s = command.replace("${IFS}", " ").replace("$IFS", " ")
    s = s.replace("\r", "\n")
    return s


def _lift_substitutions(command: str) -> List[str]:
    """Return inner text of $(...) and `...` spans so they get re-parsed.
    A command hidden inside a substitution still runs."""
    out: List[str] = []
    i = 0
    n = len(command)
    while i < n - 1:
        if command[i] == "$" and command[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < n and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            out.append(command[i + 2:j - 1])
            i = j
            continue
        if command[i] == "`":
            j = command.find("`", i + 1)
            if j == -1:
                break
            out.append(command[i + 1:j])
            i = j + 1
            continue
        i += 1
    return out


def _split_subcommands(command: str) -> List[str]:
    parts = re.split(r"\|\||&&|\||;|\n|&(?!>)", command)
    return [p.strip() for p in parts if p and p.strip()]


def _argv(sub: str) -> Optional[List[str]]:
    try:
        return shlex.split(sub, comments=True)
    except ValueError:
        return None


def _base(arg: str) -> str:
    return os.path.basename(arg.strip().strip("'\""))


def _strip_wrappers(args: List[str]) -> List[str]:
    """Peel sudo/env/timeout/... prefixes off the front of an argv."""
    out = list(args)
    guard = 0
    while out and guard < 12:
        guard += 1
        head = _base(out[0])
        if head not in _WRAPPERS:
            break
        out = out[1:]
        # drop flags and, for env, VAR=VALUE assignments
        while out and (out[0].startswith("-") or
                       re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", out[0]) or
                       re.match(r"^\d+(\.\d+)?[smhd]?$", out[0])):
            out = out[1:]
    return out


def _norm_target(t: str) -> str:
    t = t.strip().strip("'\"")
    # $HOME and ~ are the same directory; a gate that only knows one of
    # them is a gate you get past by typing the other.
    t = t.replace("${HOME}", HOME).replace("$HOME", HOME)
    if t.startswith("~"):
        t = HOME + t[1:]
    t = re.sub(r"/+", "/", t)
    if len(t) > 1:
        t = t.rstrip("/")
    return t


def _is_catastrophic_target(t: str) -> bool:
    t = _norm_target(t)
    if not t:
        return False
    if t in ("/", "/*", "*", ".", "..", HOME, HOME + "/*"):
        return True
    if t in _CRITICAL_DIRS:
        return True
    if re.match(r"^/(etc|usr|var|boot|bin|sbin|lib|lib64|home|root|sys|proc)"
                r"(/\*)?$", t):
        return True
    if _DISK_SINKS.match(t):
        return True
    # /home/<user> or /home/<user>/* -- a whole account
    if re.match(r"^/home/[^/]+(/\*)?$", t):
        return True
    return False


# Commands worth inspecting wherever they turn up.  Enumerating every
# wrapper prefix (sudo -u root, env, timeout, xargs, script -qc...) is
# unwinnable, so instead: if one of these names appears at any position
# the wrapper peel could not attribute, re-check the argv from there.
# For a destructive-command gate, over-scanning is the safe direction.
_DANGEROUS_CMDS = {
    "rm", "dd", "mkfs", "shred", "wipefs", "fdisk", "sfdisk", "parted",
    "mv", "cp", "chmod", "chown", "chgrp", "truncate", "tee", "ln",
    "shutdown", "poweroff", "halt", "reboot", "init", "pacman", "apt",
    "apt-get", "dnf", "zypper",
}


def _sub_is_catastrophic(args: List[str]) -> bool:
    peeled = _strip_wrappers(args)
    if _check_argv(peeled):
        return True
    for i, tok in enumerate(args):
        if i == 0:
            continue
        base = _base(tok)
        if base in _DANGEROUS_CMDS or base.startswith("mkfs."):
            if _check_argv(args[i:]):
                return True
    return False


def _check_argv(args: List[str]) -> bool:
    if not args:
        return False
    cmd = _base(args[0])
    rest = args[1:]

    if cmd in _SHELLS:
        for i, a in enumerate(rest):
            if a in ("-c", "-lc", "-ic") and i + 1 < len(rest):
                return is_destructive_command(rest[i + 1], _depth=1)
        return False

    if cmd == "rm":
        flags = "".join(a for a in rest if a.startswith("-"))
        recursive = ("r" in flags.lower() or "--recursive" in rest)
        operands = [a for a in rest if not a.startswith("-")]
        for t in operands:
            if _is_catastrophic_target(t):
                return True
            if recursive and _norm_target(t).count("/") <= 1 and \
                    _norm_target(t).startswith("/"):
                return True
        return False

    if cmd in ("mv", "cp", "dd", "truncate", "tee", "ln"):
        operands = [a for a in rest if not a.startswith("-")]
        for t in operands:
            if _is_catastrophic_target(t):
                return True
        for a in rest:
            if a.startswith("of=") and _DISK_SINKS.match(_norm_target(a[3:])):
                return True
        return False

    if cmd in ("chmod", "chown", "chgrp"):
        operands = [a for a in rest if not a.startswith("-")]
        for t in operands[1:]:
            if _is_catastrophic_target(t):
                return True
        return False

    if cmd in ("shutdown", "poweroff", "halt", "reboot", "init"):
        return True

    if cmd in ("pacman", "apt", "apt-get", "dnf", "zypper") and rest:
        joined = " ".join(rest)
        if re.search(r"\b(-R\w*s\w*|remove\s+--?\w*all|autoremove)\b.*\b"
                     r"(base|linux|systemd|glibc|pacman|coreutils)\b", joined):
            return True
    return False


def is_destructive_command(command: str, _depth: int = 0) -> bool:
    """True if the command could brick the box or nuke the user's data."""
    if _depth > 4:
        return True                       # fail closed on absurd nesting
    if not command or not command.strip():
        return False
    s = _normalise(command)

    if _FORK_BOMB.search(s):
        return True
    for rx in _HARD_NO:
        if rx.search(s):
            return True

    for inner in _lift_substitutions(s):
        if is_destructive_command(inner, _depth + 1):
            return True

    for sub in _split_subcommands(s):
        args = _argv(sub)
        if args is None:
            return True                   # unparseable -> refuse
        if _sub_is_catastrophic(args):
            return True

    # redirection into a device node
    if re.search(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)", s, re.I):
        return True
    return False


def is_network_pipe_to_shell(command: str) -> bool:
    return bool(_PIPE_TO_SHELL.search(_normalise(command)))


def command_needs_confirmation(command: str, cfg: Dict[str, Any]) -> bool:
    """Read-only inspection runs free; everything else asks, unless the
    operator has flipped auto-run on."""
    if cfg.get("auto_run_commands"):
        return False
    s = _normalise(command)
    subs = _split_subcommands(s)
    safe = {
        "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "stat",
        "file", "du", "df", "free", "uptime", "uname", "whoami", "id",
        "date", "hostname", "ps", "top", "pgrep", "which", "whereis",
        "echo", "printf", "pwd", "env", "lsblk", "lscpu", "lsusb", "lspci",
        "ip", "sensors", "systemctl", "journalctl", "pacman", "neofetch",
        "fastfetch", "nvidia-smi", "sort", "uniq", "cut", "awk", "sed",
        "jq", "tree", "mount", "nmcli", "bluetoothctl", "playerctl",
        "wpctl", "pactl", "xdg-open", "notify-send", "curl", "ping",
    }
    for sub in subs:
        args = _argv(sub)
        if not args:
            return True
        args = _strip_wrappers(args)
        if not args:
            return True
        base = _base(args[0])
        if base not in safe:
            return True
        if base == "systemctl" and not any(
                a in ("status", "list-units", "list-unit-files", "is-active",
                      "is-enabled", "show", "cat") for a in args[1:]):
            return True
        if base == "pacman" and not any(a.startswith("-Q") or a.startswith("-S")
                                        and "i" in a for a in args[1:]):
            return True
        if base == "sed" and "-i" in args:
            return True
    return False


def inside_sandbox(path: str, cfg: Dict[str, Any]) -> bool:
    root = os.path.realpath(os.path.expanduser(
        cfg.get("sandbox_root") or HOME))
    try:
        target = os.path.realpath(os.path.expanduser(path))
    except OSError:
        return False
    return target == root or target.startswith(root + os.sep)


# =====================================================================
# STORES  --  long-term memory, notes, chat history
# =====================================================================

_store_lock = threading.RLock()


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: str, obj: Any) -> None:
    _ensure_dirs()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        log("write %s failed: %s" % (path, exc))


class MemoryStore:
    """Flat key/value long-term memory the model can write to itself."""

    def __init__(self) -> None:
        with _store_lock:
            self._d: Dict[str, str] = _read_json(MEMORY_PATH, {})
            if not isinstance(self._d, dict):
                self._d = {}

    def remember(self, key: str, value: str) -> None:
        with _store_lock:
            self._d[key.strip().lower()] = value.strip()
            _write_json(MEMORY_PATH, self._d)

    def forget(self, key: str) -> bool:
        with _store_lock:
            gone = self._d.pop(key.strip().lower(), None) is not None
            if gone:
                _write_json(MEMORY_PATH, self._d)
            return gone

    def recall(self, key: str = "") -> Dict[str, str]:
        with _store_lock:
            if not key:
                return dict(self._d)
            k = key.strip().lower()
            return {kk: vv for kk, vv in self._d.items()
                    if k in kk or k in vv.lower()}

    def as_prompt_block(self, limit: int = 25) -> str:
        with _store_lock:
            items = list(self._d.items())[:limit]
        if not items:
            return ""
        return "\n".join("- %s: %s" % (k, v) for k, v in items)


class ChatStore:
    """Sessions on disk, auto-purged after cfg['chat_retention_hours']."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        with _store_lock:
            raw = _read_json(CHATS_PATH, [])
            self.sessions: List[Dict[str, Any]] = raw if isinstance(raw, list) else []
        self.purge()

    def purge(self) -> None:
        hours = float(self.cfg.get("chat_retention_hours") or 24)
        if hours <= 0:
            return
        cutoff = time.time() - hours * 3600.0
        with _store_lock:
            before = len(self.sessions)
            self.sessions = [s for s in self.sessions
                             if float(s.get("ts", 0)) >= cutoff]
            if len(self.sessions) != before:
                _write_json(CHATS_PATH, self.sessions)

    def save(self, session_id: str, title: str,
             messages: List[Dict[str, str]]) -> None:
        with _store_lock:
            for s in self.sessions:
                if s.get("id") == session_id:
                    s["title"] = title
                    s["messages"] = messages
                    s["ts"] = time.time()
                    break
            else:
                self.sessions.insert(0, {"id": session_id, "title": title,
                                         "messages": messages,
                                         "ts": time.time()})
            self.sessions = self.sessions[:60]
            _write_json(CHATS_PATH, self.sessions)

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        with _store_lock:
            for s in self.sessions:
                if s.get("id") == session_id:
                    return s
        return None

    def delete(self, session_id: str) -> bool:
        with _store_lock:
            before = len(self.sessions)
            self.sessions = [s for s in self.sessions
                             if s.get("id") != session_id]
            if len(self.sessions) != before:
                _write_json(CHATS_PATH, self.sessions)
                return True
        return False

    def listing(self) -> List[Tuple[str, str, float]]:
        with _store_lock:
            return [(s.get("id", ""), s.get("title", "(untitled)"),
                     float(s.get("ts", 0))) for s in self.sessions]


# =====================================================================
# OLLAMA  --  the only brain, always local, never a key
# =====================================================================

class OllamaError(RuntimeError):
    pass


class Ollama:

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg

    @property
    def base(self) -> str:
        return str(self.cfg.get("ollama_url", DEFAULTS["ollama_url"])).rstrip("/")

    def _post(self, path: str, payload: Dict[str, Any], timeout: int):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        return urllib.request.urlopen(req, timeout=timeout)

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/api/tags", timeout=4):
                return True
        except Exception:
            return False

    def models(self) -> List[str]:
        try:
            with urllib.request.urlopen(self.base + "/api/tags", timeout=8) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return sorted(m.get("name", "") for m in data.get("models", []))
        except Exception as exc:
            log("model list failed: %s" % exc)
            return []

    def chat_stream(self, messages: List[Dict[str, str]],
                    on_token: Callable[[str], None],
                    stop: threading.Event) -> str:
        """Stream a reply.  Returns the full text.  Honours the stop event
        between chunks so the UI stop button is never a lie."""
        payload = {
            "model": self.cfg.get("model", DEFAULTS["model"]),
            "messages": messages,
            "stream": True,
            "keep_alive": self.cfg.get("keep_alive", "30m"),
            "options": {
                "temperature": float(self.cfg.get("temperature", 0.6)),
                "num_ctx": int(self.cfg.get("num_ctx", 8192)),
            },
        }
        timeout = int(self.cfg.get("request_timeout", 300))
        chunks: List[str] = []
        try:
            resp = self._post("/api/chat", payload, timeout)
        except urllib.error.URLError as exc:
            raise OllamaError(
                "cannot reach ollama at %s (%s). is `ollama serve` running?"
                % (self.base, exc)) from exc
        with resp:
            for raw in resp:
                if stop.is_set():
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if obj.get("error"):
                    raise OllamaError(str(obj["error"]))
                piece = (obj.get("message") or {}).get("content", "")
                if piece:
                    chunks.append(piece)
                    on_token(piece)
                if obj.get("done"):
                    break
        return "".join(chunks)


THINK_OPEN = re.compile(r"<think>", re.I)
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.I | re.S)


def strip_reasoning(text: str) -> str:
    """deepseek-r1 wraps its scratchpad in <think>.  Remove complete
    blocks, and if the tag opened and never closed, drop the tail."""
    out = THINK_BLOCK.sub("", text)
    if THINK_OPEN.search(out):
        out = THINK_OPEN.split(out)[0]
    return out.replace("</think>", "").strip()


def reasoning_of(text: str) -> str:
    parts = re.findall(r"<think>(.*?)</think>", text, re.I | re.S)
    if parts:
        return "\n".join(p.strip() for p in parts)
    m = THINK_OPEN.split(text)
    return m[1].strip() if len(m) > 1 else ""


# =====================================================================
# WEB  --  search, fetch, readable text, RSS
# =====================================================================

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
      "Gecko) Chrome/126.0 Safari/537.36")

_TAG_DROP = re.compile(
    r"<(script|style|noscript|svg|nav|footer|form|aside|header)\b.*?</\1>",
    re.I | re.S)
_TAG_BREAK = re.compile(r"</(p|div|li|tr|h[1-6]|section|article)>", re.I)
_TAG_ANY = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\x0b\f]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(raw: str, limit: int = 8000) -> str:
    s = _TAG_DROP.sub(" ", raw)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _TAG_BREAK.sub("\n", s)
    s = _TAG_ANY.sub(" ", s)
    s = _html.unescape(s)
    s = _WS.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))
    s = _NL.sub("\n\n", s).strip()
    return s[:limit]


def http_get(url: str, timeout: int = 20, limit: int = 400000) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(limit)
    charset = "utf-8"
    m = re.search(rb'charset=["\']?([\w\-]+)', data[:2048], re.I)
    if m:
        charset = m.group(1).decode("ascii", "ignore")
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def _unwrap_ddg(href: str) -> str:
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        q = urllib.parse.urlparse(href).query
        for k, v in urllib.parse.parse_qsl(q):
            if k == "uddg":
                return v
    if href.startswith("//"):
        return "https:" + href
    return href


def web_search(query: str, count: int = 6) -> List[Dict[str, str]]:
    """DuckDuckGo HTML endpoint.  No key, no account, no telemetry beyond
    the request itself."""
    results: List[Dict[str, str]] = []
    endpoints = [
        ("https://html.duckduckgo.com/html/?q=" +
         urllib.parse.quote_plus(query)),
        ("https://lite.duckduckgo.com/lite/?q=" +
         urllib.parse.quote_plus(query)),
    ]
    for url in endpoints:
        try:
            page = http_get(url, timeout=20)
        except Exception as exc:
            log("search %s failed: %s" % (url, exc))
            continue
        for m in re.finditer(
                r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                page, re.I | re.S):
            results.append({"url": _unwrap_ddg(_html.unescape(m.group(1))),
                            "title": html_to_text(m.group(2), 300),
                            "snippet": ""})
        if not results:
            for m in re.finditer(
                    r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    page, re.I | re.S):
                results.append({"url": _unwrap_ddg(_html.unescape(m.group(1))),
                                "title": html_to_text(m.group(2), 300),
                                "snippet": ""})
        snips = [html_to_text(s, 400) for s in re.findall(
            r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', page,
            re.I | re.S)]
        for i, sn in enumerate(snips):
            if i < len(results):
                results[i]["snippet"] = sn
        if results:
            break
    seen = set()
    clean: List[Dict[str, str]] = []
    for r in results:
        u = r.get("url", "")
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        clean.append(r)
        if len(clean) >= count:
            break
    return clean


def _feed_entries(xml_text: str, source: str,
                  limit: int = 20) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace"))
    except ET.ParseError as exc:
        log("feed parse failed (%s): %s" % (source, exc))
        return out
    ns = "{http://www.w3.org/2005/Atom}"

    for item in root.iter():
        tag = item.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = link = when = summary = ""
        for child in item:
            ctag = child.tag.split("}")[-1].lower()
            if ctag == "title":
                title = (child.text or "").strip()
            elif ctag == "link":
                link = (child.get("href") or child.text or "").strip()
            elif ctag in ("pubdate", "published", "updated", "date"):
                when = (child.text or "").strip()
            elif ctag in ("description", "summary", "content"):
                summary = html_to_text(child.text or "", 400)
        if title:
            out.append({"title": _html.unescape(title), "url": link,
                        "when": when, "summary": summary, "source": source})
        if len(out) >= limit:
            break
    del ns
    return out


def fetch_news(feeds: List[List[str]], per_feed: int = 5,
               topic: str = "") -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for entry in feeds:
        try:
            name, url = entry[0], entry[1]
        except (IndexError, TypeError):
            continue
        try:
            items.extend(_feed_entries(http_get(url, timeout=15), name,
                                       per_feed))
        except Exception as exc:
            log("feed %s failed: %s" % (name, exc))
    if topic:
        t = topic.lower()
        items = [i for i in items
                 if t in i["title"].lower() or t in i["summary"].lower()]
    return items


# =====================================================================
# SYSTEM  --  vitals, media, clipboard, launching, notifications
# =====================================================================

def run_shell(command: str, timeout: int = 60,
              cwd: Optional[str] = None) -> Tuple[int, str]:
    try:
        proc = subprocess.run(command, shell=True, capture_output=True,
                              text=True, timeout=timeout, cwd=cwd,
                              errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "[timed out after %ds]" % timeout
    except Exception as exc:                       # pragma: no cover
        return 1, "[failed to launch: %s]" % exc
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _read_first(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def system_status() -> Dict[str, str]:
    st: Dict[str, str] = {}
    uname = os.uname()
    st["host"] = uname.nodename
    st["kernel"] = "%s %s" % (uname.sysname, uname.release)
    st["distro"] = "unknown"
    for line in _read_first("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            st["distro"] = line.split("=", 1)[1].strip().strip('"')
            break

    up = _read_first("/proc/uptime", "0 0").split()
    try:
        secs = int(float(up[0]))
        st["uptime"] = "%dd %dh %dm" % (secs // 86400, (secs % 86400) // 3600,
                                        (secs % 3600) // 60)
    except (ValueError, IndexError):
        st["uptime"] = "?"

    st["load"] = " ".join(_read_first("/proc/loadavg", "").split()[:3])

    mem: Dict[str, int] = {}
    for line in _read_first("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                mem[parts[0][:-1]] = int(parts[1])
            except ValueError:
                pass
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    if total:
        used = total - avail
        st["memory"] = "%.1f / %.1f GiB (%d%%)" % (
            used / 1048576.0, total / 1048576.0, round(100.0 * used / total))
        st["mem_pct"] = str(round(100.0 * used / total))
    try:
        du = shutil.disk_usage(HOME)
        st["disk"] = "%.0f / %.0f GiB free on home" % (
            du.free / 1073741824.0, du.total / 1073741824.0)
        st["disk_pct"] = str(round(100.0 * (du.total - du.free) / du.total))
    except OSError:
        pass

    base = "/sys/class/power_supply"
    try:
        for name in sorted(os.listdir(base)):
            if name.startswith("BAT"):
                cap = _read_first(os.path.join(base, name, "capacity"))
                state = _read_first(os.path.join(base, name, "status"))
                if cap:
                    st["battery"] = "%s%% (%s)" % (cap, state or "?")
                break
    except OSError:
        pass

    cpu_t = ""
    try:
        for zone in sorted(os.listdir("/sys/class/thermal")):
            if zone.startswith("thermal_zone"):
                raw = _read_first("/sys/class/thermal/%s/temp" % zone)
                if raw.isdigit():
                    cpu_t = "%.0f C" % (int(raw) / 1000.0)
                    break
    except OSError:
        pass
    if cpu_t:
        st["temp"] = cpu_t
    return st


def weather(location: str = "") -> Dict[str, str]:
    loc = urllib.parse.quote(location.strip()) if location.strip() else ""
    url = "https://wttr.in/%s?format=j1" % loc
    try:
        raw = http_get(url, timeout=15, limit=200000)
        data = json.loads(raw)
    except Exception as exc:
        return {"error": "weather lookup failed: %s" % exc}
    cur = (data.get("current_condition") or [{}])[0]
    area = (data.get("nearest_area") or [{}])[0]
    name = ""
    try:
        name = area["areaName"][0]["value"]
        name += ", " + area["country"][0]["value"]
    except (KeyError, IndexError, TypeError):
        name = location or "here"
    desc = ""
    try:
        desc = cur["weatherDesc"][0]["value"]
    except (KeyError, IndexError, TypeError):
        pass
    today = (data.get("weather") or [{}])[0]
    return {
        "place": name,
        "temp_c": cur.get("temp_C", "?"),
        "feels_c": cur.get("FeelsLikeC", "?"),
        "desc": desc,
        "wind_kph": cur.get("windspeedKmph", "?"),
        "humidity": cur.get("humidity", "?"),
        "max_c": today.get("maxtempC", "?"),
        "min_c": today.get("mintempC", "?"),
    }


def open_in_browser(url: str, cfg: Dict[str, Any]) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    browser = (cfg.get("browser") or "").strip()
    cmd = [browser, url] if browser else ["xdg-open", url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return "opened %s on screen" % url
    except Exception as exc:
        return "could not open browser: %s" % exc


def launch_app(name: str) -> str:
    name = name.strip()
    if not name:
        return "no application given"
    exe = shutil.which(name)
    if exe:
        try:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return "launched %s" % name
        except Exception as exc:
            return "launch failed: %s" % exc
    if shutil.which("gtk-launch"):
        rc, out = run_shell("gtk-launch %s" % shlex.quote(name), timeout=10)
        if rc == 0:
            return "launched %s" % name
        return "gtk-launch failed: %s" % out
    return "no executable called %r on PATH" % name


def notify(title: str, body: str = "") -> None:
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", APP_NAME, title, body],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass


def media_control(action: str) -> str:
    action = action.strip().lower()
    if action in ("volume_up", "volume_down", "mute", "volume"):
        if shutil.which("wpctl"):
            arg = {"volume_up": "5%+", "volume_down": "5%-"}.get(action, "")
            if action == "mute":
                rc, out = run_shell("wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle")
            elif arg:
                rc, out = run_shell(
                    "wpctl set-volume @DEFAULT_AUDIO_SINK@ %s" % arg)
            else:
                rc, out = run_shell("wpctl get-volume @DEFAULT_AUDIO_SINK@")
            return out or ("volume %s" % action)
        if shutil.which("pactl"):
            arg = {"volume_up": "+5%", "volume_down": "-5%"}.get(action, "")
            if action == "mute":
                rc, out = run_shell("pactl set-sink-mute @DEFAULT_SINK@ toggle")
            elif arg:
                rc, out = run_shell(
                    "pactl set-sink-volume @DEFAULT_SINK@ %s" % arg)
            else:
                rc, out = run_shell("pactl get-sink-volume @DEFAULT_SINK@")
            return out or ("volume %s" % action)
        return "no wpctl or pactl on this box"
    if not shutil.which("playerctl"):
        return "playerctl is not installed"
    verb = {"play": "play", "pause": "pause", "toggle": "play-pause",
            "next": "next", "previous": "previous", "prev": "previous",
            "stop": "stop", "status": "status",
            "current": "metadata"}.get(action)
    if not verb:
        return "unknown media action %r" % action
    rc, out = run_shell("playerctl %s" % verb, timeout=10)
    return out or ("player: %s" % verb)


def clipboard_read() -> str:
    for cmd in ("wl-paste -n", "xclip -selection clipboard -o", "xsel -b"):
        if shutil.which(cmd.split()[0]):
            rc, out = run_shell(cmd, timeout=10)
            if rc == 0:
                return out
    return "[no clipboard tool: install wl-clipboard or xclip]"


def clipboard_write(text: str) -> str:
    for cmd in ("wl-copy", "xclip -selection clipboard", "xsel -b -i"):
        exe = cmd.split()[0]
        if shutil.which(exe):
            try:
                proc = subprocess.Popen(shlex.split(cmd),
                                        stdin=subprocess.PIPE)
                proc.communicate(text.encode("utf-8"), timeout=10)
                return "copied %d chars to the clipboard" % len(text)
            except Exception as exc:
                return "clipboard write failed: %s" % exc
    return "[no clipboard tool: install wl-clipboard or xclip]"


def take_screenshot() -> Tuple[bool, str]:
    path = os.path.join(tempfile.gettempdir(),
                        "george-shot-%d.png" % int(time.time()))
    candidates = [
        ("grim", "grim %s" % shlex.quote(path)),
        ("spectacle", "spectacle -b -n -o %s" % shlex.quote(path)),
        ("gnome-screenshot", "gnome-screenshot -f %s" % shlex.quote(path)),
        ("scrot", "scrot %s" % shlex.quote(path)),
        ("import", "import -window root %s" % shlex.quote(path)),
    ]
    for exe, cmd in candidates:
        if shutil.which(exe):
            rc, out = run_shell(cmd, timeout=20)
            if rc == 0 and os.path.exists(path):
                return True, path
            return False, out or "screenshot tool failed"
    return False, "no screenshot tool found (grim, spectacle, scrot...)"


def safe_calc(expression: str) -> str:
    """Arithmetic without eval()."""
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
               ast.Pow, ast.USub, ast.UAdd, ast.Tuple, ast.Load)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return "not an expression: %s" % exc
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return "refused: only plain arithmetic is allowed"
        if isinstance(node, ast.Constant) and not isinstance(
                node.value, (int, float)):
            return "refused: numbers only"
    try:
        return str(eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {}))
    except Exception as exc:
        return "arithmetic error: %s" % exc



# =====================================================================
# CROSS-DISTRO LAYER
#
# CachyOS reports ID=cachyos with ID_LIKE=arch, EndeavourOS/Manjaro the
# same shape, so match on ID_LIKE before falling back to sniffing for a
# binary.  Same approach Basilisk uses.
# =====================================================================

_PKG_MGRS = [
    ("pacman", "sudo pacman -Syu --needed %s"),
    ("apt-get", "sudo apt-get install -y %s"),
    ("dnf", "sudo dnf install -y %s"),
    ("zypper", "sudo zypper install -y %s"),
    ("apk", "sudo apk add %s"),
    ("xbps-install", "sudo xbps-install -Sy %s"),
]

_PKG_NAMES = {
    "pacman": {"ollama": "ollama", "gtk": "gtk4 libadwaita python-gobject",
               "espeak": "espeak-ng", "clip": "wl-clipboard"},
    "apt-get": {"ollama": "", "gtk": "python3-gi gir1.2-gtk-4.0 gir1.2-adw-1",
                "espeak": "espeak-ng", "clip": "wl-clipboard"},
    "dnf": {"ollama": "ollama", "gtk": "python3-gobject gtk4 libadwaita",
            "espeak": "espeak-ng", "clip": "wl-clipboard"},
    "zypper": {"ollama": "", "gtk": "python3-gobject gtk4 libadwaita",
               "espeak": "espeak-ng", "clip": "wl-clipboard"},
    "apk": {"ollama": "", "gtk": "py3-gobject3 gtk4.0 libadwaita",
            "espeak": "espeak-ng", "clip": "wl-clipboard"},
    "xbps-install": {"ollama": "", "gtk": "python3-gobject gtk4 libadwaita",
                     "espeak": "espeak-ng", "clip": "wl-clipboard"},
}


def distro_id() -> Tuple[str, str]:
    ident = like = ""
    for line in _read_first("/etc/os-release").splitlines():
        if line.startswith("ID="):
            ident = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("ID_LIKE="):
            like = line.split("=", 1)[1].strip().strip('"')
    return ident, like


def detect_pkg_mgr() -> Optional[str]:
    ident, like = distro_id()
    family = (ident + " " + like).lower()
    for token, mgr in (("arch", "pacman"), ("debian", "apt-get"),
                       ("ubuntu", "apt-get"), ("fedora", "dnf"),
                       ("rhel", "dnf"), ("suse", "zypper"),
                       ("alpine", "apk"), ("void", "xbps-install")):
        if token in family and shutil.which(mgr):
            return mgr
    for mgr, _tmpl in _PKG_MGRS:
        if shutil.which(mgr):
            return mgr
    return None


def install_hint(what: str) -> str:
    mgr = detect_pkg_mgr()
    if not mgr:
        return "install %s with your package manager" % what
    pkgs = _PKG_NAMES.get(mgr, {}).get(what, "")
    if not pkgs:
        if what == "ollama":
            return ("your distro has no ollama package - see "
                    "https://ollama.com/download")
        return "install %s with your package manager" % what
    tmpl = dict(_PKG_MGRS)[mgr]
    return tmpl % pkgs


# =====================================================================
# OLLAMA LIFECYCLE
#
# George starts the daemon with the app and stops it on the way out --
# but ONLY the daemon it started itself.  If ollama was already running,
# or systemd owns it, George leaves it alone: killing a service the
# operator started is not ours to do.
# =====================================================================

class OllamaSupervisor:

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.client = Ollama(cfg)
        self.proc: Optional[subprocess.Popen] = None
        self.owned = False
        self.state = "unknown"

    # ---- helpers ------------------------------------------------------
    @staticmethod
    def systemd_owns() -> bool:
        if not shutil.which("systemctl"):
            return False
        for scope in ("--user", "--system"):
            try:
                r = subprocess.run(["systemctl", scope, "is-active",
                                    "ollama.service"],
                                   capture_output=True, text=True, timeout=5)
            except Exception:
                continue
            if r.stdout.strip() == "active":
                return True
        return False

    @staticmethod
    def binary() -> Optional[str]:
        return shutil.which("ollama")

    def wait_healthy(self, seconds: float = 40.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.client.alive():
                return True
            if self.proc is not None and self.proc.poll() is not None:
                return False            # daemon died on us, stop waiting
            time.sleep(0.4)
        return False

    # ---- lifecycle ----------------------------------------------------
    def ensure_running(self, on_status: Callable[[str], None] = lambda s: None
                       ) -> Tuple[bool, str]:
        if self.client.alive():
            self.state = "external"
            return True, "ollama already running"

        if self.systemd_owns():
            on_status("systemd has ollama, waiting for it")
            if self.wait_healthy(15):
                self.state = "systemd"
                return True, "ollama managed by systemd"

        exe = self.binary()
        if not exe:
            self.state = "missing"
            return False, ("ollama is not installed. %s" %
                           install_hint("ollama"))

        on_status("starting ollama serve")
        logfile = os.path.join(DATA_DIR, "ollama.log")
        _ensure_dirs()
        try:
            handle = open(logfile, "ab")
        except OSError:
            handle = subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                [exe, "serve"], stdout=handle, stderr=handle,
                stdin=subprocess.DEVNULL, start_new_session=True)
        except Exception as exc:
            self.state = "failed"
            return False, "could not start ollama: %s" % exc

        self.owned = True
        if self.wait_healthy(40):
            self.state = "owned"
            log("started ollama serve (pid %d)" % self.proc.pid)
            return True, "ollama started"
        self.state = "failed"
        tail = ""
        try:
            with open(logfile, "r", encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-400:]
        except OSError:
            pass
        self.shutdown()
        return False, ("ollama did not come up in 40s. %s" % tail).strip()

    def shutdown(self) -> None:
        """Stop the daemon -- only if this process started it."""
        proc, self.proc = self.proc, None
        if not (self.owned and proc):
            return
        self.owned = False
        if proc.poll() is not None:
            return
        log("stopping the ollama we started (pid %d)" % proc.pid)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                return
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log("ollama would not die; leaving it")

    def status_line(self) -> str:
        return {"owned": "started by George", "external": "already running",
                "systemd": "systemd", "missing": "not installed",
                "failed": "failed to start"}.get(self.state, "unknown")


# =====================================================================
# MODEL MANAGEMENT  --  pull, delete, browse, all from inside the app
# =====================================================================

CURATED_MODELS = [
    ("deepseek-r1:7b", "4.7 GB", "reasoner, George's default"),
    ("deepseek-r1:8b", "5.2 GB", "same family, a bit sharper"),
    ("deepseek-r1:14b", "9.0 GB", "much better, needs ~12 GB VRAM"),
    ("qwen2.5:7b", "4.7 GB", "fast all-rounder, follows JSON well"),
    ("qwen2.5:14b", "9.0 GB", "stronger tool use"),
    ("llama3.1:8b", "4.9 GB", "solid general chat"),
    ("mistral:7b", "4.1 GB", "light and quick"),
    ("gemma2:9b", "5.4 GB", "good writing"),
    ("phi4:14b", "9.1 GB", "strong reasoning for the size"),
    ("qwen2.5-coder:7b", "4.7 GB", "code"),
    ("llava:7b", "4.7 GB", "vision"),
    ("nomic-embed-text", "274 MB", "embeddings"),
]


class ModelManager:

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.client = Ollama(cfg)

    def installed(self) -> List[Dict[str, Any]]:
        try:
            with urllib.request.urlopen(self.client.base + "/api/tags",
                                        timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            log("model list failed: %s" % exc)
            return []
        out = []
        for m in data.get("models", []):
            size = m.get("size", 0)
            out.append({"name": m.get("name", "?"),
                        "size": "%.1f GB" % (size / 1073741824.0)
                        if size else "?",
                        "family": (m.get("details") or {}).get("family", "")})
        return sorted(out, key=lambda d: d["name"])

    def pull(self, name: str, on_progress: Callable[[str, float], None],
             stop: threading.Event) -> Tuple[bool, str]:
        """Stream /api/pull.  Reports (status, 0..1) as it goes."""
        payload = json.dumps({"model": name, "stream": True}).encode("utf-8")
        req = urllib.request.Request(
            self.client.base + "/api/pull", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except Exception as exc:
            return False, "pull failed to start: %s" % exc
        last = ""
        with resp:
            for raw in resp:
                if stop.is_set():
                    return False, "cancelled"
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if obj.get("error"):
                    return False, str(obj["error"])
                last = str(obj.get("status", ""))
                total = float(obj.get("total") or 0)
                done = float(obj.get("completed") or 0)
                frac = (done / total) if total > 0 else 0.0
                on_progress(last, max(0.0, min(1.0, frac)))
                if last.lower() == "success":
                    return True, "pulled %s" % name
        return (True, "pulled %s" % name) if last else (False, "pull ended early")

    def delete(self, name: str) -> Tuple[bool, str]:
        payload = json.dumps({"model": name}).encode("utf-8")
        req = urllib.request.Request(
            self.client.base + "/api/delete", data=payload,
            headers={"Content-Type": "application/json"}, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True, "removed %s" % name
        except urllib.error.HTTPError as exc:
            return False, "delete failed: %s" % exc
        except Exception as exc:
            return False, "delete failed: %s" % exc
