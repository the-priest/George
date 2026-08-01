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
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple

import george_platform as osx
from george_platform import IS_WINDOWS


APP_ID = "com.thepriest.george"
APP_NAME = "George"
VERSION = "2.4.0"

HOME = os.path.expanduser("~")
CONFIG_DIR = osx.config_dir()
DATA_DIR = osx.data_dir()
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
    "stall_seconds": 90,             # no token for this long = say so
    "tool_timeout": 150,             # a wedged tool cannot wedge the turn
    "auto_model_fallback": True,     # configured model gone -> use one here
    "thinking": "off",               # auto | off | on  (see chat_stream)

    "voice_enabled": True,
    "voice_engine": "auto",          # auto | piper | espeak | none
    "voice_speed": 1.0,
    "voice_pitch": 38,               # espeak only, 0-99
    "piper_model": "",
    "piper_voice_pref": "en_GB",     # which voice to pick when several
    "stt_enabled": True,

    "auto_run_commands": False,      # False = confirm every shell command
    "allow_writes": False,           # file writes outside the notes file
    "sandbox_root": HOME,

    "vision_model": "",              # blank = whatever vision model is pulled
    "vision_timeout": 120,
    "watch_enabled": False,          # ambient mode is OFF until he says so
    "watch_interval": 120,           # seconds between looks
    "watch_min_gap": 240,            # seconds between things he hears
    "watch_max_per_hour": 8,
    "watch_mode": "advice",          # advice | banter | quiet
    "watch_speak": True,
    "sounds": True,

    "persona": "jarvis",             # jarvis | plain | blunt
    "accent": "cyan",                # cyan | amber | violet | green | red
    "ui_density": "comfortable",     # comfortable | compact
    "animations": True,
    "safe_graphics": False,          # cairo renderer; see the runtime hook
    "show_reasoning": True,
    "greet_on_start": True,

    "font_scale": 1.0,
    "transcript_live_rows": 40,      # widgets kept in the view
    "chat_retention_hours": 24,
    "chat_max_sessions": 60,

    "feeds": DEFAULT_FEEDS,
    "news_count": 12,
    "location": "",                  # blank = wttr.in geo-IP guess
    "browser": "",                   # blank = xdg-open
    "user_name": "",
}

# Anything numeric gets clamped to these on load, so a hand-edited or
# half-written config can never hand the UI a value that breaks a widget.
LIMITS: Dict[str, Tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "num_ctx": (1024, 131072),
    "max_steps": (1, 40),
    "request_timeout": (10, 3600),
    "stall_seconds": (10, 600),
    "tool_timeout": (5, 900),
    "voice_speed": (0.5, 2.0),
    "voice_pitch": (0, 99),
    "font_scale": (0.75, 2.0),
    "transcript_live_rows": (10, 400),
    "chat_retention_hours": (0, 8760),
    "chat_max_sessions": (5, 500),
    "news_count": (3, 40),
    "vision_timeout": (15, 900),
    "watch_interval": (20, 3600),
    "watch_min_gap": (0, 7200),
    "watch_max_per_hour": (1, 120),
}

CHOICES: Dict[str, Tuple[str, ...]] = {
    "thinking": ("auto", "off", "on"),
    "voice_engine": ("auto", "piper", "espeak", "sapi", "none"),
    "persona": ("jarvis", "plain", "blunt"),
    "watch_mode": ("advice", "banter", "quiet"),
    "accent": ("cyan", "amber", "violet", "green", "red", "white"),
    "ui_density": ("comfortable", "compact"),
}


