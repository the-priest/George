#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
tests/test_windows.py -- the Windows half, checked from anywhere.

Everything in here runs on Linux. That is the whole point: the safety
tables and the parsers are merged rather than switched on os.name, so
the Windows behaviour can be pinned by the same suite that runs on his
box instead of only being exercised on a machine nobody tests on.

What it cannot check is the ctypes calls and the ollama lifecycle on a
real Windows kernel. Those are marked as such at the bottom rather than
being quietly skipped.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

import george_core as core                                   # noqa: E402
import george_platform as osx                                 # noqa: E402

fails = 0
checks = 0


def check(cond, label):
    global fails, checks
    checks += 1
    if not cond:
        fails += 1
        print("  FAIL %s" % label)


# =====================================================================
# 1. DESTRUCTIVE GATE  --  Windows spellings must be caught
# =====================================================================

MUST_REFUSE = [
    r"del /f /s /q C:\*",
    r"del /s /q C:\Windows",
    r"rd /s /q C:\Windows\System32",
    r"rmdir /s /q %SystemRoot%",
    r"format C: /fs:ntfs /q",
    r"format c:",
    "diskpart",
    "vssadmin delete shadows /all /quiet",
    "wbadmin delete catalog -quiet",
    "bcdedit /set {default} recoveryenabled No",
    "cipher /w:C",
    r"takeown /f C:\ /r /d y",
    r"icacls C:\ /reset /t",
    r'reg delete "HKLM\SOFTWARE" /f',
    r"reg delete HKLM\SYSTEM /f",
    "Remove-Item -Recurse -Force C:\\",
    "Remove-Item C:\\Windows -Recurse",
    "Clear-Disk -Number 0 -RemoveData",
    "Format-Volume -DriveLetter C",
    "Initialize-Disk -Number 1",
    "Stop-Computer -Force",
    "Restart-Computer",
    "shutdown /s /t 0",
    "shutdown /r /f /t 0",
    "net user administrator /delete",
    "sc delete WinDefend",
    "schtasks /delete /tn Backup /f",
    "wevtutil cl Security",
    "fsutil usn deletejournal /d C:",
    # obfuscation: cmd's caret escape must not smuggle del past the gate
    r"de^l /s /q C:\Windows",
    r"r^d /s /q C:\Windows",
    # inline shells: the gate has to look INSIDE these
    r'cmd /c del /s /q C:\Windows',
    r'cmd.exe /c "rd /s /q C:\Windows"',
    r'powershell -Command "Remove-Item -Recurse -Force C:\\"',
    r'powershell -c "Format-Volume -DriveLetter C"',
    # base64 UTF-16LE of: rm -rf /
    "powershell -EncodedCommand cgBtACAALQByAGYAIAAvAA==",
    # start is a wrapper, not a hiding place
    r"start /b del /s /q C:\Windows",
    # and the Linux side must still be caught, unchanged
    "rm -rf /",
    "rm -rf $HOME",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/nvme0n1",
]

MUST_ALLOW = [
    "dir",
    r"dir C:\Users\luka\Documents",
    r"del %TEMP%\george-shot-1.png",
    "Remove-Item .\\build -Recurse",
    r"del C:\Users\luka\Downloads\old.zip",
    "systeminfo",
    "tasklist",
    "ipconfig /all",
    "Get-Process",
    "winget list",
    "git status",
    "reg query HKCU\\Environment",
    "ls -la",
    "rm ./build/temp.o",
]

print("== windows destructive gate ==")
for cmd in MUST_REFUSE:
    check(core.is_destructive_command(cmd), "should REFUSE: %s" % cmd)
for cmd in MUST_ALLOW:
    check(not core.is_destructive_command(cmd), "false positive: %s" % cmd)


# =====================================================================
# 2. AUTO-RUN ALLOWLIST  --  the Windows half of "runs without asking"
# =====================================================================

