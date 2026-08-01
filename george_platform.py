#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_platform.py -- the one place that knows which OS this is.

Everything that differs between Linux and Windows lives in here so the
rest of George can stay written once. GTK-free by design, same as the
core: every function in this file runs headless and can be tested
without a display, and the pure ones (path normalisation, argv
splitting, the safety tables) can be tested on EITHER OS by passing
windows=True/False explicitly instead of asking the interpreter.

Rules that shaped this file:

  * The safety tables are MERGED, not switched. The Windows destructive
    patterns are active on Linux too and vice versa. For a gate, over-
    scanning is the safe direction, and it means the gate that ships is
    the gate that gets tested on both boxes.

  * Windows facts come from ctypes and winreg wherever possible, not
    from spawning cmd. A GUI process spawning cmd.exe flashes a console
    window on screen, and `wmic` is gone from Windows 11 24H2.

  * Every subprocess started on Windows gets CREATE_NO_WINDOW. Miss one
    and the user sees a black box blink every time George checks the
    time.
"""

from __future__ import annotations

import base64
import ctypes
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = not IS_WINDOWS and not IS_MACOS

OS_LABEL = "windows" if IS_WINDOWS else ("macos" if IS_MACOS else "linux")

HOME = os.path.expanduser("~")

# Windows creation flags, spelled out so this module imports on Linux.
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


# =====================================================================
# DIRECTORIES
# =====================================================================

def _win_dir(env: str, *tail: str) -> str:
    root = os.environ.get(env, "")
    if not root:
        root = os.path.join(HOME, "AppData",
                            "Roaming" if env == "APPDATA" else "Local")
    return os.path.join(root, *tail)


def config_dir() -> str:
    if IS_WINDOWS:
        return _win_dir("APPDATA", "George")
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")),
        "george")


def data_dir() -> str:
    if IS_WINDOWS:
        return _win_dir("LOCALAPPDATA", "George")
    return os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")),
        "george")


def cache_dir() -> str:
    if IS_WINDOWS:
        return _win_dir("LOCALAPPDATA", "George", "cache")
    return os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")),
        "george")


# =====================================================================
# PROCESSES
# =====================================================================

def spawn_kwargs(detach: bool = False) -> Dict[str, Any]:
    """Popen kwargs that keep a child from taking the app down with it.

    POSIX gets its own session so a killpg cannot walk back up into us.
    Windows gets its own process group (so Ctrl-C in a console does not
    reach it) and, always, no console window.
    """
    if IS_WINDOWS:
        flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        if detach:
            flags |= DETACHED_PROCESS
        return {"creationflags": flags}
    return {"start_new_session": True}


def run_kwargs() -> Dict[str, Any]:
    """subprocess.run kwargs for a short blocking call."""
    if IS_WINDOWS:
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def kill_tree(proc: subprocess.Popen, force: bool = False) -> bool:
    """Kill a child AND its children. Returns False if it could not.

    `ollama serve` forks a runner; killing only the parent leaves the
    model resident and the port held, which is exactly the bug this
    function exists to avoid.
    """
    if proc is None or proc.poll() is not None:
        return True
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"] +
                           (["/F"] if force else []),
                           capture_output=True, timeout=15, **run_kwargs())
            return True
        except Exception:
            try:
                proc.kill()
                return True
            except Exception:
                return False
    import signal as _signal
    sig = _signal.SIGKILL if force else _signal.SIGTERM
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return True
    except Exception:
        try:
            proc.kill() if force else proc.terminate()
            return True
        except Exception:
            return False


def interrupt(proc: subprocess.Popen) -> bool:
    """Politely ask a child to stop -- SIGINT, or its Windows twin."""
    if proc is None:
        return False
    if IS_WINDOWS:
        try:
            proc.send_signal(getattr(__import__("signal"), "CTRL_BREAK_EVENT"))
            return True
        except Exception:
            return kill_tree(proc, force=True)
    import signal as _signal
    try:
        proc.send_signal(_signal.SIGINT)
        return True
    except Exception:
        return False


_OEM_CP: Optional[str] = None


def decode_output(raw: bytes) -> str:
    """Decode console output without mangling it.

    cmd.exe writes in the OEM code page (437/850), not the ANSI one
    Python picks by default, so `text=True` turns every box-drawing
    character and accented name into noise. Try UTF-8 first because
    modern tools emit it, then the real OEM page, then give up
    gracefully.
    """
    global _OEM_CP
    if not raw:
        return ""
    if not IS_WINDOWS:
        return raw.decode("utf-8", "replace")
    if _OEM_CP is None:
        try:
            _OEM_CP = "cp%d" % ctypes.windll.kernel32.GetOEMCP()
        except Exception:
            _OEM_CP = "cp437"
    for enc in ("utf-8", _OEM_CP, "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


# =====================================================================
# COMMAND PARSING  --  shared by the safety gate
# =====================================================================

def base_name(arg: str) -> str:
    """Last path component, splitting on BOTH separators always.

    Used only on command-name positions, so being aggressive costs
    nothing and stops `C:\\Windows\\System32\\format.com` from reading as
    an unknown name on a Linux-built test run.
    """
    a = arg.strip().strip("'\"")
    for sep in ("\\", "/"):
        if sep in a:
            a = a.rsplit(sep, 1)[-1]
    return a


def argv_variants(sub: str) -> Optional[List[List[str]]]:
    """Every plausible argv for one subcommand. None = unparseable.

    POSIX and Windows disagree about the backslash, so a string that is
    harmless read one way can be a different command read the other.
    The gate checks all of them; unparseable means refused.
    """
    out: List[List[str]] = []
    try:
        posix = shlex.split(sub, comments=True)
        if posix:
            out.append(posix)
    except ValueError:
        posix = None
    try:
        raw = shlex.split(sub, posix=False)
        win = [t.strip('"') for t in raw if t]
        if win and win not in out:
            out.append(win)
    except ValueError:
        win = None
    if posix is None and win is None:
        return None
    return out or [[]]


# Inline-command flags: everything after one of these is a whole new
# command that this gate has not looked at yet.
INLINE_FLAGS = {"-c", "-lc", "-ic", "/c", "/k", "/C", "/K",
                "-command", "--command", "-noprofile", "-nop"}

ENCODED_FLAGS = {"-encodedcommand", "-enc", "-ec", "-e"}


def decode_powershell(token: str) -> Optional[str]:
    """PowerShell -EncodedCommand is base64 UTF-16LE. Decode it so the
    gate sees the real command instead of an opaque blob."""
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=True)
    except Exception:
        return None
    for enc in ("utf-16-le", "utf-8"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if text.isprintable() or "\n" in text:
            return text
    return None


def expand_vars(text: str) -> str:
    """Expand $HOME, ${HOME} and %USERPROFILE% style references.

    A gate that knows one spelling of a path is a gate you get past by
    typing the other one.
    """
    out = text.replace("${HOME}", HOME).replace("$HOME", HOME)

    def _win(match: "re.Match[str]") -> str:
        name = match.group(1)
        return os.environ.get(name, os.environ.get(name.upper(), match.group(0)))

    return re.sub(r"%([A-Za-z_][A-Za-z0-9_()]*)%", _win, out)


_WIN_DRIVE = re.compile(r"^[a-z]:$", re.I)


def norm_target(text: str, windows: Optional[bool] = None) -> str:
    """Canonical form of a path operand for comparison against the
    critical list. Never touches the disk -- the gate stays a pure
    function of the string."""
    win = IS_WINDOWS if windows is None else windows
    t = expand_vars(text.strip().strip("'\""))
    if t.startswith("~"):
        t = HOME + t[1:]
    looks_windows = (bool(re.match(r"^[a-z]:[\\/]", t, re.I)) or
                     "\\" in t or bool(re.match(r"^%[A-Za-z_]+%", t)))
    if win or looks_windows:
        t = t.replace("/", "\\")
        t = re.sub(r"\\+", "\\\\", t)
        t = t.lower()
        if len(t) > 3 and t.endswith("\\"):
            t = t.rstrip("\\")
        if _WIN_DRIVE.match(t):
            t += "\\"
        return t
    t = re.sub(r"/+", "/", t)
    if len(t) > 1:
        t = t.rstrip("/")
    return t


# =====================================================================
# SAFETY TABLES  --  merged into the core gate, never switched
# =====================================================================

WINDOWS_CRITICAL_TARGETS = {
    "c:\\", "d:\\", "c:", "\\", "\\\\", "*", "*.*",
    "c:\\windows", "c:\\windows\\system32", "c:\\windows\\syswow64",
    "c:\\windows\\*", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\users", "c:\\users\\*", "c:\\boot",
    "c:\\recovery", "c:\\system volume information",
    "c:\\$recycle.bin", "c:\\perflogs",
    "%systemroot%", "%windir%", "%systemdrive%", "%programfiles%",
    "%userprofile%", "%public%", "%appdata%", "%localappdata%",
}

WINDOWS_SHELLS = {"cmd", "cmd.exe", "powershell", "powershell.exe",
                  "pwsh", "pwsh.exe", "wsl", "wsl.exe", "bash.exe"}

# Deliberately NOT the shells. cmd and powershell belong in _SHELLS so
# the gate recurses into what they were told to run; peeling them off as
# wrappers would throw that inner command away.
WINDOWS_WRAPPERS = {"start", "runas", "call", "wt", "conhost", "elevate"}

WINDOWS_DANGEROUS_CMDS = {
    "del", "erase", "rd", "rmdir", "format", "diskpart", "vssadmin",
    "wbadmin", "bcdedit", "cipher", "sdelete", "takeown", "icacls",
    "cacls", "attrib", "reg", "shutdown", "remove-item", "ri", "rm",
    "rmdir.exe", "clear-disk", "format-volume", "stop-computer",
    "restart-computer", "remove-partition", "initialize-disk",
    "set-content", "clear-content", "move-item", "rename-item",
}

# Whole-string patterns. Anything matching is destructive, full stop.
WINDOWS_HARD_NO = (
    re.compile(r"\bformat\b\s+[a-z]:", re.I),
    re.compile(r"\bformat-volume\b|\bclear-disk\b|\binitialize-disk\b", re.I),
    re.compile(r"\bremove-partition\b|\bset-disk\b|\bclear-recyclebin\b", re.I),
    re.compile(r"\bdiskpart\b", re.I),
    re.compile(r"\bvssadmin\b[^|;&]*\bdelete\b[^|;&]*\bshadows?\b", re.I),
    re.compile(r"\bwbadmin\b[^|;&]*\bdelete\b", re.I),
    re.compile(r"\bbcdedit\b[^|;&]*\s/(delete|deletevalue|set|import)\b",
               re.I),
    re.compile(r"\bbootrec\b|\bbootsect\b", re.I),
    re.compile(r"\bcipher\b\s+/w", re.I),
    re.compile(r"\bsdelete\b", re.I),
    re.compile(r"\bfsutil\b[^|;&]*\b(deletejournal|setzerodata|file\s+"
               r"setshortname)\b", re.I),
    re.compile(r"\breg\b\s+delete\s+\"?hk(lm|cr|u|ey_local_machine|"
               r"ey_classes_root|ey_users)\b", re.I),
    re.compile(r"\bremove-item(property)?\b[^|;&]*\bhk(lm|cr):", re.I),
    re.compile(r"\btakeown\b[^|;&]*/f\s+\"?[a-z]:\\?\"?(\s|$)", re.I),
    re.compile(r"\b(icacls|cacls)\b\s+\"?[a-z]:\\?\"?\s+/(reset|grant|deny)",
               re.I),
    re.compile(r"\bnet\b\s+user\s+\S+\s+/delete", re.I),
    re.compile(r"\bnet\b\s+localgroup\s+administrators\s+\S+\s+/delete", re.I),
    re.compile(r"\bwmic\b[^|;&]*\bdelete\b", re.I),
    re.compile(r"\bstop-computer\b|\brestart-computer\b", re.I),
    re.compile(r"\bshutdown\b\s+/[srlhg]\b", re.I),
    re.compile(r"\bsc\b\s+(delete|config)\s+", re.I),
    re.compile(r"\bschtasks\b\s+/delete", re.I),
    re.compile(r"\bdism\b[^|;&]*\b/remove", re.I),
    re.compile(r"\bsfc\b\s+/scannow\s*&", re.I),
    re.compile(r"\bwevtutil\b\s+(cl|clear-log)\b", re.I),
    re.compile(r"\bset-executionpolicy\b[^|;&]*\bunrestricted\b", re.I),
    re.compile(r"%0\s*\|\s*%0"),                       # cmd fork bomb
    re.compile(r"\bwhile\s*\(\s*1\s*\)\s*\{\s*start\b", re.I),
)

# Downloading straight into an interpreter, Windows spelling.
WINDOWS_PIPE_TO_SHELL = re.compile(
    r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b[^|]*\|"
    r"\s*(iex|invoke-expression|cmd|powershell|pwsh|python3?|node)\b", re.I)


def _no_flags(args: Sequence[str]) -> List[str]:
    return [a for a in args[1:] if not a.startswith(("-", "/"))]


def _win_verb(args: Sequence[str], allowed: Sequence[str]) -> bool:
    return any(a.lower().lstrip("-/") in allowed for a in args[1:])


def _ok_wmic(args: List[str]) -> bool:
    return _win_verb(args, ("get", "list", "brief", "full")) and \
        not _win_verb(args, ("delete", "call", "set", "create"))


def _ok_netsh(args: List[str]) -> bool:
    return _win_verb(args, ("show", "dump")) and \
        not _win_verb(args, ("set", "add", "delete", "reset"))


def _ok_sc(args: List[str]) -> bool:
    return _win_verb(args, ("query", "queryex", "qc", "qdescription",
                            "qfailure", "showsid", "getdisplayname"))


def _ok_reg(args: List[str]) -> bool:
    return _win_verb(args, ("query", "export", "compare"))


def _ok_powercfg(args: List[str]) -> bool:
    return _win_verb(args, ("list", "l", "query", "q", "a", "batteryreport",
                            "energy", "getactivescheme", "devicequery"))


def _ok_attrib(args: List[str]) -> bool:
    return not any(a.startswith(("+", "-")) for a in args[1:])


def _ok_chkdsk(args: List[str]) -> bool:
    bad = ("/f", "/r", "/x", "/b", "/spotfix")
    return not any(a.lower() in bad for a in args[1:])


def _ok_win_ping(args: List[str]) -> bool:
    """One checker for both dialects.

    These three names exist on Linux AND Windows with different flags,
    so the merged table needs a checker that understands both -- a
    Windows-only one would have quietly stopped `ping -c 3` from
    auto-running on Linux, which is exactly the kind of regression a
    merge like this introduces if nobody looks.
    """
    if any(a.lower() in ("-f", "--flood", "-t") for a in args[1:]):
        return False                    # unbounded: -t on Windows, -f on Linux
    return any(a.lower() in ("-c", "-n", "-w") for a in args[1:])


_ROUTE_WRITES = ("add", "delete", "del", "change", "flush", "replace", "-f",
                 "/f", "restore", "-p", "/p")


def _ok_route(args: List[str]) -> bool:
    return not any(a.lower() in _ROUTE_WRITES for a in args[1:])


def _ok_arp(args: List[str]) -> bool:
    return not any(a.lower() in ("-d", "/d", "-s", "/s") for a in args[1:])


def _ok_certutil(args: List[str]) -> bool:
    return _win_verb(args, ("hashfile", "dump", "verify", "store")) and \
        not _win_verb(args, ("urlcache", "decode", "encode", "addstore",
                             "delstore", "importpfx"))


def _ok_ps_cmdlet(args: List[str]) -> bool:
    """Read-only PowerShell verbs, and only those.

    `Format-` is deliberately NOT here: Format-Table is harmless and
    Format-Volume wipes a disk, and telling them apart by prefix is
    exactly the kind of cleverness that goes wrong once.
    """
    name = base_name(args[0]).lower()
    if name in ("get-content", "get-childitem", "get-item", "get-process",
                "get-service", "get-date", "get-location", "get-host",
                "get-command", "get-help", "get-member", "get-module",
                "get-clipboard", "get-computerinfo", "get-ciminstance",
                "get-wmiobject", "get-netipconfiguration", "get-netadapter",
                "get-netipaddress", "get-nettcpconnection", "get-netroute",
                "get-volume", "get-disk", "get-partition", "get-psdrive",
                "get-hotfix", "get-eventlog", "get-winevent",
                "get-filehash", "get-acl", "get-localuser",
                "get-localgroup", "get-timezone", "get-uptime",
                "get-random", "get-variable", "get-alias", "get-history",
                "test-path", "test-connection", "test-netconnection",
                "resolve-dnsname", "measure-object", "select-object",
                "select-string", "where-object", "sort-object",
                "convertto-json", "convertfrom-json", "out-string",
                "compare-object", "group-object"):
        return True
    return False


# name -> extra check, or None when the command is safe on its own.
WINDOWS_READ_ONLY: Dict[str, Any] = {
    # files and text
    "dir": None, "type": None, "more": None, "findstr": None,
    "fc": None, "comp": None, "tree": None, "where": None,
    "attrib": _ok_attrib, "certutil": _ok_certutil, "cd": None,
    "chdir": None, "vol": None, "label": None,
    # system facts
    "systeminfo": None, "hostname": None, "ver": None, "whoami": None,
    "wmic": _ok_wmic, "driverquery": None, "powercfg": _ok_powercfg,
    "reg": _ok_reg, "sc": _ok_sc, "chkdsk": _ok_chkdsk,
    "qwinsta": None, "query": None, "openfiles": None, "w32tm": None,
    "bcdedit": lambda a: len(a) == 1, "msinfo32": None,
    "set": lambda a: not any("=" in x for x in a[1:]),
    # processes
    "tasklist": None,
    # network, read side
    "ipconfig": lambda a: not _win_verb(a, ("release", "renew",
                                            "flushdns", "registerdns")),
    "getmac": None, "netstat": None, "nslookup": None, "tracert": None,
    "pathping": None, "netsh": _ok_netsh, "ping": _ok_win_ping,
    "route": _ok_route, "arp": _ok_arp,
    # package managers
    "winget": lambda a: _win_verb(a, ("list", "search", "show", "source",
                                      "--version", "features")),
    "choco": lambda a: _win_verb(a, ("list", "search", "info", "outdated")),
    "scoop": lambda a: _win_verb(a, ("list", "search", "info", "status",
                                     "which", "prefix")),
}
for _name in ("get-content", "get-childitem", "get-item", "get-process",
              "get-service", "get-date", "get-location", "get-host",
              "get-command", "get-help", "get-member", "get-module",
              "get-clipboard", "get-computerinfo", "get-ciminstance",
              "get-wmiobject", "get-netipconfiguration", "get-netadapter",
              "get-netipaddress", "get-nettcpconnection", "get-netroute",
              "get-volume", "get-disk", "get-partition", "get-psdrive",
              "get-hotfix", "get-eventlog", "get-winevent", "get-filehash",
              "get-acl", "get-localuser", "get-localgroup", "get-timezone",
              "get-uptime", "get-random", "get-variable", "get-alias",
              "get-history", "test-path", "test-connection",
              "test-netconnection", "resolve-dnsname", "measure-object",
              "select-object", "select-string", "where-object",
              "sort-object", "convertto-json", "convertfrom-json",
              "out-string", "compare-object", "group-object"):
    WINDOWS_READ_ONLY[_name] = _ok_ps_cmdlet

# `^` is cmd's escape character: `de^l` runs `del`. Anything carrying one
# is not auto-run, and the gate also re-checks the string with them
# stripped.
WINDOWS_NO_AUTORUN_CHARS = ("^", "%")


# =====================================================================
# WINDOWS FACTS  --  ctypes and winreg, no console flashes
# =====================================================================

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte),
                ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def _ft(f: _FILETIME) -> int:
    return (f.high << 32) | f.low


def win_cpu_times() -> Optional[Tuple[int, int]]:
    """(idle, total) in 100ns ticks, or None. Deltas give the load."""
    if not IS_WINDOWS:
        return None
    idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
    try:
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    except Exception:
        return None
    if not ok:
        return None
    # kernel time INCLUDES idle time, which is the trap in this API
    return _ft(idle), _ft(kern) + _ft(user)


def win_memory() -> Dict[str, int]:
    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    except Exception:
        return {}
    return {"total": stat.ullTotalPhys, "avail": stat.ullAvailPhys,
            "load": stat.dwMemoryLoad,
            "swap_total": stat.ullTotalPageFile,
            "swap_avail": stat.ullAvailPageFile}


def win_uptime() -> float:
    try:
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:
        return 0.0


def win_battery() -> str:
    st = _SYSTEM_POWER_STATUS()
    try:
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return ""
    except Exception:
        return ""
    pct = st.BatteryLifePercent
    if pct in (255, 0) and st.BatteryFlag == 128:
        return ""                       # no battery: a desktop
    plugged = "charging" if st.ACLineStatus == 1 else "on battery"
    if pct == 255:
        return plugged
    return "%d%% (%s)" % (pct, plugged)


def _reg_get(root: int, path: str, name: str) -> str:
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(root, path) as key:
            value, _kind = winreg.QueryValueEx(key, name)
            return str(value).strip()
    except Exception:
        return ""


def win_cpu_model() -> str:
    try:
        import winreg
        return _reg_get(winreg.HKEY_LOCAL_MACHINE,
                        r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                        "ProcessorNameString") or platform.processor()
    except Exception:
        return platform.processor()


def win_os_name() -> str:
    """Windows 11 still reports ProductName "Windows 10" -- the build
    number is the only honest answer, so use it."""
    try:
        import winreg
        cv = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        root = winreg.HKEY_LOCAL_MACHINE
        product = _reg_get(root, cv, "ProductName") or "Windows"
        build = _reg_get(root, cv, "CurrentBuildNumber")
        display = _reg_get(root, cv, "DisplayVersion")
        try:
            if int(build) >= 22000 and "11" not in product:
                product = product.replace("Windows 10", "Windows 11")
        except ValueError:
            pass
        parts = [product]
        if display:
            parts.append(display)
        if build:
            parts.append("build " + build)
        return " ".join(parts)
    except Exception:
        return "Windows " + platform.release()


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong),
                ("DeviceName", ctypes.c_wchar * 32),
                ("DeviceString", ctypes.c_wchar * 128),
                ("StateFlags", ctypes.c_ulong),
                ("DeviceID", ctypes.c_wchar * 128),
                ("DeviceKey", ctypes.c_wchar * 128)]


def win_gpu() -> str:
    """Adapter name straight from the display driver -- no wmic, which
    Windows 11 24H2 removed."""
    names: List[str] = []
    dev = _DISPLAY_DEVICEW()
    dev.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
    i = 0
    try:
        while ctypes.windll.user32.EnumDisplayDevicesW(None, i,
                                                       ctypes.byref(dev), 0):
            name = dev.DeviceString.strip()
            if name and name not in names:
                names.append(name)
            i += 1
            if i > 16:
                break
    except Exception:
        return ""
    return ", ".join(names[:2])


def win_session() -> Tuple[str, str]:
    """(desktop, session) -- the Windows answers to XDG_CURRENT_DESKTOP."""
    shell = "explorer"
    try:
        if not ctypes.windll.user32.GetShellWindow():
            shell = "no shell"
    except Exception:
        pass
    remote = False
    try:
        remote = bool(ctypes.windll.user32.GetSystemMetrics(0x1000))
    except Exception:
        pass
    return ("Windows Shell (%s)" % shell,
            "remote desktop" if remote else "windows")


def win_drives() -> List[str]:
    """Every mounted drive letter, via the API rather than by guessing."""
    out: List[str] = []
    try:
        buf = ctypes.create_unicode_buffer(512)
        n = ctypes.windll.kernel32.GetLogicalDriveStringsW(511, buf)
        if n:
            for item in buf[:n].split("\0"):
                if item:
                    out.append(item)
    except Exception:
        pass
    return out or ["C:\\"]


def win_ollama_paths() -> List[str]:
    """Ollama's installer does not always leave ollama.exe on the PATH a
    GUI process inherits, so look where it actually lands."""
    roots = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Ollama"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Ollama"),
        os.path.join(HOME, "AppData", "Local", "Programs", "Ollama"),
        os.path.join(os.environ.get("ProgramData", ""), "chocolatey", "bin"),
        os.path.join(os.environ.get("USERPROFILE", HOME), "scoop", "shims"),
    ]
    out = []
    for root in roots:
        if root and os.path.isdir(root):
            out.append(root)
    return out


def find_binary(name: str) -> Optional[str]:
    """shutil.which, plus the places Windows installers actually use."""
    hit = shutil.which(name)
    if hit:
        return hit
    if not IS_WINDOWS:
        return None
    exts = [".exe", ".cmd", ".bat", ""]
    extra = win_ollama_paths() + [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft",
                     "WindowsApps"),
    ]
    for folder in extra:
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate):
                return candidate
    # App Paths is how the shell resolves `chrome`, `code`, `notepad++`
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for ext in (".exe", ""):
                path = _reg_get(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
                    "\\" + name + ext, "")
                if path and os.path.isfile(path.strip('"')):
                    return path.strip('"')
    except Exception:
        pass
    return None


# =====================================================================
# WINDOWS ACTIONS
# =====================================================================

_PS = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
       "Bypass", "-Command", "-"]


def powershell(script: str, timeout: int = 30) -> Tuple[int, str]:
    """Run a PowerShell snippet with the script on stdin.

    stdin, not -Command "...", on purpose: it removes every quoting
    question at once, and quoting is where this kind of code goes wrong.
    """
    exe = find_binary("pwsh") or find_binary("powershell")
    if not exe:
        return 1, "powershell not found"
    argv = [exe] + _PS[1:]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **run_kwargs())
        out, err = proc.communicate(script.encode("utf-8"), timeout=timeout)
        return proc.returncode, decode_output(out or err).strip()
    except subprocess.TimeoutExpired:
        return 124, "[powershell timed out after %ds]" % timeout
    except Exception as exc:
        return 1, "powershell failed: %s" % exc


def win_open(target: str) -> str:
    """The shell's own idea of "open this" -- browser, file or folder."""
    try:
        os.startfile(target)            # noqa: attr-defined (Windows only)
        return "opened %s" % target
    except Exception as exc:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", target],
                             **spawn_kwargs(detach=True))
            return "opened %s" % target
        except Exception:
            return "could not open %s: %s" % (target, exc)