def _ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def coerce_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Force every value to the shape of its default.

    A config file is a text file he can edit, and half-written JSON, a
    string where a number belongs, or a stale key from an older build
    all have to end in a usable app rather than a traceback on a widget
    three screens away.  Anything that cannot be repaired is replaced by
    its default and logged.
    """
    out: Dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        value = cfg.get(key, default)
        try:
            if isinstance(default, bool):
                if isinstance(value, str):
                    value = value.strip().lower() in ("1", "true", "yes", "on")
                value = bool(value)
            elif isinstance(default, float):
                value = float(value)
            elif isinstance(default, int):
                value = int(float(value))
            elif isinstance(default, str):
                value = str(value)
            elif isinstance(default, list):
                if not isinstance(value, list) or not value:
                    value = default
                else:
                    rows = [[str(r[0]), str(r[1])] for r in value
                            if isinstance(r, (list, tuple)) and len(r) >= 2
                            and str(r[1]).strip()]
                    value = rows or default
        except (TypeError, ValueError):
            log("config: %s=%r is not usable, using the default" % (key, value))
            value = default

        lo_hi = LIMITS.get(key)
        if lo_hi is not None and isinstance(value, (int, float)):
            lo, hi = lo_hi
            clamped = min(hi, max(lo, value))
            if clamped != value:
                log("config: %s clamped to %s" % (key, clamped))
            value = type(default)(clamped)

        allowed = CHOICES.get(key)
        if allowed and value not in allowed:
            log("config: %s=%r is not one of %s, using %s"
                % (key, value, allowed, default))
            value = default

        out[key] = value

    root = str(out.get("sandbox_root") or "").strip()
    if not root or not os.path.isdir(os.path.expanduser(root)):
        out["sandbox_root"] = HOME
    return out


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
    except OSError:
        pass
    except ValueError as exc:
        # Keep the broken file so he can look at it; do not overwrite it
        # silently and lose whatever he was in the middle of typing.
        log("config is not valid JSON (%s); starting from defaults" % exc)
        try:
            os.replace(CONFIG_PATH, CONFIG_PATH + ".broken")
        except OSError:
            pass

    cfg = coerce_config(cfg)
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
LOG_MAX_BYTES = 2 * 1024 * 1024


def _rotate_log() -> None:
    """Keep one previous log.  Called with the lock held."""
    try:
        if os.path.getsize(LOG_PATH) < LOG_MAX_BYTES:
            return
        os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def log(msg: str) -> None:
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        try:
            _ensure_dirs()
            _rotate_log()
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    # A GUI build on Windows is launched with no console attached, so
    # sys.stderr is None and writing to it raises. Never let logging be
    # the thing that crashes the app.
    if os.environ.get("GEORGE_DEBUG") and sys.stderr is not None:
        try:
            sys.stderr.write(line + "\n")
        except Exception:
            pass


def log_exc(msg: str) -> None:
    """Log a message and the current exception traceback (if any)."""
    try:
        log(msg)
        # traceback.format_exc() is safe even if there's no current exception
        log(traceback.format_exc())
    except Exception:
        # Never let logging cause a crash
        pass


def install_crash_handlers() -> None:
    """Send anything that escapes -- main thread or worker -- to the log
    file.  Without this a background thread can die with nothing on
    screen and nothing on disk, which is the worst way to debug a
    desktop app."""
    import traceback

    def hook(exc_type, exc, tb) -> None:
        log("UNCAUGHT %s: %s\n%s" % (exc_type.__name__, exc,
                                     "".join(traceback.format_tb(tb))[-4000:]))
        if sys.stderr is not None:
            try:
                sys.__excepthook__(exc_type, exc, tb)
            except Exception:
                pass

    sys.excepthook = hook

    if hasattr(threading, "excepthook"):
        def thook(args) -> None:
            log("UNCAUGHT in %s: %s: %s\n%s"
                % (getattr(args.thread, "name", "?"),
                   getattr(args.exc_type, "__name__", "?"), args.exc_value,
                   "".join(traceback.format_tb(args.exc_traceback))[-4000:]))
        threading.excepthook = thook


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

_SHELLS = ({"sh", "bash", "zsh", "dash", "ksh", "fish", "busybox"} |
           osx.WINDOWS_SHELLS)
_WRAPPERS = ({"sudo", "doas", "pkexec", "env", "nice", "ionice", "setsid",
              "nohup", "time", "timeout", "stdbuf", "unbuffer", "command",
              "exec", "xargs", "watch", "script", "chrt", "firejail"} |
             osx.WINDOWS_WRAPPERS)

# Both operating systems' critical paths, always. The tables are merged
# rather than switched on os.name for two reasons: the gate that ships
# is then the gate that gets tested on whichever box runs the suite, and
# for a destructive-command gate over-scanning is the safe direction.
_CRITICAL_DIRS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64",
    "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/usr",
    "/var", "/home", "/efi", "/boot/efi",
} | osx.WINDOWS_CRITICAL_TARGETS

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
) + osx.WINDOWS_HARD_NO

# curl|bash and friends: piping the network straight into a shell.
_PIPE_TO_SHELL = re.compile(
    r"\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?"
    r"(sh|bash|zsh|dash|python3?|perl|ruby|node)\b", re.I)


def _normalise(command: str) -> str:
    s = command.replace("${IFS}", " ").replace("$IFS", " ")
    s = s.replace("\r", "\n")
    return s


def _decaret(command: str) -> str:
    """cmd.exe's escape character is `^`, so `de^l /s /q C:\\` runs del.

    The gate checks the string both ways -- as typed and with the carets
    removed -- because only one of those two readings has to be
    dangerous for the command to be dangerous.
    """
    return command.replace("^", "")


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


def _argv_all(sub: str) -> Optional[List[List[str]]]:
    """Every way this subcommand could be read. None = refuse.

    POSIX and Windows disagree about the backslash, so `del C:\\Windows`
    is two different argvs depending on who is doing the splitting. The
    gate looks at all of them.
    """
    return osx.argv_variants(sub)


def _argv(sub: str) -> Optional[List[str]]:
    variants = osx.argv_variants(sub)
    return variants[0] if variants else None


def _base(arg: str) -> str:
    return osx.base_name(arg)


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
    # $HOME, ${HOME}, ~ and %USERPROFILE% are all the same directory; a
    # gate that only knows one spelling is a gate you get past by typing
    # another. Windows paths are folded to lower case because its file
    # system does not care about it and neither can this.
    return osx.norm_target(t)


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
} | osx.WINDOWS_DANGEROUS_CMDS


def _sub_is_catastrophic(args: List[str], _depth: int = 0) -> bool:
    peeled = _strip_wrappers(args)
    if _check_argv(peeled, _depth):
        return True
    for i, tok in enumerate(args):
        if i == 0:
            continue
        base = _base(tok).lower()
        if base in _DANGEROUS_CMDS or base.startswith("mkfs."):
            if _check_argv(args[i:], _depth):
                return True
    return False


def _check_argv(args: List[str], _depth: int = 0) -> bool:
    if not args:
        return False
    cmd = _base(args[0])
    rest = args[1:]

    if cmd in _SHELLS or cmd.lower() in _SHELLS:
        for i, a in enumerate(rest):
            low = a.lower()
            if low in osx.ENCODED_FLAGS and i + 1 < len(rest):
                # -EncodedCommand is base64 UTF-16LE. Decode it or refuse:
                # an inner command this gate cannot read is not a command
                # this gate may approve.
                inner = osx.decode_powershell(rest[i + 1])
                if inner is None:
                    return True
                return is_destructive_command(inner, _depth + 1)
            if (a in ("-c", "-lc", "-ic") or low in osx.INLINE_FLAGS) and \
                    i + 1 < len(rest):
                return is_destructive_command(" ".join(rest[i + 1:]),
                                              _depth + 1)
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

    # ---- Windows spellings of the same three ideas: delete a tree,
    # ---- overwrite something important, hand ownership away.
    low = cmd.lower()
    if low in ("del", "erase", "rd", "rmdir", "remove-item", "ri"):
        flags = " ".join(a.lower() for a in rest if a.startswith(("-", "/")))
        recursive = ("/s" in flags or "-recurse" in flags or "/q" in flags or
                     low in ("rd", "rmdir", "remove-item", "ri"))
        for t in [a for a in rest if not a.startswith(("-", "/"))]:
            if _is_catastrophic_target(t):
                return True
            norm = _norm_target(t)
            # `del C:\*` or `Remove-Item D:\ -Recurse` -- one level below
            # a drive root with a wildcard is the whole drive
            if recursive and re.match(r"^[a-z]:\\(\*|\*\.\*)?$", norm):
                return True
        return False

    if low in ("move", "copy", "xcopy", "robocopy", "move-item",
               "rename-item", "ren", "rename"):
        for t in [a for a in rest if not a.startswith(("-", "/"))]:
            if _is_catastrophic_target(t):
                return True
        return False

    if low in ("takeown", "icacls", "cacls", "attrib"):
        for t in [a for a in rest if not a.startswith(("-", "/", "+"))]:
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
    # `de^l /s /q C:\` is del. Both readings have to be clean, not one.
    bare = _decaret(s)
    for candidate in ({s, bare} if bare != s else {s}):
        for rx in _HARD_NO:
            if rx.search(candidate):
                return True

    for inner in _lift_substitutions(s):
        if is_destructive_command(inner, _depth + 1):
            return True

    if bare != s and is_destructive_command(bare, _depth + 1):
        return True

    for sub in _split_subcommands(s):
        variants = _argv_all(sub)
        if variants is None:
            return True                   # unparseable -> refuse
        for args in variants:
            if _sub_is_catastrophic(args, _depth):
                return True

    # redirection into a device node
    if re.search(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)", s, re.I):
        return True
    return False


def is_network_pipe_to_shell(command: str) -> bool:
    s = _normalise(command)
    return bool(_PIPE_TO_SHELL.search(s) or
                osx.WINDOWS_PIPE_TO_SHELL.search(s))


# Commands George may run on his own.
#
# This is an allowlist, and it is deliberately conservative about the
# ways a "read-only" tool stops being read-only: a redirect writes a
# file, a substitution runs a second command, and sudo turns anything
# into a root action. Those are checked before the name is even looked
# up. Anything not listed here still runs -- it just asks first.
#
# Things that LOOK harmless but are not auto-run on purpose:
#   xdg-open / notify-send / playerctl / wpctl / pactl -- these act on
#     his session rather than reporting on it, and there are proper
#     tools for each of them with their own confirmation.
#   sudo anything -- privilege escalation is never automatic, and a
#     password prompt on a tty nobody is watching just hangs.

def _flagless(args: List[str]) -> List[str]:
    return [a for a in args[1:] if not a.startswith("-")]


def _ok_systemctl(args: List[str]) -> bool:
    verbs = ("status", "list-units", "list-unit-files", "list-timers",
             "is-active", "is-enabled", "is-failed", "show", "cat",
             "show-environment", "get-default")
    return any(a in verbs for a in args[1:])


def _ok_journalctl(args: List[str]) -> bool:
    return not any(a.startswith("--vacuum") or a in ("--rotate", "--flush",
                                                     "--sync")
                   for a in args[1:])


def _ok_pacman(args: List[str]) -> bool:
    for a in args[1:]:
        if a.startswith("-Q"):
            return True
        if a.startswith("-S") and ("i" in a[2:] or "s" in a[2:]):
            return True   # -Si info, -Ss search
    return False


def _ok_pkg_query(args: List[str]) -> bool:
    """dpkg/rpm/apt/dnf/flatpak/snap etc: query verbs only."""
    reads = ("list", "search", "show", "info", "policy", "depends",
             "rdepends", "-l", "-L", "-qa", "-qi", "-ql", "-q", "-s",
             "--version", "repoquery", "provides", "which", "cache")
    return any(a in reads or a.startswith("-q") or a.startswith("-l")
               for a in args[1:])


def _ok_ip(args: List[str]) -> bool:
    writes = ("set", "add", "del", "delete", "flush", "change", "replace")
    return not any(a in writes for a in args[1:])


def _ok_nmcli(args: List[str]) -> bool:
    writes = ("up", "down", "add", "delete", "modify", "edit", "connect",
              "disconnect", "on", "off")
    return not any(a in writes for a in args[1:])


def _ok_find(args: List[str]) -> bool:
    writes = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
              "-fprintf", "-fls")
    return not any(a in writes for a in args[1:])


def _ok_sed(args: List[str]) -> bool:
    return not any(a == "-i" or a.startswith("--in-place") or
                   a.startswith("-i") and len(a) > 2 for a in args[1:])


def _ok_awk(args: List[str]) -> bool:
    body = " ".join(args[1:])
    return "system(" not in body and ">" not in body and "print >" not in body


def _ok_curl(args: List[str]) -> bool:
    writes = ("-o", "-O", "--output", "--remote-name", "-T", "--upload-file",
              "-d", "--data", "--data-binary", "--data-raw", "-F", "--form")
    for a in args[1:]:
        if a in writes or a.startswith("--output"):
            return False
        if a in ("-X", "--request"):
            return False
    return True


def _ok_ping(args: List[str]) -> bool:
    if any(a in ("-f", "--flood") for a in args[1:]):
        return False
    return any(a in ("-c", "-w", "-W") for a in args[1:])   # bounded only


def _ok_mount(args: List[str]) -> bool:
    return not _flagless(args)          # bare `mount` lists; args mount


def _ok_git(args: List[str]) -> bool:
    reads = ("status", "log", "diff", "show", "branch", "remote", "config",
             "describe", "blame", "shortlog", "rev-parse", "ls-files",
             "ls-remote", "tag", "stash")
    subs = _flagless(args)
    if not subs:
        return False
    if subs[0] == "stash" and len(subs) > 1 and subs[1] != "list":
        return False
    if subs[0] == "config" and any(not a.startswith("-") for a in subs[1:]) \
            and "--get" not in args:
        return False                    # `git config x y` writes
    if subs[0] == "tag" and len(subs) > 1:
        return False
    if subs[0] == "branch" and len(subs) > 1:
        return False
    return subs[0] in reads


# name -> extra check, or None when the command is safe on its own
READ_ONLY: Dict[str, Optional[Callable[[List[str]], bool]]] = {
    # files and text
    "ls": None, "cat": None, "head": None, "tail": None, "wc": None,
    "grep": None, "egrep": None, "fgrep": None, "rg": None, "ag": None,
    "stat": None, "file": None, "tree": None, "realpath": None,
    "readlink": None, "basename": None, "dirname": None, "dircolors": None,
    "nl": None, "tac": None, "cut": None, "sort": None, "uniq": None,
    "column": None, "paste": None, "comm": None, "diff": None, "cmp": None,
    "strings": None, "md5sum": None, "sha1sum": None, "sha256sum": None,
    "base64": None, "od": None, "xxd": None, "jq": None, "yq": None,
    "find": _ok_find, "locate": None, "sed": _ok_sed, "awk": _ok_awk,
    "gawk": _ok_awk, "mawk": _ok_awk,
    # system facts
    "uname": None, "hostname": None, "hostnamectl": None, "arch": None,
    "whoami": None, "id": None, "groups": None, "who": None, "w": None,
    "users": None, "last": None, "logname": None, "getent": None,
    "date": None, "cal": None, "uptime": None, "timedatectl": None,
    "locale": None, "printenv": None, "env": None, "pwd": None,
    "echo": None, "printf": None, "seq": None, "test": None, "true": None,
    "nproc": None, "lscpu": None, "lsblk": None, "lsusb": None,
    "lspci": None, "lsmod": None, "lsof": None, "lshw": None,
    "dmidecode": None, "inxi": None, "hwinfo": None, "sensors": None,
    "acpi": None, "upower": None, "free": None, "vmstat": None,
    "iostat": None, "mpstat": None, "df": None, "du": None,
    "findmnt": None, "blkid": None, "smartctl": None, "mount": _ok_mount,
    "lsb_release": None, "neofetch": None, "fastfetch": None,
    "screenfetch": None, "nvidia-smi": None, "rocm-smi": None,
    "glxinfo": None, "vulkaninfo": None, "xrandr": None, "xdpyinfo": None,
    "wmctrl": None, "loginctl": None, "ulimit": None,
    # processes and services
    "ps": None, "pstree": None, "pgrep": None, "pidof": None, "top": None,
    "systemctl": _ok_systemctl, "journalctl": _ok_journalctl,
    "dmesg": None, "rc-status": None,
    # network, read side only
    "ip": _ok_ip, "ss": None, "netstat": None, "ifconfig": None,
    "iwgetid": None, "iwconfig": None, "rfkill": None, "route": None,
    "arp": None, "dig": None, "host": None, "nslookup": None,
    "nmcli": _ok_nmcli, "ping": _ok_ping, "curl": _ok_curl,
    # wget is not here on purpose: writing a file is its default
    # behaviour, not an opt-in flag. open_page reads the web instead.
    # toolchain versions and package queries
    "which": None, "whereis": None, "type": None, "command": None,
    "ldd": None, "pkg-config": None, "python": None, "python3": None,
    "pip": _ok_pkg_query, "pip3": _ok_pkg_query, "node": None, "npm":
        _ok_pkg_query, "git": _ok_git, "ollama": _ok_pkg_query,
    "pacman": _ok_pacman, "dpkg": _ok_pkg_query, "dpkg-query":
        _ok_pkg_query, "rpm": _ok_pkg_query, "apt": _ok_pkg_query,
    "apt-cache": None, "dnf": _ok_pkg_query, "zypper": _ok_pkg_query,
    "apk": _ok_pkg_query, "xbps-query": None, "flatpak": _ok_pkg_query,
    "snap": _ok_pkg_query, "pamac": _ok_pkg_query,
}

# The Windows half of the same list: dir, systeminfo, tasklist, ipconfig,
# the read-only PowerShell cmdlets and the query-only forms of netsh, sc,
# reg and winget. Merged in rather than swapped, so one allowlist ships
# and one allowlist gets tested.
READ_ONLY.update(osx.WINDOWS_READ_ONLY)

# If any of these appear anywhere in the command it is not auto-run,
# whatever the command name says. A redirect writes; a substitution runs
# something this gate never saw; `&` detaches it from the timeout.
_NO_AUTORUN_CHARS = ((">", "<", "$(", "`", "${", "&") +
                     osx.WINDOWS_NO_AUTORUN_CHARS)

_PRIV = ("sudo", "doas", "pkexec", "su", "runuser", "setpriv",
         "runas", "gsudo", "elevate")


_MISSING = object()


def command_needs_confirmation(command: str, cfg: Dict[str, Any]) -> bool:
    """False = George may run it himself.

    Read-only inspection runs free so he can actually answer questions
    about the machine without a click for every `uname -a`. Everything
    else asks, unless the operator has flipped auto-run on.
    """
    if cfg.get("auto_run_commands"):
        return False
    s = _normalise(command)
    if any(ch in s for ch in _NO_AUTORUN_CHARS):
        return True
    subs = _split_subcommands(s)
    if not subs:
        return True
    for sub in subs:
        variants = _argv_all(sub)
        if not variants:
            return True
        # Every reading has to be read-only, not just the convenient one.
        for args in variants:
            if not args:
                return True
            if any(_base(a).lower() in _PRIV for a in args):
                return True             # never escalate on its own
            args = _strip_wrappers(args)
            if not args:
                return True
            base = _base(args[0])
            check = READ_ONLY.get(base, _MISSING)
            if check is _MISSING:
                check = READ_ONLY.get(base.lower(), _MISSING)
            if check is _MISSING:
                return True
            if check is not None and not check(args):
                return True
    return False


def inside_sandbox(path: str, cfg: Dict[str, Any]) -> bool:
    root = os.path.realpath(os.path.expanduser(
        cfg.get("sandbox_root") or HOME))
    try:
        target = os.path.realpath(os.path.expanduser(path))
    except OSError:
        return False
    # NTFS does not care about case, so a check that does is a check you
    # walk straight past by typing C:\USERS instead of C:\Users.
    root, target = os.path.normcase(root), os.path.normcase(target)
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
        except Exception as exc:
            log_exc("ollama alive check failed: %s" % exc)
            return False

    def models(self) -> List[str]:
        try:
            with urllib.request.urlopen(self.base + "/api/tags", timeout=8) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return sorted(m.get("name", "") for m in data.get("models", []))
        except Exception as exc:
            log("model list failed: %s" % exc)
            return []

    def version(self) -> str:
        try:
            with urllib.request.urlopen(self.base + "/api/version",
                                        timeout=5) as r:
                return str(json.loads(r.read().decode("utf-8", "replace")
                                      ).get("version", "?"))
        except Exception as exc:
            log_exc("ollama version failed: %s" % exc)
            return "?"

    def resolve_model(self) -> Tuple[str, str]:
        """-> (model_to_use, note).

        If the configured tag is not pulled, fall back to something that
        is rather than failing the turn.  A model he did not choose is
        worth saying out loud, hence the note.
        """
        want = str(self.cfg.get("model", DEFAULTS["model"]))
        if not self.cfg.get("auto_model_fallback", True):
            return want, ""
        names = self.models()
        if not names or want in names:
            return want, ""
        # exact tag missing: same family with any tag is the closest thing
        stem = want.split(":")[0]
        same = [n for n in names if n.split(":")[0] == stem]
        pick = (same or names)[0]
        return pick, ("%s is not pulled - using %s instead" % (want, pick))

    def chat_stream(self, messages: List[Dict[str, str]],
                    on_token: Callable[[str], None],
                    stop: threading.Event,
                    on_stall: Optional[Callable[[float], None]] = None,
                    model: str = "") -> str:
        """Stream a reply.  Returns the full text.  Honours the stop event
        between chunks so the UI stop button is never a lie."""
        payload = {
            "model": model or str(self.cfg.get("model", DEFAULTS["model"])),
            "messages": messages,
            "stream": True,
            "keep_alive": self.cfg.get("keep_alive", "30m"),
            "options": {
                "temperature": float(self.cfg.get("temperature", 0.6)),
                "num_ctx": int(self.cfg.get("num_ctx", 8192)),
            },
        }
        # Reasoning models auto-think on every request, and George's loop
        # can take fourteen turns -- so the thinking happens fourteen
        # times before he says a word. Switching it off is the single
        # biggest speed win available without changing model.
        think = str(self.cfg.get("thinking", "off")).lower()
        if think == "off":
            payload["think"] = False
        elif think == "on":
            payload["think"] = True
        timeout = int(self.cfg.get("request_timeout", 300))
        stall_after = float(self.cfg.get("stall_seconds", 90))
        chunks: List[str] = []
        try:
            resp = self._post("/api/chat", payload, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and "think" in payload:
                # an ollama too old to know the field: retry without it
                log("this ollama rejects `think`; retrying without it")
                payload.pop("think", None)
                try:
                    resp = self._post("/api/chat", payload, timeout)
                except Exception as exc2:
                    raise OllamaError("ollama request failed: %s" % exc2) from exc2
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:400]
                body = str(json.loads(body).get("error", body))
            except Exception:
                log_exc("failed to parse ollama error body")
            if exc.code == 404:
                raise OllamaError(
                    "ollama does not have %s. pull it from the Models "
                    "window, or run: ollama pull %s"
                    % (payload["model"], payload["model"])) from exc
            raise OllamaError("ollama said %s: %s"
                              % (exc.code, body or exc.reason)) from exc
        except urllib.error.URLError as exc:
            raise OllamaError(
                "cannot reach ollama at %s (%s). is `ollama serve` running?"
                % (self.base, exc.reason)) from exc
        except Exception as exc:
            log_exc("ollama request failed: %s" % exc)
            raise OllamaError("ollama request failed: %s" % exc) from exc

        last_token = time.time()
        warned = False
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
                    last_token = time.time()
                    warned = False
                    on_token(piece)
                elif on_stall is not None and not warned:
                    waited = time.time() - last_token
                    if waited > stall_after:
                        warned = True
                        on_stall(waited)
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


def http_get(url: str, timeout: int = 20, limit: int = 400000,
             attempts: int = 2) -> str:
    """One GET, retried once on a transient failure.

    Feeds and search endpoints drop connections often enough that a
    single blip should not become "no headlines" on his screen.  4xx is
    not retried -- that answer will not change.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    last: Optional[Exception] = None
    data = b""
    for attempt in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read(limit)
            break
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise
            last = exc
        except Exception as exc:
            last = exc
        if attempt + 1 < max(1, attempts):
            time.sleep(0.8 * (attempt + 1))
    else:
        raise last if last else RuntimeError("request failed")
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
    """Run a shell command safely.

    If the command string contains no obvious shell metacharacters, run
    it without invoking the shell (safer).  Otherwise fall back to
    shell=True to preserve behaviour for complex one-liners.
    """
    try:
        if isinstance(command, str):
            # Characters that usually require a shell to interpret.
            shell_meta = set('|&;<>$`*?(){}[]')
            if not any(ch in command for ch in shell_meta):
                args = shlex.split(command)
                proc = subprocess.run(args, shell=False, capture_output=True,
                                      timeout=timeout, cwd=cwd,
                                      **osx.run_kwargs())
            else:
                proc = subprocess.run(command, shell=True, capture_output=True,
                                      timeout=timeout, cwd=cwd,
                                      **osx.run_kwargs())
        else:
            proc = subprocess.run(command, shell=False, capture_output=True,
                                  timeout=timeout, cwd=cwd, **osx.run_kwargs())
    except subprocess.TimeoutExpired:
        return 124, "[timed out after %ds]" % timeout
    except Exception as exc:                       # pragma: no cover
        log_exc("run_shell failed to launch: %s" % exc)
        return 1, "[failed to launch: %s]" % exc
    out = osx.decode_output(proc.stdout or b"") + \
        osx.decode_output(proc.stderr or b"")
    return proc.returncode, out.strip()