CFG = dict(core.DEFAULTS)
CFG["auto_run_commands"] = False

RUN_FREE = [
    "dir", "systeminfo", "hostname", "ver", "whoami", "tasklist",
    "ipconfig", "ipconfig /all", "getmac", "netstat -ano", "tracert 1.1.1.1",
    "nslookup ollama.com", "driverquery", "qwinsta",
    "where python", "type README.md", "findstr TODO george.py",
    "tree", "fc a.txt b.txt", "vol",
    "reg query HKCU\\Environment", "sc query wuauserv",
    "netsh wlan show interfaces", "netsh interface show interface",
    "powercfg /list", "wmic cpu get name", "route print",
    "arp -a", "ping -n 3 1.1.1.1", "chkdsk C:",
    "winget list", "winget search ollama", "choco list", "scoop list",
    "certutil -hashfile george.py SHA256",
    "attrib george.py", "getmac /v",
    "Get-Process", "Get-ChildItem", "Get-Content george.py",
    "Get-ComputerInfo", "Get-NetIPConfiguration", "Get-Volume",
    "Test-Path C:\\Windows", "Resolve-DnsName ollama.com",
    "Get-FileHash george.py", "Select-String TODO george.py",
    # and the Linux side, unchanged
    "uname -a", "ping -c 3 1.1.1.1", "ls -la", "git status", "df -h",
]

MUST_ASK = [
    # writes, deletes, escalation, service control
    r"del C:\Users\luka\notes.txt",
    "Remove-Item .\\build -Recurse",
    "runas /user:Administrator cmd",
    "netsh interface set interface Ethernet disable",
    "netsh wlan disconnect",
    "sc config wuauserv start=disabled",
    "sc stop wuauserv",
    "reg add HKCU\\Environment /v X /d Y",
    "reg delete HKCU\\Environment /v X",
    "winget install Ollama.Ollama",
    "choco install ollama -y",
    "scoop install ollama",
    "attrib +h secret.txt",
    "chkdsk C: /f",
    "ipconfig /flushdns",
    "ipconfig /release",
    "powercfg /setactive SCHEME_MIN",
    "wmic process call create notepad.exe",
    "Stop-Process -Name notepad",
    "Set-Content out.txt hello",
    "route add 10.0.0.0 mask 255.0.0.0 10.0.0.1",
    "arp -d 10.0.0.1",
    "ping -t 1.1.1.1",
    "certutil -urlcache -f http://x/y.exe y.exe",
    "wmic product where name='x' call uninstall",
    # obfuscation and escape hatches
    "dir > out.txt",
    "dir & del x.txt",
    "di^r",
    "Get-Process; Stop-Process -Name notepad",
    'cmd /c "del x.txt"',
    "powershell -EncodedCommand cgBtACAALQByAGYAIAAvAA==",
    # unknown names are not on the list, so they ask
    "mysterytool --wipe",
]

print("== windows auto-run gate ==")
for cmd in RUN_FREE:
    check(not core.command_needs_confirmation(cmd, CFG),
          "should RUN FREE but asks: %s" % cmd)
for cmd in MUST_ASK:
    check(core.command_needs_confirmation(cmd, CFG),
          "should ASK but runs free: %s" % cmd)


# =====================================================================
# 3. PARSERS
# =====================================================================

print("== parsers ==")
check(osx.base_name(r"C:\Windows\System32\format.com") == "format.com",
      "windows basename")
check(osx.base_name("/usr/bin/rm") == "rm", "posix basename")
check(osx.norm_target(r"C:\Windows\\", windows=True) == r"c:\windows",
      "trailing separator and case folded")
check(osx.norm_target("C:/Windows", windows=True) == r"c:\windows",
      "forward slashes on windows normalise to backslashes")
check(osx.norm_target("c:", windows=True) == "c:\\", "bare drive is a root")
check(osx.norm_target("/usr/") == "/usr", "posix normalisation untouched")

