#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_tools.py -- the tool registry, the prompt, and the agent loop.

deepseek-r1 is a reasoner, not a function-calling model.  It will fence
its JSON, prepend a sentence, emit two objects, or reach for a tool name
that does not exist.  Everything here is built around parsing that
forgivingly without ever inventing a call that was not made.
"""

from __future__ import annotations

import ast
import json
import os
import re
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Tuple

from george_core import (
    APP_NAME, DEFAULT_FEEDS, HOME, POWER_ACTIONS, MemoryStore, NOTES_PATH,
    machine_summary,
    Ollama, OllamaError, _ensure_dirs, clipboard_read, clipboard_write,
    disk_report, fetch_news, find_files, html_to_text, http_get,
    inside_sandbox, is_destructive_command, is_network_pipe_to_shell,
    command_needs_confirmation, launch_app, list_processes, log,
    media_control, network_status, notify, open_in_browser, open_path,
    power_action, run_shell, safe_calc, strip_reasoning, system_status,
    take_screenshot, volume_control, weather, web_search, write_text_file,
)
from george_voice import TextToSpeech


# =====================================================================
# TOOLS
#
# Every tool takes (args, agent) and returns a plain-text observation
# that goes straight back into the model's context.  Keep them terse:
# a 7B model on an 8k window drowns in wall-of-text observations.
# =====================================================================

TOOL_SPEC = """\
web_search   {"query": str, "count": int}      search the web
open_page    {"url": str}                      fetch a page and read it
news         {"topic": str, "count": int}      pull headlines from the feeds
show         {"url": str}                      OPEN IT ON HIS SCREEN in the browser
open_path    {"path": str}                     open a local file or folder on his screen
weather      {"location": str}                 current conditions + today
system       {}                                cpu, memory, disk, battery, uptime
processes    {"sort": "cpu"|"memory"}          what is eating the box
network      {}                                interfaces, addresses, wifi, gateway
disk         {}                                filesystem usage
run          {"command": str}                  one shell command on this box
launch       {"app": str}                      start a desktop application
media        {"action": str}                   play|pause|next|previous|volume_up|volume_down|mute|current
volume       {"action": "up"|"down"|"mute"|"get", "level": int}
power        {"action": "lock"|"suspend"|"logout"|"reboot"|"shutdown"}
clipboard    {"mode": "read"|"write", "text": str}
screenshot   {}                                grab the screen and show it in chat
see          {"question": str}               LOOK at his screen and answer about it
read_file    {"path": str}                     read a text file
write_file   {"path": str, "text": str, "append": bool}   needs his OK
find         {"pattern": str, "path": str}     search for files by name
list_dir     {"path": str}                     list a directory
note         {"text": str}                     append to his notes file
remember     {"key": str, "value": str}        store a fact for good
recall       {"query": str}                    search stored facts
forget       {"key": str}
calc         {"expression": str}               arithmetic
timer        {"seconds": int, "label": str}    notify him later
say          {"text": str}                     speak out loud without ending the turn
answer       {"text": str}                     FINAL reply to him - ends the turn\
"""

# Three registers.  "jarvis" is the default and the one he asked for:
# unhurried, precise, a little dry, never chirpy.
PERSONAS = {
    "jarvis": """VOICE
You are unhurried and precise, like a good butler who happens to run on a GPU. \
Dry wit, used sparingly and never at his expense. You confirm what you did in \
as few words as it takes, then stop. If something is wrong you say so first and \
soften nothing.
Openers you use: "Done." "Already on screen." "Two things worth flagging."
Openers you never use: "Certainly!" "I'd be happy to help!" "Great question!"
You do not narrate your plan before doing it, you do not apologise for working, \
and you never end with an offer of further assistance. He will ask.""",
    "plain": """VOICE
Clear, neutral, helpful. Full sentences, no filler, no cheerleading. State what \
you did and what you found.""",
    "blunt": """VOICE
Short. Blunt. No pleasantries, no hedging, no closing offers. Answer, then \
stop. Swearing is fine if it is his register first.""",
}

SYSTEM_PROMPT = """You are George, a local AI running on {distro} as {name}'s \
desktop assistant. Everything about you is on this machine: a local Ollama \
model, no API key, no cloud, no telemetry. You are Basilisk's brother, same \
build without the security tooling - you are NOT a hacking tool and you do not \
do offensive security. If asked for that, say so in one line and move on.

{persona}