def _read_first(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


class CpuMeter:
    """Busy percentage from /proc/stat deltas.

    The first call has no previous sample to diff against, so it reports
    0 rather than a made-up number.
    """

    def __init__(self) -> None:
        self._prev: Optional[Tuple[int, int]] = None
        self._lock = threading.Lock()

    def _sample_windows(self) -> float:
        """GetSystemTimes deltas. The trap in that API is that kernel
        time INCLUDES idle time, so busy is total minus idle, not
        kernel plus user minus idle."""
        now = osx.win_cpu_times()
        if now is None:
            return 0.0
        idle, total = now
        prev = getattr(self, "_win_prev", None)
        self._win_prev = now
        if prev is None:
            return 0.0
        d_idle, d_total = idle - prev[0], total - prev[1]
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))

    def sample(self) -> float:
        if IS_WINDOWS:
            return self._sample_windows()
        line = _read_first("/proc/stat").split("\n")[0]
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return 0.0
        try:
            nums = [int(x) for x in parts[1:11]]
        except ValueError:
            return 0.0
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        with self._lock:
            prev = self._prev
            self._prev = (idle, total)
        if prev is None:
            return 0.0
        d_idle = idle - prev[0]
        d_total = total - prev[1]
        if d_total <= 0:
            return 0.0
        return min(100.0, max(0.0, 100.0 * (1.0 - d_idle / float(d_total))))