def win_clipboard_get() -> str:
    """CF_UNICODETEXT off the real clipboard, PowerShell as backup."""
    CF_UNICODETEXT = 13
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    try:
        if not user32.OpenClipboard(None):
            raise OSError("clipboard is held by another process")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(ctypes.c_void_p(handle))
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                kernel32.GlobalUnlock(ctypes.c_void_p(handle))
        finally:
            user32.CloseClipboard()
    except Exception:
        rc, out = powershell("Get-Clipboard -Raw", timeout=15)
        return out if rc == 0 else ""


def win_clipboard_set(text: str) -> str:
    GMEM_MOVEABLE = 0x0002
    CF_UNICODETEXT = 13
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    try:
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise OSError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(ctypes.c_void_p(handle))
        ctypes.memmove(ptr, ctypes.byref(data), size)
        kernel32.GlobalUnlock(ctypes.c_void_p(handle))
        if not user32.OpenClipboard(None):
            raise OSError("clipboard is held by another process")
        try:
            user32.EmptyClipboard()
            user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(handle))
        finally:
            user32.CloseClipboard()
        return "copied %d chars to the clipboard" % len(text)
    except Exception:
        exe = find_binary("clip")
        if exe:
            try:
                proc = subprocess.Popen([exe], stdin=subprocess.PIPE,
                                        **run_kwargs())
                proc.communicate(text.encode("utf-16-le"), timeout=10)
                return "copied %d chars to the clipboard" % len(text)
            except Exception:
                pass
        return "clipboard write failed"