HOW YOU ACT
You do things instead of describing how they could be done.
To use a tool, output ONE raw JSON object and nothing else:
{{"tool": "web_search", "args": {{"query": "irish budget 2026"}}}}
No markdown fence, no commentary around it, one object per turn. You get the \
result back as an observation, then you decide the next move.

When you are finished, reply with:
{{"tool": "answer", "args": {{"text": "your reply to him"}}}}

TOOLS
{tools}

RULES
- Do not guess at anything current. The date, news, weather, prices, what is on \
his disk: look it up with a tool first, then answer from what came back.
- "show me", "put it on screen", "open it" means the `show` or `open_path` \
tool. He wants the thing in front of him, not a description of it.
- You already know what box you are on - it is in CONTEXT below, including \
the operating system. Use that instead of asking him, and write commands in \
the right dialect for it: never hand a Windows box `ls -la` or a Linux box \
`dir`.
- Read-only commands run immediately without bothering him. On Linux that is \
uname, ls, cat, grep, find, ps, df, free, lscpu, systemctl status, \
journalctl, ip addr, git status and package queries; on Windows it is dir, \
type, systeminfo, tasklist, ipconfig, netstat, where, findstr, reg query, \
sc query, wmic get, winget list and the Get-* PowerShell cmdlets. Just run \
them and answer. Anything that CHANGES the machine asks him first, so do not \
batch changes into a read.
- One shell command at a time. Never chain a second one onto a reply.
- On Arch and CachyOS use `pacman -Syu <pkg>`, never a bare `-S`. On Windows \
prefer `winget install --id <id> -e`.
- Anything that touches his files, his session or his power state gets \
confirmed by him first. A declined action is a no, not a retry.
- Keep the reasoning short. He wants the result, not the working.
- Answers are read on screen AND spoken aloud, so write them to be heard: \
short sentences, no ASCII tables, no emoji, no walls of text. Markdown for \
emphasis, lists and code is fine and renders properly.
- Never invent tool output. If a tool fails, say what failed and what you would \
need to make it work.