_cpu_meter = CpuMeter()


def machine_summary() -> str:
    """One dense line about the box, for the system prompt.

    He was answering questions about "a Linux machine" in the abstract
    because the prompt only carried the distro name. Package manager and
    session type in particular change what the right answer actually is.
    """
    st = system_status()
    bits = ["%s on %s" % (st.get("distro", "this machine"),
                          st.get("arch", "?"))]
    if st.get("kernel"):
        bits.append(("build %s" if IS_WINDOWS else "kernel %s")
                    % st["kernel"])
    if st.get("desktop"):
        bits.append("%s%s" % (st["desktop"],
                              "/" + st["session"] if st.get("session")
                              else ""))
    if st.get("cpu_model"):
        bits.append(st["cpu_model"])
    if st.get("cores"):
        bits.append("%s cores" % st["cores"])
    if st.get("memory"):
        bits.append("%s ram" % st["memory"].split("(")[0].strip())
    if st.get("gpu"):
        bits.append("gpu " + st["gpu"])
    pkg = detect_pkg_mgr()
    if pkg:
        bits.append("package manager: %s" % pkg)
    if st.get("shell"):
        bits.append("shell %s" % st["shell"])
    if st.get("battery"):
        bits.append("battery %s" % st["battery"])
    return ", ".join(bits)