variants = osx.argv_variants(r"del /s /q C:\Windows")
check(variants is not None and len(variants) == 2,
      "backslash gives two readings")
check(any("C:\\Windows" in v for v in variants),
      "one reading keeps the backslash")

check(osx.decode_powershell("cgBtACAALQByAGYAIAAvAA==") == "rm -rf /",
      "encoded command decodes")
check(osx.decode_powershell("!!!not base64!!!") is None,
      "junk does not decode")

os.environ["GEORGE_TEST_VAR"] = "/tmp/x"
check(osx.expand_vars("%GEORGE_TEST_VAR%/y") == "/tmp/x/y",
      "percent expansion")
check(osx.expand_vars("$HOME") == os.path.expanduser("~"),
      "dollar expansion still works")

check(osx.config_dir() and osx.data_dir() and osx.cache_dir(),
      "all three directories resolve")
check(osx.spawn_kwargs(detach=True), "spawn kwargs are non-empty")


# =====================================================================
# 4. CROSS-MODULE ATTRIBUTE AUDIT
#
# pyflakes does NOT resolve `osx.something` against the real module, so
# a name that never existed -- or one renamed later -- sails through
# every static check and only explodes at run time, on Windows, where
# nobody is watching. Walk the AST of every module and hasattr each
# attribute against the real thing.
# =====================================================================

print("== platform attribute audit ==")
here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
seen = 0
for name in sorted(os.listdir(here)):
    if not name.startswith("george") or not name.endswith(".py"):
        continue
    if name == "george_platform.py":
        continue
    tree = ast.parse(open(os.path.join(here, name), encoding="ascii").read())
    alias = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "george_platform":
                    alias = item.asname or item.name
    if not alias:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and \
                isinstance(node.value, ast.Name) and node.value.id == alias:
            seen += 1
            check(hasattr(osx, node.attr),
                  "%s uses osx.%s which does not exist" % (name, node.attr))
check(seen > 20, "audit actually found attribute uses (found %d)" % seen)


# =====================================================================
# 5. DISPATCH  --  does the Windows branch actually get taken?
# =====================================================================

print("== dispatch ==")


class _Fake:
    """Stand in for the Windows implementations and record the calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append(a)
        return "windows-path"


saved = {}
for fn in ("win_open", "win_launch", "win_power", "win_clipboard_get",
           "win_clipboard_set", "win_processes", "win_network",
           "win_pkg_mgr"):
    saved[fn] = getattr(osx, fn)
core_windows = core.IS_WINDOWS
try:
    core.IS_WINDOWS = True
    for fn in saved:
        setattr(osx, fn, _Fake())
    check(core.launch_app("notepad") == "windows-path", "launch dispatches")
    check(core.power_action("lock") == "windows-path", "power dispatches")
    check(core.clipboard_read() == "windows-path", "clipboard read dispatches")
    check(core.clipboard_write("x") == "windows-path",
          "clipboard write dispatches")
    check(core.list_processes() == "windows-path", "processes dispatch")
    check(core.open_in_browser("example.com", {"browser": ""}) ==
          "windows-path", "browser failure message is passed through")
    check(core.distro_id() == ("windows", "windows"), "distro id")
    hint = core.install_hint("ollama")
    check("ollama" in hint.lower(), "install hint mentions ollama: %s" % hint)
    check(core.volume_control("get").startswith("Windows does not report"),
          "volume level is admitted as unknown, not invented")
finally:
    core.IS_WINDOWS = core_windows
    for fn, orig in saved.items():
        setattr(osx, fn, orig)

check(core.CONFIG_DIR and core.DATA_DIR, "core still has its directories")

print("\nwindows checks: %d; failures: %d" % (checks, fails))
print("NOT covered here (needs a real Windows kernel): ctypes calls into "
      "user32/kernel32, SAPI speech, the PyInstaller bundle, ollama "
      "process-tree teardown.")
sys.exit(1 if fails else 0)