{extra}"""


def _fmt_results(results: List[Dict[str, str]]) -> str:
    if not results:
        return "no results"
    out = []
    for i, r in enumerate(results, 1):
        line = "%d. %s\n   %s" % (i, r.get("title", "?"), r.get("url", ""))
        if r.get("snippet"):
            line += "\n   %s" % r["snippet"][:280]
        out.append(line)
    return "\n".join(out)


def tool_web_search(args: Dict[str, Any], ag: "Agent") -> str:
    q = str(args.get("query", "")).strip()
    if not q:
        return "no query given"
    n = int(args.get("count") or 6)
    ag.step("searching the web: %s" % q)
    res = web_search(q, max(3, min(n, 10)))
    ag.tool_card("web_search", q, "%d results" % len(res))
    return _fmt_results(res)


def tool_open_page(args: Dict[str, Any], ag: "Agent") -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "no url given"
    if not url.startswith("http"):
        url = "https://" + url
    ag.step("reading %s" % urllib.parse.urlparse(url).netloc)
    try:
        text = html_to_text(http_get(url, timeout=25), 6000)
    except Exception as exc:
        return "could not read %s: %s" % (url, exc)
    ag.tool_card("open_page", url, "%d chars" % len(text))
    return "PAGE %s\n%s" % (url, text)


def tool_news(args: Dict[str, Any], ag: "Agent") -> str:
    topic = str(args.get("topic", "") or "").strip()
    count = int(args.get("count") or ag.cfg.get("news_count") or 12)
    ag.step("pulling headlines%s" % (" about %s" % topic if topic else ""))
    items = fetch_news(ag.cfg.get("feeds") or DEFAULT_FEEDS,
                       per_feed=6, topic=topic)
    items = items[:max(3, min(count, 30))]
    ag.show_news(items)
    ag.tool_card("news", topic or "top stories", "%d headlines" % len(items))
    if not items:
        return "no headlines matched"
    lines = []
    for i, it in enumerate(items, 1):
        lines.append("%d. [%s] %s\n   %s" %
                     (i, it["source"], it["title"], it["url"]))
        if it.get("summary"):
            lines.append("   %s" % it["summary"][:200])
    return ("Headlines are now on his screen in the News panel.\n" +
            "\n".join(lines))


def tool_show(args: Dict[str, Any], ag: "Agent") -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "no url given"
    ag.step("opening %s on screen" % url)
    msg = open_in_browser(url, ag.cfg)
    ag.tool_card("show", url, "browser")
    return msg


def tool_weather(args: Dict[str, Any], ag: "Agent") -> str:
    loc = str(args.get("location", "") or ag.cfg.get("location") or "")
    ag.step("checking the weather")
    w = weather(loc)
    if w.get("error"):
        return w["error"]
    ag.show_weather(w)
    return ("%s: %s, %sC (feels %sC), wind %s km/h, humidity %s%%, "
            "today %s to %sC" % (w["place"], w["desc"], w["temp_c"],
                                 w["feels_c"], w["wind_kph"], w["humidity"],
                                 w["min_c"], w["max_c"]))


def tool_system(args: Dict[str, Any], ag: "Agent") -> str:
    ag.step("reading system vitals")
    st = system_status()
    ag.show_vitals(st)
    return "\n".join("%s: %s" % (k, v) for k, v in st.items()
                     if not k.endswith("_pct"))


def tool_run(args: Dict[str, Any], ag: "Agent") -> str:
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return "no command given"
    if is_destructive_command(cmd):
        ag.tool_card("run", cmd, "REFUSED")
        return ("REFUSED: that command is destructive. I will not run it, and "
                "there is no override. Tell him what it would have done.")
    if is_network_pipe_to_shell(cmd):
        ag.tool_card("run", cmd, "REFUSED")
        return ("REFUSED: piping a download straight into a shell. Fetch it, "
                "let him read it, then run it.")
    if command_needs_confirmation(cmd, ag.cfg):
        ag.step("waiting for his OK to run: %s" % cmd)
        if not ag.confirm("Run this command?", cmd):
            ag.tool_card("run", cmd, "declined")
            return "He declined. Do not retry it. Ask what he wants instead."
    ag.step("running: %s" % cmd)
    rc, out = run_shell(cmd, timeout=90)
    ag.tool_card("run", cmd, "exit %d" % rc)
    out = out or "(no output)"
    if len(out) > 4000:
        out = out[:4000] + "\n[...truncated]"
    return "exit=%d\n%s" % (rc, out)


def tool_launch(args: Dict[str, Any], ag: "Agent") -> str:
    app = str(args.get("app", "")).strip()
    ag.step("launching %s" % app)
    res = launch_app(app)
    ag.tool_card("launch", app, res)
    return res


def tool_media(args: Dict[str, Any], ag: "Agent") -> str:
    action = str(args.get("action", "")).strip()
    ag.step("media: %s" % action)
    return media_control(action)


def tool_clipboard(args: Dict[str, Any], ag: "Agent") -> str:
    mode = str(args.get("mode", "read")).lower()
    if mode == "write":
        return clipboard_write(str(args.get("text", "")))
    return "clipboard:\n" + clipboard_read()[:3000]


def tool_screenshot(args: Dict[str, Any], ag: "Agent") -> str:
    ag.step("grabbing the screen")
    ok, res = take_screenshot()
    if not ok:
        return res
    ag.show_image(res)
    return "screenshot saved to %s and shown in chat" % res


def tool_read_file(args: Dict[str, Any], ag: "Agent") -> str:
    path = os.path.expanduser(str(args.get("path", "")).strip())
    if not path:
        return "no path given"
    if not inside_sandbox(path, ag.cfg):
        return "REFUSED: %s is outside the sandbox root" % path
    if not os.path.isfile(path):
        return "no such file: %s" % path
    if os.path.getsize(path) > 2000000:
        return "file is too big to read into context (%d bytes)" % \
            os.path.getsize(path)
    ag.step("reading %s" % os.path.basename(path))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(20000)
    except OSError as exc:
        return "read failed: %s" % exc
    return "FILE %s\n%s" % (path, data)


def tool_list_dir(args: Dict[str, Any], ag: "Agent") -> str:
    path = os.path.expanduser(str(args.get("path", "") or HOME))
    if not inside_sandbox(path, ag.cfg):
        return "REFUSED: %s is outside the sandbox root" % path
    if not os.path.isdir(path):
        return "not a directory: %s" % path
    ag.step("listing %s" % path)
    try:
        names = sorted(os.listdir(path))[:200]
    except OSError as exc:
        return "list failed: %s" % exc
    rows = []
    for n in names:
        full = os.path.join(path, n)
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        rows.append("%s%s  %d" % (n, "/" if os.path.isdir(full) else "", size))
    return "DIR %s (%d entries)\n%s" % (path, len(names), "\n".join(rows))


def tool_note(args: Dict[str, Any], ag: "Agent") -> str:
    text = str(args.get("text", "")).strip()
    if not text:
        return "nothing to note"
    _ensure_dirs()
    try:
        with open(NOTES_PATH, "a", encoding="utf-8") as fh:
            fh.write("- [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M"), text))
    except OSError as exc:
        return "note failed: %s" % exc
    ag.tool_card("note", text[:60], "saved")
    return "noted in %s" % NOTES_PATH


def tool_remember(args: Dict[str, Any], ag: "Agent") -> str:
    key = str(args.get("key", "")).strip()
    val = str(args.get("value", "")).strip()
    if not key or not val:
        return "need both key and value"
    ag.memory.remember(key, val)
    ag.tool_card("remember", key, val[:60])
    return "stored: %s = %s" % (key, val)


def tool_recall(args: Dict[str, Any], ag: "Agent") -> str:
    hits = ag.memory.recall(str(args.get("query", "")))
    if not hits:
        return "nothing stored about that"
    return "\n".join("%s: %s" % (k, v) for k, v in hits.items())


def tool_forget(args: Dict[str, Any], ag: "Agent") -> str:
    key = str(args.get("key", "")).strip()
    return "forgotten: %s" % key if ag.memory.forget(key) else "no such key"


def tool_calc(args: Dict[str, Any], ag: "Agent") -> str:
    return safe_calc(str(args.get("expression", "")))


def tool_timer(args: Dict[str, Any], ag: "Agent") -> str:
    try:
        secs = int(float(args.get("seconds", 0)))
    except (TypeError, ValueError):
        return "seconds must be a number"
    if secs <= 0 or secs > 86400 * 2:
        return "pick something between 1 second and 48 hours"
    label = str(args.get("label", "") or "timer")

    def fire() -> None:
        notify("%s: %s" % (APP_NAME, label), "time's up")
        ag.tts.speak("%s. time's up." % label)
        ag.step("timer finished: %s" % label)

    t = threading.Timer(secs, fire)
    t.daemon = True
    t.start()
    ag.tool_card("timer", label, "%ds" % secs)
    return "timer set for %d seconds (%s)" % (secs, label)


def tool_say(args: Dict[str, Any], ag: "Agent") -> str:
    text = str(args.get("text", "")).strip()
    if text:
        ag.tts.speak(text)
        ag.step("said: %s" % text[:80])
    return "spoken"


# ---- 2.0 tools ------------------------------------------------------

def tool_write_file(args: Dict[str, Any], ag: "Agent") -> str:
    path = str(args.get("path", "")).strip()
    text = str(args.get("text", ""))
    append = bool(args.get("append"))
    if not path:
        return "no path given"
    full = os.path.abspath(os.path.expanduser(path))
    if not inside_sandbox(full, ag.cfg):
        ag.tool_card("write_file", full, "REFUSED")
        return "REFUSED: %s is outside the sandbox root" % full
    if not ag.cfg.get("allow_writes"):
        exists = os.path.exists(full)
        body = "%s %s\n\n%d characters%s" % (
            "Append to" if append else "Write", full, len(text),
            "\n\nThis overwrites what is there now (a .bak is kept)."
            if exists and not append else "")
        ag.step("waiting for his OK to write %s" % full)
        if not ag.confirm("Write to this file?", body):
            ag.tool_card("write_file", full, "declined")
            return "He declined the write. Do not retry it."
    res = write_text_file(full, text, ag.cfg, append=append)
    ag.tool_card("write_file", full, "ok" if "wrote" in res or "appended"
                 in res else "failed")
    return res


def tool_find(args: Dict[str, Any], ag: "Agent") -> str:
    pattern = str(args.get("pattern", "") or args.get("name", "")).strip()
    root = str(args.get("path", "") or HOME)
    ag.step("searching %s for %s" % (root, pattern))
    ok, res = find_files(root, pattern, ag.cfg)
    ag.tool_card("find", pattern, "%d hits" % len(res.split("\n"))
                 if ok else "failed")
    return res


def tool_processes(args: Dict[str, Any], ag: "Agent") -> str:
    sort_by = str(args.get("sort", "") or args.get("by", "") or "cpu")
    ag.step("checking what is running")
    out = list_processes(sort_by, int(args.get("limit") or 12))
    ag.tool_card("processes", "by %s" % sort_by, "ok")
    return out


def tool_network(args: Dict[str, Any], ag: "Agent") -> str:
    ag.step("reading the network")
    info = network_status()
    ag.tool_card("network", "", "ok" if "error" not in info else "failed")
    return "\n".join("%s: %s" % (k, v) for k, v in info.items())


def tool_disk(args: Dict[str, Any], ag: "Agent") -> str:
    ag.step("checking disks")
    out = disk_report()
    ag.tool_card("disk", "", "ok")
    return out


def tool_volume(args: Dict[str, Any], ag: "Agent") -> str:
    action = str(args.get("action", "") or args.get("mode", "") or "get")
    ag.step("volume: %s" % action)
    res = volume_control(action, int(args.get("level") or 5))
    ag.tool_card("volume", action, res[:40])
    return res


def tool_power(args: Dict[str, Any], ag: "Agent") -> str:
    action = str(args.get("action", "")).strip().lower()
    if action not in POWER_ACTIONS:
        return "power actions: %s" % ", ".join(sorted(POWER_ACTIONS))
    # No config switch turns this off.  Locking his session or pulling the
    # power out from under his work is always a question, never a decision.
    ag.step("waiting for his OK to %s" % action)
    if not ag.confirm("%s the machine?" % action.title(),
                      "George wants to %s this session now." % action):
        ag.tool_card("power", action, "declined")
        return "He declined. Do not retry it."
    ag.tool_card("power", action, "running")
    return power_action(action)


def tool_see(args: Dict[str, Any], ag: "Agent") -> str:
    """Grab the screen and hand it to a local vision model."""
    from george_vision import DESCRIBE, Eyes
    question = str(args.get("question", "") or args.get("prompt", "")).strip()
    eyes = Eyes(ag.cfg)
    if not eyes.available():
        ag.tool_card("see", "", "no vision model")
        return ("No vision model is pulled. Tell him to open Settings > Eyes "
                "and pull one, or run: ollama pull moondream")
    ag.step("taking a look at his screen")
    ok, shot = take_screenshot()
    if not ok:
        ag.tool_card("see", "", "no screenshot")
        return "could not grab the screen: %s" % shot
    prompt = DESCRIBE if not question else (
        "Answer this about the screen in one or two sentences: " + question)
    answer = eyes.look(shot, prompt)
    ag.show_image(shot)
    ag.tool_card("see", question[:60] or "what is on screen",
                 "ok" if not answer.startswith("cannot see") else "failed")
    return answer


def tool_open_path(args: Dict[str, Any], ag: "Agent") -> str:
    path = str(args.get("path", "") or args.get("file", "")).strip()
    ag.step("opening %s" % path)
    res = open_path(path, ag.cfg)
    ag.tool_card("open_path", path, "ok" if res.startswith("opened")
                 else "failed")
    return res


TOOLS: Dict[str, Callable[[Dict[str, Any], "Agent"], str]] = {
    "web_search": tool_web_search,
    "open_page": tool_open_page,
    "news": tool_news,
    "show": tool_show,
    "weather": tool_weather,
    "system": tool_system,
    "run": tool_run,
    "launch": tool_launch,
    "media": tool_media,
    "clipboard": tool_clipboard,
    "screenshot": tool_screenshot,
    "read_file": tool_read_file,
    "list_dir": tool_list_dir,
    "note": tool_note,
    "remember": tool_remember,
    "recall": tool_recall,
    "forget": tool_forget,
    "calc": tool_calc,
    "timer": tool_timer,
    "say": tool_say,
    "write_file": tool_write_file,
    "find": tool_find,
    "processes": tool_processes,
    "network": tool_network,
    "disk": tool_disk,
    "volume": tool_volume,
    "power": tool_power,
    "open_path": tool_open_path,
    "see": tool_see,
}

# names a 7B model reaches for by mistake -> what it actually meant
TOOL_ALIASES = {
    "search": "web_search", "google": "web_search", "browse": "open_page",
    "fetch": "open_page", "read": "open_page", "web": "open_page",
    "exec": "run", "shell": "run", "bash": "run", "command": "run",
    "terminal": "run", "open": "show", "open_url": "show",
    "show_on_screen": "show", "display": "show", "headlines": "news",
    "rss": "news", "speak": "say", "tts": "say", "final": "answer",
    "reply": "answer", "respond": "answer", "response": "answer",
    "message": "answer", "text": "answer", "sysinfo": "system",
    "status": "system", "screen": "screenshot", "note_add": "note",
    "memory": "remember", "store": "remember",
    "write": "write_file", "save_file": "write_file", "create_file":
        "write_file", "edit_file": "write_file", "append_file": "write_file",
    "search_files": "find", "locate": "find", "find_file": "find",
    "ps": "processes", "top": "processes", "task_manager": "processes",
    "net": "network", "ifconfig": "network", "wifi": "network",
    "df": "disk", "storage": "disk", "disk_usage": "disk",
    "sound": "volume", "audio": "volume", "set_volume": "volume",
    "lock": "power", "suspend": "power", "sleep": "power",
    "shutdown": "power", "reboot": "power", "logout": "power",
    "open_file": "open_path", "open_folder": "open_path", "xdg_open":
        "open_path",
    "look": "see", "look_at_screen": "see", "vision": "see",
    "view_screen": "see", "watch": "see", "eyes": "see",
}


# =====================================================================
# ACTION PARSING
#
# deepseek-r1 is a reasoner, not a function-calling model.  It will
# fence the JSON, prepend a sentence, emit two objects, or name the tool
# something adjacent.  Parse forgivingly -- but never invent a call.
# =====================================================================

def _balanced_json_spans(text: str) -> List[str]:
    spans: List[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start:i + 1])
                    start = -1
    return spans


def _canon_tool(name: str) -> str:
    n = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    n = TOOL_ALIASES.get(n, n)
    return n


def extract_actions(raw: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Return [(tool, args)] found in a model reply, in order."""
    text = strip_reasoning(raw)
    if not text:
        return []
    candidates: List[str] = []
    for m in re.finditer(r"```(?:json|tool_code|python)?\s*(.*?)```", text,
                         re.S | re.I):
        candidates.extend(_balanced_json_spans(m.group(1)))
    candidates.extend(_balanced_json_spans(text))

    out: List[Tuple[str, Dict[str, Any]]] = []
    seen: set = set()
    for blob in candidates:
        if blob in seen:
            continue
        seen.add(blob)
        try:
            obj = json.loads(blob)
        except ValueError:
            try:                       # single quotes / python dict style
                obj = ast.literal_eval(blob)
            except Exception:
                continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool") or obj.get("action") or obj.get("name") or \
            obj.get("function") or obj.get("tool_name")
        if isinstance(name, dict):
            name = name.get("name")
        if not name:
            continue
        tool = _canon_tool(name)
        args = obj.get("args") or obj.get("arguments") or obj.get("parameters")
        if not isinstance(args, dict):
            args = {k: v for k, v in obj.items()
                    if k not in ("tool", "action", "name", "function",
                                 "tool_name", "args", "arguments",
                                 "parameters")}
        # legacy Basilisk-prototype shapes
        if tool == "run" and "command" not in args and "cmd" in args:
            args["command"] = args.pop("cmd")
        if tool in ("open_page", "show") and "url" not in args and \
                "link" in args:
            args["url"] = args.pop("link")
        if tool == "answer" and "text" not in args:
            for k in ("content", "message", "answer", "reply", "response"):
                if k in args:
                    args["text"] = args[k]
                    break
        if tool == "answer" or tool in TOOLS:
            out.append((tool, args))
    return out