def system_status() -> Dict[str, str]:
    if IS_WINDOWS:
        return _system_status_windows()
    return _system_status_posix()


def _system_status_windows() -> Dict[str, str]:
    """Same keys as the POSIX one, from ctypes and the registry.

    Deliberately no subprocess in the common path: this runs on the HUD
    timer, and spawning cmd every couple of seconds in a windowed app is
    both slow and visible.
    """
    st: Dict[str, str] = {}
    st["host"] = platform.node() or "?"
    st["distro"] = osx.win_os_name()
    st["kernel"] = platform.version()
    st["arch"] = platform.machine()
    desktop, session = osx.win_session()
    st["desktop"] = desktop
    st["session"] = session
    st["shell"] = os.path.basename(os.environ.get("COMSPEC", "cmd.exe"))

    secs = int(osx.win_uptime())
    st["uptime"] = "%dd %dh %dm" % (secs // 86400, (secs % 86400) // 3600,
                                    (secs % 3600) // 60)

    model = osx.win_cpu_model()
    if model:
        st["cpu_model"] = model
    gpu = osx.win_gpu()
    if gpu:
        st["gpu"] = gpu[:70]
    cpu = _cpu_meter.sample()
    st["cpu"] = "%d%%" % int(round(cpu))
    st["cpu_pct"] = str(int(round(cpu)))
    st["cores"] = str(os.cpu_count() or 1)
    # Windows has no load average. Saying so beats inventing one.
    st["load"] = "n/a"

    mem = osx.win_memory()
    total, avail = mem.get("total", 0), mem.get("avail", 0)
    if total:
        used = total - avail
        st["memory"] = "%.1f / %.1f GiB (%d%%)" % (
            used / 1073741824.0, total / 1073741824.0,
            round(100.0 * used / total))
        st["mem_pct"] = str(round(100.0 * used / total))
    sw_total, sw_avail = mem.get("swap_total", 0), mem.get("swap_avail", 0)
    if sw_total and sw_total > total:
        # Windows reports commit limit, not a swap file size; the part
        # above physical RAM is the closest honest equivalent.
        st["swap"] = "%.1f / %.1f GiB" % (
            (sw_total - sw_avail) / 1073741824.0,
            (sw_total - total) / 1073741824.0)
    try:
        du = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        st["disk"] = "%.0f / %.0f GiB free on %s" % (
            du.free / 1073741824.0, du.total / 1073741824.0,
            os.environ.get("SystemDrive", "C:"))
        st["disk_pct"] = str(round(100.0 * (du.total - du.free) / du.total))
    except OSError:
        pass
    battery = osx.win_battery()
    if battery:
        st["battery"] = battery
    return st


def _system_status_posix() -> Dict[str, str]:
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

    st["kernel"] = uname.release
    st["arch"] = uname.machine
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP")
               or os.environ.get("DESKTOP_SESSION") or "")
    if desktop:
        st["desktop"] = desktop
    session = os.environ.get("XDG_SESSION_TYPE", "")
    if session:
        st["session"] = session          # x11 or wayland changes advice
    shell = os.environ.get("SHELL", "")
    if shell:
        st["shell"] = os.path.basename(shell)
    cpu_model = ""
    for line in _read_first("/proc/cpuinfo", "").split("\n"):
        if line.lower().startswith("model name"):
            cpu_model = line.split(":", 1)[-1].strip()
            break
    if cpu_model:
        st["cpu_model"] = cpu_model
    gpu = ""
    if shutil.which("lspci"):
        rc, out = run_shell("lspci -mm 2>/dev/null | grep -iE 'vga|3d|display'",
                            timeout=6)
        if rc == 0 and out.strip():
            first = out.strip().split("\n")[0]
            parts = [p.strip('"') for p in first.split('" "')]
            gpu = " ".join(parts[2:4]) if len(parts) > 3 else first[:60]
    if gpu:
        st["gpu"] = gpu.strip()[:70]
    st["load"] = " ".join(_read_first("/proc/loadavg", "").split()[:3])
    cpu = _cpu_meter.sample()
    st["cpu"] = "%d%%" % int(round(cpu))
    st["cpu_pct"] = str(int(round(cpu)))
    try:
        st["cores"] = str(os.cpu_count() or 1)
    except Exception:
        pass

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
    sw_total = mem.get("SwapTotal", 0)
    sw_free = mem.get("SwapFree", 0)
    if sw_total:
        st["swap"] = "%.1f / %.1f GiB" % ((sw_total - sw_free) / 1048576.0,
                                          sw_total / 1048576.0)
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
    if not browser and IS_WINDOWS:
        result = osx.win_open(url)
        return ("opened %s on screen" % url) if result.startswith("opened") \
            else result
    cmd = [browser, url] if browser else ["xdg-open", url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         **osx.spawn_kwargs(detach=True))
        return "opened %s on screen" % url
    except Exception as exc:
        return "could not open browser: %s" % exc