_SHOT_PS = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object Drawing.Bitmap $b.Width, $b.Height
$g = [Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [Drawing.Point]::Empty, $b.Size)
$bmp.Save('%s', [Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""


def win_screenshot(path: str) -> Tuple[bool, str]:
    """Whole virtual desktop, every monitor, no third-party tool."""
    rc, out = powershell(_SHOT_PS % path.replace("'", "''"), timeout=45)
    if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 1000:
        return True, path
    return False, out or "screen capture failed"


# WM_APPCOMMAND: the same messages a keyboard's media keys send, so it
# works with whatever mixer and player the box actually has.
_WM_APPCOMMAND = 0x0319
_APPCOMMAND = {
    "mute": 8, "volume_down": 9, "volume_up": 10,
    "next": 11, "previous": 12, "prev": 12, "stop": 13,
    "toggle": 14, "play": 14, "pause": 14,
}


def win_appcommand(name: str, repeat: int = 1) -> bool:
    cmd = _APPCOMMAND.get(name)
    if cmd is None:
        return False
    try:
        hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        ctypes.windll.user32.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        for _ in range(max(1, repeat)):
            ctypes.windll.user32.SendMessageW(
                ctypes.c_void_p(hwnd), _WM_APPCOMMAND,
                ctypes.c_void_p(hwnd), ctypes.c_void_p(cmd << 16))
        return True
    except Exception:
        return False


def win_volume_level() -> str:
    """Reading the master level needs the COM audio endpoint API, which
    is a lot of hand-written vtable calls for one number. George says so
    rather than guessing."""
    return ""


def win_power(action: str) -> str:
    table = {
        "shutdown": (["shutdown", "/s", "/t", "0"], "shutting down"),
        "poweroff": (["shutdown", "/s", "/t", "0"], "shutting down"),
        "reboot": (["shutdown", "/r", "/t", "0"], "rebooting"),
        "restart": (["shutdown", "/r", "/t", "0"], "rebooting"),
        "logout": (["shutdown", "/l"], "logging out"),
        "logoff": (["shutdown", "/l"], "logging out"),
        "hibernate": (["shutdown", "/h"], "hibernating"),
        "lock": (["rundll32.exe", "user32.dll,LockWorkStation"], "locking"),
        "suspend": (["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                    "sleeping"),
        "sleep": (["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                  "sleeping"),
    }
    entry = table.get(action.strip().lower())
    if not entry:
        return "unknown power action %r" % action
    argv, said = entry
    try:
        subprocess.Popen(argv, **spawn_kwargs(detach=True))
        return said
    except Exception as exc:
        return "power action failed: %s" % exc


_NOTIFY_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.BalloonTipTitle = %s
$n.BalloonTipText = %s
$n.ShowBalloonTip(6000)
Start-Sleep -Seconds 7
$n.Dispose()
"""


def _ps_str(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def win_notify(title: str, body: str) -> None:
    exe = find_binary("powershell")
    if not exe:
        return
    script = _NOTIFY_PS % (_ps_str(title), _ps_str(body or " "))
    try:
        proc = subprocess.Popen(
            [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-Command", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **spawn_kwargs(detach=True))
        proc.stdin.write(script.encode("utf-8"))
        proc.stdin.close()
    except Exception:
        pass


def win_processes(sort_by: str, limit: int) -> str:
    """tasklist is instant and always present; PowerShell only when CPU
    time is actually what was asked for."""
    if sort_by == "cpu":
        rc, out = powershell(
            "Get-Process | Sort-Object CPU -Descending | "
            "Select-Object -First %d Name,Id,CPU,WS | "
            "ForEach-Object { '{0,-24} {1,8} {2,9:N1}s {3,9:N0} MB' -f "
            "$_.Name,$_.Id,$_.CPU,($_.WS/1MB) }" % limit, timeout=30)
        if rc == 0 and out:
            return ("NAME                          PID   CPU-TIME    MEMORY\n"
                    + out)
    exe = find_binary("tasklist")
    if not exe:
        return "no process listing tool found"
    try:
        proc = subprocess.run([exe, "/FO", "CSV", "/NH"],
                              capture_output=True, timeout=30, **run_kwargs())
    except Exception as exc:
        return "tasklist failed: %s" % exc
    import csv
    import io
    rows: List[Tuple[int, str, str]] = []
    for row in csv.reader(io.StringIO(decode_output(proc.stdout))):
        if len(row) < 5:
            continue
        kb = re.sub(r"[^0-9]", "", row[4]) or "0"
        rows.append((int(kb), row[0], row[1]))
    rows.sort(reverse=True)
    lines = ["NAME                          PID      MEMORY"]
    for kb, name, pid in rows[:limit]:
        lines.append("%-28s %6s %8.0f MB" % (name[:28], pid, kb / 1024.0))
    return "\n".join(lines)


def win_network() -> Dict[str, str]:
    info: Dict[str, str] = {}
    try:
        host = socket.gethostname()
        addrs = sorted(set(socket.gethostbyname_ex(host)[2]))
        info["addresses"] = ", ".join(a for a in addrs
                                      if not a.startswith("127."))
    except Exception:
        pass
    exe = find_binary("netsh")
    if exe:
        try:
            proc = subprocess.run([exe, "wlan", "show", "interfaces"],
                                  capture_output=True, timeout=15,
                                  **run_kwargs())
            text = decode_output(proc.stdout)
            ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", text, re.M)
            signal = re.search(r"^\s*Signal\s*:\s*(.+)$", text, re.M)
            if ssid:
                info["wifi"] = ssid.group(1).strip()
            if signal:
                info["signal"] = signal.group(1).strip()
        except Exception:
            pass
    exe = find_binary("route")
    if exe:
        try:
            proc = subprocess.run([exe, "print", "0.0.0.0"],
                                  capture_output=True, timeout=15,
                                  **run_kwargs())
            match = re.search(r"0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)",
                              decode_output(proc.stdout))
            if match and match.group(1).lower() != "on-link":
                info["gateway"] = match.group(1)
        except Exception:
            pass
    return info


def win_pkg_mgr() -> Optional[str]:
    for name in ("winget", "choco", "scoop"):
        if find_binary(name):
            return name
    return None


def win_launch(name: str) -> str:
    exe = find_binary(name)
    if exe:
        try:
            subprocess.Popen([exe], **spawn_kwargs(detach=True))
            return "launched %s" % name
        except Exception as exc:
            return "launch failed: %s" % exc
    try:
        subprocess.Popen(["cmd", "/c", "start", "", name],
                         **spawn_kwargs(detach=True))
        return "asked the shell to start %s" % name
    except Exception as exc:
        return "could not start %r: %s" % (name, exc)


def win_temp_wav(tag: str) -> str:
    return os.path.join(tempfile.gettempdir(),
                        "george-%s-%d.wav" % (tag, int(time.time() * 1000)))


def win_set_app_id(app_id: str) -> None:
    """Group the taskbar button under George's own identity instead of
    lumping it in with every other Python window."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def describe() -> str:
    """One line for the log, so a bug report says which OS it came from."""
    return "%s %s (%s)" % (OS_LABEL, platform.release(), platform.machine())