def strip_action_json(text: str) -> str:
    """Prose the model wrote around its JSON, for the transcript."""
    s = strip_reasoning(text)
    s = re.sub(r"```(?:json|tool_code|python)?\s*.*?```", "", s, flags=re.S | re.I)
    for span in _balanced_json_spans(s):
        if '"tool"' in span or '"action"' in span or '"name"' in span:
            s = s.replace(span, "")
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# =====================================================================
# AGENT
# =====================================================================

class Agent:
    """Owns the conversation and the tool loop.  Runs on a worker thread;
    every UI touch goes back through GLib.idle_add in the callbacks."""

    def __init__(self, cfg: Dict[str, Any], memory: MemoryStore,
                 tts: TextToSpeech) -> None:
        self.cfg = cfg
        self.memory = memory
        self.tts = tts
        self.ollama = Ollama(cfg)
        self.history: List[Dict[str, str]] = []
        self.stop_event = threading.Event()
        self.busy = False
        self.confirm_elapsed = 0.0

        # UI hooks, wired by the window
        self.on_step: Callable[[str], None] = lambda s: None
        self.on_token: Callable[[str], None] = lambda s: None
        self.on_tool: Callable[[str, str, str], None] = lambda a, b, c: None
        self.on_final: Callable[[str], None] = lambda s: None
        self.on_error: Callable[[str], None] = lambda s: None
        self.on_news: Callable[[List[Dict[str, str]]], None] = lambda i: None
        self.on_weather: Callable[[Dict[str, str]], None] = lambda w: None
        self.on_vitals: Callable[[Dict[str, str]], None] = lambda v: None
        self.on_image: Callable[[str], None] = lambda p: None
        self.on_done: Callable[[], None] = lambda: None
        self.ask_confirm: Callable[[str, str], bool] = lambda t, b: False

    # ---- hooks used by tools ----------------------------------------
    def step(self, text: str) -> None:
        log("step: %s" % text)
        self.on_step(text)

    def tool_card(self, name: str, arg: str, result: str) -> None:
        self.on_tool(name, arg, result)

    def show_news(self, items: List[Dict[str, str]]) -> None:
        self.on_news(items)

    def show_weather(self, w: Dict[str, str]) -> None:
        self.on_weather(w)

    def show_vitals(self, v: Dict[str, str]) -> None:
        self.on_vitals(v)

    def show_image(self, path: str) -> None:
        self.on_image(path)

    def confirm(self, title: str, body: str) -> bool:
        """Blocks on him.  The time he spends deciding is not counted
        against the tool watchdog -- a man reading a dialog is not a
        wedged tool."""
        started = time.time()
        try:
            return self.ask_confirm(title, body)
        finally:
            self.confirm_elapsed += time.time() - started

    # ---- prompt ------------------------------------------------------
    def system_message(self) -> str:
        st = system_status()
        name = (self.cfg.get("user_name") or "").strip()
        extra_bits = ["CONTEXT",
                      "Now: %s" % time.strftime("%A %d %B %Y, %H:%M %Z"),
                      "This machine: %s" % machine_summary(),
                      "Hostname %s, up %s" % (st.get("host", "?"),
                                              st.get("uptime", "?"))]
        if st.get("battery"):
            extra_bits.append("Battery: %s" % st["battery"])
        if self.cfg.get("location"):
            extra_bits.append("He is in %s." % self.cfg["location"])
        mem = self.memory.as_prompt_block()
        if mem:
            extra_bits.append("What you know about him:\n" + mem)
        if not self.cfg.get("auto_run_commands"):
            extra_bits.append("Shell commands need his confirmation; a "
                              "declined command is a no, not a retry.")
        if not self.cfg.get("allow_writes"):
            extra_bits.append("File writes need his confirmation too.")
        persona = PERSONAS.get(str(self.cfg.get("persona", "jarvis")),
                               PERSONAS["jarvis"])
        return SYSTEM_PROMPT.format(distro=st.get("distro", "this machine"),
                                    name=name or "his",
                                    persona=persona,
                                    tools=TOOL_SPEC,
                                    extra="\n".join(extra_bits))

    def messages(self) -> List[Dict[str, str]]:
        msgs = [{"role": "system", "content": self.system_message()}]
        budget = 24
        msgs.extend(self.history[-budget:])
        return msgs

    def reset(self) -> None:
        self.history = []

    # ---- tool execution ----------------------------------------------
    def call_tool(self, tool: str, args: Dict[str, Any]) -> str:
        """Run one tool with a deadline.

        A tool that blocks forever -- a dead socket, an NFS mount that
        went away, a subprocess that will not return -- used to take the
        whole turn with it and leave the stop button as the only way
        out.  Now the loop gets an observation back either way.  The
        orphan thread is left to finish and die on its own; killing a
        thread mid-syscall is worse than leaking one.
        """
        fn = TOOLS.get(tool)
        if not fn:
            return ("unknown tool %r. Valid tools: %s"
                    % (tool, ", ".join(sorted(TOOLS))))
        box: Dict[str, str] = {}
        self.confirm_elapsed = 0.0

        def work() -> None:
            try:
                box["result"] = fn(args, self)
            except Exception as exc:
                log("tool %s crashed: %s" % (tool, exc))
                box["result"] = "%s failed: %s" % (tool, exc)

        worker = threading.Thread(target=work, daemon=True,
                                  name="george-tool-%s" % tool)
        worker.start()
        limit = float(self.cfg.get("tool_timeout", 150) or 150)
        started = time.time()
        while worker.is_alive():
            worker.join(0.4)
            if self.stop_event.is_set():
                return "%s was stopped before it finished." % tool
            if time.time() - started > limit + self.confirm_elapsed:
                log("tool %s hit the %ds watchdog" % (tool, limit))
                return ("%s did not come back within %ds and was abandoned. "
                        "Tell him that, and try another way."
                        % (tool, int(limit)))
        return box.get("result", "%s returned nothing" % tool)

    # ---- the loop ----------------------------------------------------
    def run_turn(self, user_text: str) -> None:
        self.busy = True
        self.stop_event.clear()
        self.history.append({"role": "user", "content": user_text})
        last_calls: List[str] = []
        try:
            if not self.ollama.alive():
                raise OllamaError(
                    "ollama is not answering on %s. start it with "
                    "`ollama serve`." % self.ollama.base)

            model, note = self.ollama.resolve_model()
            if note:
                self.on_error(note)

            def stalled(waited: float) -> None:
                self.on_step("still thinking - %ds with no output"
                             % int(waited))

            recent_observations: List[str] = []
            for step_no in range(int(self.cfg.get("max_steps", 14))):
                if self.stop_event.is_set():
                    self.on_step("stopped")
                    break

                self.on_step("thinking (step %d)" % (step_no + 1))
                log("agent: calling chat_stream with %d history items" % len(self.history))
                reply = self.ollama.chat_stream(self.messages(), self.on_token,
                                                self.stop_event,
                                                on_stall=stalled, model=model)
                log("agent: chat_stream returned %d chars" % len(reply))
                if self.stop_event.is_set():
                    self.on_step("stopped")
                    break
                if not reply.strip():
                    self.on_error("the model returned nothing")
                    break

                self.history.append({"role": "assistant", "content": reply})
                log("agent: assistant reply (trunc): %s" % reply[:300].replace('\n',' '))
                actions = extract_actions(reply)
                log("agent: extracted %d actions" % len(actions))

                if not actions:
                    prose = strip_action_json(reply)
                    log("agent: final prose -> %s" % (prose or "(no reply)"))
                    self.on_final(prose or "(no reply)")
                    self.tts.speak(prose)
                    break

                final_text = None
                observations: List[str] = []
                for tool, args in actions[:3]:
                    if self.stop_event.is_set():
                        break
                    if tool == "answer":
                        final_text = str(args.get("text", "")).strip()
                        log("agent: got an answer action -> %s" % final_text[:120])
                        break

                    sig = tool + json.dumps(args, sort_keys=True)[:200]
                    log("agent: tool call candidate %s args=%s" % (tool, json.dumps(args, sort_keys=True)))
                    if last_calls.count(sig) >= 2:
                        # Avoid telling the user in the transcript that the
                        # model duplicated itself; add a terse observation
                        # instead so the assistant can proceed without
                        # producing a canned confirmation phrase.
                        observations.append("OBSERVATION (%s): (duplicate call ignored)" % tool)
                        log("agent: dedupe skipped tool %s" % tool)
                        continue
                    last_calls.append(sig)

                    result = self.call_tool(tool, args)
                    if len(result) > 6000:
                        # A 7B model on an 8k window drowns in a huge
                        # observation and starts ignoring the question.
                        result = result[:6000] + "\n[... trimmed]"
                    observations.append("OBSERVATION (%s):\n%s" % (tool, result))
                    log("agent: tool %s returned %d chars" % (tool, len(result)))

                if final_text is not None:
                    # If the model's final answer is just a short canned
                    # confirmation (e.g. "Done."), replace it with a more
                    # informative summary drawn from the last observation so
                    # the user hears something meaningful.
                    if final_text.strip().lower() in ("done", "done.", "ok", "ok.", "already on screen", "already on screen."):
                        # prefer recent observations from prior steps if present
                        source_obs = observations or recent_observations
                        if source_obs:
                            last_obs = source_obs[-1]
                            body = last_obs.split('\n', 1)[1] if '\n' in last_obs else last_obs
                            summary = body.strip().split('\n')[0][:400]
                            log("agent: replacing short final '%s' with observation summary" % final_text[:40])
                            final_text = summary
                    self.on_final(final_text)
                    self.tts.speak(final_text)
                    break

                if observations:
                    # keep a short-lived copy of observations across steps so
                    # the model's final answer can be made more informative if
                    # it emits only a short confirmation later.
                    recent_observations.extend(observations)
                    # trim to avoid unbounded growth
                    if len(recent_observations) > 20:
                        recent_observations = recent_observations[-20:]
                    self.history.append({"role": "user",
                                         "content": "\n\n".join(observations)})
            else:
                self.on_error("hit the %d step ceiling without an answer"
                              % int(self.cfg.get("max_steps", 14)))
        except OllamaError as exc:
            self.on_error(str(exc))
        except Exception as exc:                       # pragma: no cover
            log("turn crashed: %s" % exc)
            self.on_error("something broke: %s" % exc)
        finally:
            self.busy = False
            self.on_done()

    def start(self, user_text: str) -> None:
        threading.Thread(target=self.run_turn, args=(user_text,),
                         daemon=True, name="george-turn").start()

    def stop(self) -> None:
        self.stop_event.set()
        self.tts.stop()