def launch_app(name: str) -> str:
    name = name.strip()
    if not name:
        return "no application given"
    if IS_WINDOWS:
        return osx.win_launch(name)
    exe = shutil.which(name)
    if exe:
        try:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             **osx.spawn_kwargs(detach=True))
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
    if IS_WINDOWS:
        osx.win_notify(title, body)
        return
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", APP_NAME, title, body],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass


def media_control(action: str) -> str:
    action = action.strip().lower()
    if IS_WINDOWS:
        # The same WM_APPCOMMAND messages a keyboard's media keys send,
        # so it works with whatever mixer and player the box has rather
        # than needing a particular one installed.
        if action in ("volume_up", "volume_down", "mute"):
            steps = 1 if action == "mute" else 3
            ok = osx.win_appcommand(action, steps)
            return ("volume %s" % action.replace("_", " ")) if ok else \
                "could not reach the volume control"
        if action == "volume":
            return "Windows does not expose the master level without extra tooling"
        if osx.win_appcommand(action):
            return "player: %s" % action
        return "unknown media action %r" % action
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
    if IS_WINDOWS:
        return osx.win_clipboard_get()
    for cmd in ("wl-paste -n", "xclip -selection clipboard -o", "xsel -b"):
        if shutil.which(cmd.split()[0]):
            rc, out = run_shell(cmd, timeout=10)
            if rc == 0:
                return out
    return "[no clipboard tool: install wl-clipboard or xclip]"


def clipboard_write(text: str) -> str:
    if IS_WINDOWS:
        return osx.win_clipboard_set(text)
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
    if IS_WINDOWS:
        return osx.win_screenshot(path)
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
# NEW IN 2.0  --  writes, search, processes, network, volume, power
#
# Everything here that can change the machine is written so the refusal
# is structural: the sandbox check happens before the open(), and the
# destructive power actions have no code path that skips confirmation.
# =====================================================================

def write_text_file(path: str, text: str, cfg: Dict[str, Any],
                    append: bool = False) -> str:
    """Write inside the sandbox root only, atomically, with a backup of
    whatever was there before."""
    path = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not path:
        return "no path given"
    if not inside_sandbox(path, cfg):
        return "REFUSED: %s is outside the sandbox root" % path
    if os.path.isdir(path):
        return "REFUSED: %s is a directory" % path
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        return "cannot create %s: %s" % (parent, exc)
    try:
        if append:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text)
            return "appended %d chars to %s" % (len(text), path)
        if os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except OSError:
                pass
        tmp = path + ".george.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        return "write failed: %s" % exc
    return "wrote %d chars to %s%s" % (
        len(text), path, " (previous kept as .bak)"
        if os.path.exists(path + ".bak") else "")


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache", ".venv",
              "venv", ".mozilla", ".thunderbird", "snap"}


def find_files(root: str, pattern: str, cfg: Dict[str, Any],
               limit: int = 60, max_dirs: int = 4000) -> Tuple[bool, str]:
    """Walk for a glob-ish name match, bounded in both directions so a
    search of / cannot hang the turn."""
    import fnmatch
    root = os.path.abspath(os.path.expanduser(str(root or HOME).strip()))
    pattern = str(pattern or "").strip()
    if not pattern:
        return False, "no pattern given"
    if not inside_sandbox(root, cfg):
        return False, "REFUSED: %s is outside the sandbox root" % root
    if not os.path.isdir(root):
        return False, "not a directory: %s" % root
    if "*" not in pattern and "?" not in pattern:
        pattern = "*%s*" % pattern
    hits: List[str] = []
    scanned = 0
    started = time.time()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in _SKIP_DIRS and not d.startswith(".git")]
        scanned += 1
        if scanned > max_dirs or time.time() - started > 20:
            hits.append("[search stopped early - narrow it down]")
            break
        for name in files:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                full = os.path.join(base, name)
                try:
                    hits.append("%s  %d bytes" % (full, os.path.getsize(full)))
                except OSError:
                    hits.append(full)
                if len(hits) >= limit:
                    return True, "\n".join(hits)
    if not hits:
        return True, "nothing under %s matched %s" % (root, pattern)
    return True, "\n".join(hits)


def list_processes(sort_by: str = "cpu", limit: int = 12) -> str:
    """Top processes via ps, with a /proc fallback if ps is missing."""
    if IS_WINDOWS:
        return osx.win_processes(sort_by, limit)
    key = "-%cpu" if str(sort_by).lower().startswith("cpu") else "-%mem"
    if shutil.which("ps"):
        rc, out = run_shell(
            "ps -eo pid,comm,%%cpu,%%mem --sort=%s --no-headers | head -n %d"
            % (key, max(1, min(int(limit), 40))), timeout=10)
        if rc == 0 and out.strip():
            rows = ["pid    process            cpu%   mem%"]
            for line in out.strip().split("\n"):
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    rows.append("%-6s %-18s %-6s %s" % (parts[0], parts[1][:18],
                                                        parts[2], parts[3]))
            return "\n".join(rows)
    names: List[str] = []
    try:
        for pid in sorted(os.listdir("/proc")):
            if pid.isdigit():
                comm = _read_first("/proc/%s/comm" % pid)
                if comm:
                    names.append("%s %s" % (pid, comm))
            if len(names) >= limit:
                break
    except OSError as exc:
        return "cannot read processes: %s" % exc
    return "\n".join(names) or "no processes readable"


def network_status() -> Dict[str, str]:
    if IS_WINDOWS:
        return osx.win_network()
    """Interfaces, addresses and default route, read from the tools that
    exist rather than assuming any one of them does."""
    out: Dict[str, str] = {}
    ifaces: List[str] = []
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            state = _read_first("/sys/class/net/%s/operstate" % name, "?")
            ifaces.append("%s (%s)" % (name, state))
    except OSError:
        pass
    if ifaces:
        out["interfaces"] = ", ".join(ifaces)
    if shutil.which("ip"):
        rc, txt = run_shell("ip -4 -o addr show scope global", timeout=6)
        if rc == 0 and txt.strip():
            addrs = []
            for line in txt.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 4:
                    addrs.append("%s %s" % (parts[1], parts[3]))
            if addrs:
                out["addresses"] = ", ".join(addrs)
        rc, txt = run_shell("ip route show default", timeout=6)
        if rc == 0 and txt.strip():
            out["gateway"] = txt.strip().split("\n")[0]
    if shutil.which("nmcli"):
        rc, txt = run_shell("nmcli -t -f NAME,TYPE,DEVICE connection show "
                            "--active", timeout=8)
        if rc == 0 and txt.strip():
            out["connections"] = "; ".join(txt.strip().split("\n")[:4])
    ssid = ""
    if shutil.which("iwgetid"):
        rc, txt = run_shell("iwgetid -r", timeout=5)
        if rc == 0 and txt.strip():
            ssid = txt.strip()
    if ssid:
        out["wifi"] = ssid
    return out or {"error": "no network information available"}


def volume_control(action: str, level: int = 5) -> str:
    if IS_WINDOWS:
        act = action.strip().lower()
        if act in ("up", "raise", "louder"):
            key, steps = "volume_up", max(1, int(round(level / 2.0)))
        elif act in ("down", "lower", "quieter"):
            key, steps = "volume_down", max(1, int(round(level / 2.0)))
        elif act in ("mute", "unmute", "toggle"):
            key, steps = "mute", 1
        elif act in ("get", "status", "level"):
            # Reading the master level needs the COM audio endpoint API,
            # which is a lot of hand-written vtable calls for one number.
            # Saying so beats returning a made-up figure.
            return ("Windows does not report the master volume without "
                    "extra tooling - I can still turn it up, down or mute")
        else:
            return "unknown volume action %r" % action
        if osx.win_appcommand(key, steps):
            return "volume %s" % key.replace("volume_", "").replace("_", " ")
        return "could not reach the volume control"
    """pipewire, then pulse, then alsa.  Returns what actually happened."""
    action = str(action or "").strip().lower()
    level = max(1, min(int(level or 5), 50))
    if shutil.which("wpctl"):
        sink = "@DEFAULT_AUDIO_SINK@"
        cmds = {
            "up": "wpctl set-volume %s %d%%+" % (sink, level),
            "down": "wpctl set-volume %s %d%%-" % (sink, level),
            "mute": "wpctl set-mute %s toggle" % sink,
            "get": "wpctl get-volume %s" % sink,
        }
    elif shutil.which("pactl"):
        sink = "@DEFAULT_SINK@"
        cmds = {
            "up": "pactl set-sink-volume %s +%d%%" % (sink, level),
            "down": "pactl set-sink-volume %s -%d%%" % (sink, level),
            "mute": "pactl set-sink-mute %s toggle" % sink,
            "get": "pactl get-sink-volume %s" % sink,
        }
    elif shutil.which("amixer"):
        cmds = {
            "up": "amixer -q sset Master %d%%+" % level,
            "down": "amixer -q sset Master %d%%-" % level,
            "mute": "amixer -q sset Master toggle",
            "get": "amixer sget Master",
        }
    else:
        return "no volume control found (wpctl, pactl or amixer)"
    if action in ("set",):
        return "use up, down, mute or get"
    cmd = cmds.get(action)
    if not cmd:
        return "volume actions: up, down, mute, get"
    rc, out = run_shell(cmd, timeout=8)
    if rc != 0:
        return "volume %s failed: %s" % (action, out[:200])
    return out.strip() or "volume %s" % action


POWER_ACTIONS = {
    "lock": ["loginctl lock-session", "xdg-screensaver lock",
             "swaylock", "i3lock"],
    "suspend": ["systemctl suspend", "loginctl suspend"],
    "hibernate": ["systemctl hibernate"],
    "logout": ["loginctl terminate-session self",
               "qdbus org.kde.ksmserver /KSMServer logout 0 0 0"],
    "reboot": ["systemctl reboot"],
    "shutdown": ["systemctl poweroff"],
}


def power_action(action: str) -> str:
    if IS_WINDOWS:
        return osx.win_power(action)
    """Session control.  The caller is responsible for confirming --
    every one of these is disruptive, so nothing here is automatic."""
    action = str(action or "").strip().lower()
    candidates = POWER_ACTIONS.get(action)
    if not candidates:
        return "power actions: %s" % ", ".join(sorted(POWER_ACTIONS))
    for cmd in candidates:
        exe = cmd.split()[0]
        if not shutil.which(exe):
            continue
        rc, out = run_shell(cmd, timeout=12)
        if rc == 0:
            return "%s: done" % action
        return "%s failed: %s" % (action, (out or "exit %d" % rc)[:200])
    return "nothing on this box can %s (tried %s)" % (
        action, ", ".join(c.split()[0] for c in candidates))


def open_path(path: str, cfg: Dict[str, Any]) -> str:
    """xdg-open a local file or folder, sandbox-checked first."""
    path = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not os.path.exists(path):
        return "no such path: %s" % path
    if not inside_sandbox(path, cfg):
        return "REFUSED: %s is outside the sandbox root" % path
    try:
        if IS_WINDOWS:
            return osx.win_open(path)
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        return "could not open %s: %s" % (path, exc)
    return "opened %s on his screen" % path


def disk_report() -> str:
    if IS_WINDOWS:
        lines = ["DRIVE        USED       FREE      TOTAL   USE%"]
        for drive in osx.win_drives():
            try:
                du = shutil.disk_usage(drive)
            except OSError:
                continue
            gb = 1073741824.0
            pct = round(100.0 * (du.total - du.free) / du.total) if du.total \
                else 0
            lines.append("%-8s %7.0f GB %7.0f GB %7.0f GB %4d%%" % (
                drive, (du.total - du.free) / gb, du.free / gb,
                du.total / gb, pct))
        return "\n".join(lines)
    if shutil.which("df"):
        rc, out = run_shell("df -h -x tmpfs -x devtmpfs -x squashfs", timeout=8)
        if rc == 0 and out.strip():
            return out
    try:
        du = shutil.disk_usage(HOME)
        return "home: %.0f GiB free of %.0f GiB" % (du.free / 1073741824.0,
                                                    du.total / 1073741824.0)
    except OSError as exc:
        return "disk read failed: %s" % exc


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
    if IS_WINDOWS:
        return "windows", "windows"
    ident = like = ""
    for line in _read_first("/etc/os-release").splitlines():
        if line.startswith("ID="):
            ident = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("ID_LIKE="):
            like = line.split("=", 1)[1].strip().strip('"')
    return ident, like


def detect_pkg_mgr() -> Optional[str]:
    if IS_WINDOWS:
        return osx.win_pkg_mgr()
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


_WIN_PKG_NAMES = {
    "winget": {"ollama": "winget install --id Ollama.Ollama -e",
               "espeak": "winget install --id eSpeak-NG.eSpeak-NG -e",
               "ffmpeg": "winget install --id Gyan.FFmpeg -e"},
    "choco": {"ollama": "choco install ollama -y",
              "espeak": "choco install espeak -y",
              "ffmpeg": "choco install ffmpeg -y"},
    "scoop": {"ollama": "scoop install ollama",
              "espeak": "scoop install espeak-ng",
              "ffmpeg": "scoop install ffmpeg"},
}


def install_hint(what: str) -> str:
    if IS_WINDOWS:
        mgr = osx.win_pkg_mgr()
        hint = _WIN_PKG_NAMES.get(mgr or "", {}).get(what, "")
        if hint:
            return hint
        if what == "ollama":
            return "download it from https://ollama.com/download/windows"
        return "install %s and put it on your PATH" % what
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
        """True when something other than George is responsible for the
        daemon -- systemd on Linux, the Ollama tray app or its service on
        Windows. Either way George uses it and does not kill it."""
        if IS_WINDOWS:
            return OllamaSupervisor._windows_service_owns()
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
    def _windows_service_owns() -> bool:
        exe = osx.find_binary("tasklist")
        if not exe:
            return False
        try:
            proc = subprocess.run([exe, "/FO", "CSV", "/NH"],
                                  capture_output=True, timeout=15,
                                  **osx.run_kwargs())
        except Exception:
            return False
        listing = osx.decode_output(proc.stdout).lower()
        # "ollama app.exe" is the tray application, which serves on 11434
        # itself. Ours is a bare "ollama.exe serve" we started.
        return "ollama app.exe" in listing

    @staticmethod
    def binary() -> Optional[str]:
        # Ollama's Windows installer drops the exe in LOCALAPPDATA and
        # does not always refresh the PATH a running GUI process
        # inherited, so which() alone finds nothing on a fresh install.
        return osx.find_binary("ollama")

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
                stdin=subprocess.DEVNULL, **osx.spawn_kwargs(detach=True))
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
        # The wrapper is not the daemon: `ollama serve` forks a runner
        # that holds the model and the port. Kill the whole tree or the
        # next launch finds 11434 already taken.
        if not osx.kill_tree(proc, force=False):
            return
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass
        osx.kill_tree(proc, force=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log("ollama would not die; leaving it")

    def status_line(self) -> str:
        return {"owned": "started by George", "external": "already running",
                "systemd": "the Ollama app" if IS_WINDOWS else "systemd",
                "missing": "not installed",
                "failed": "failed to start"}.get(self.state, "unknown")


# =====================================================================
# MODEL MANAGEMENT  --  pull, delete, browse, all from inside the app
# =====================================================================

# Ordered for THIS app: George drives a tool loop, so what matters is
# emitting clean JSON on the first try, not prose quality. Anything on
# ollama.com works via the free-text box; these are the ones worth
# starting from.
CURATED_MODELS = [
    ("qwen3:4b", "2.6 GB", "fast agent pick - trained for tool calls"),
    ("qwen3:8b", "5.2 GB", "same family, sharper, still quick"),
    ("granite4:3b", "2.1 GB", "tiny and tidy, built for tool calling"),
    ("llama3.2:3b", "2.0 GB", "smallest usable, good on a laptop"),
    ("gemma3:4b", "3.3 GB", "small, tool calls, vision too"),
    ("qwen2.5:7b", "4.7 GB", "proven all-rounder, follows JSON well"),
    ("mistral:7b", "4.1 GB", "light and quick"),
    ("deepseek-r1:7b", "4.7 GB", "reasoner - smart but slow in a loop"),
    ("qwen3:14b", "9.0 GB", "best quality here, wants ~12 GB VRAM"),
    ("qwen2.5-coder:7b", "4.7 GB", "code"),
    ("llava:7b", "4.7 GB", "vision"),
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
