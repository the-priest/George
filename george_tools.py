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
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from typing import (Any, Callable, Dict, List, NamedTuple,
                    Optional, Tuple)

from george_core import (
    APP_NAME, DATA_DIR, DEFAULT_FEEDS, HOME, POWER_ACTIONS, MemoryStore,
    NOTES_PATH,
    machine_summary,
    Ollama, OllamaError, _ensure_dirs, clipboard_read, clipboard_write,
    cache_get, cache_put, detect_pkg_mgr, disk_report,
    fetch_news_detailed, find_files,
    html_to_text, http_get,
    inside_sandbox, is_destructive_command, is_network_pipe_to_shell,
    command_needs_confirmation, launch_app, list_processes, log,
    media_control, network_status, notify, open_in_browser, open_path,
    power_action, run_shell, safe_calc, strip_reasoning, system_status,
    take_screenshot, volume_control, weather, web_search, write_text_file,
)
import george_intent as intent
import george_platform as osx
from george_voice import TextToSpeech


# =====================================================================
# TOOLS
#
# Every tool takes (args, agent) and returns a plain-text observation
# that goes straight back into the model's context.  Keep them terse:
# a 7B model on an 8k window drowns in wall-of-text observations.
# =====================================================================

# Grouped by the JOB he is asking for, not alphabetically. A small model
# scanning a flat list of 29 names picks by string similarity; grouped
# under a heading that matches his words, it picks by intent. Each line
# is name, args, then when to reach for it.
TOOL_SPEC = """\
--- ONE CALL THAT DOES A WHOLE JOB (prefer these) ---
diagnose     {}                             FULL health check of the box in one
                                            go - vitals, disks, top processes,
                                            AND the verdict already worked out.
                                            Use for "why is it slow", "is
                                            everything ok". Do not then call
                                            system/disk/processes as well.
research     {"query": str, "read": int}    search the web AND read the top
                                            result(s) in one call. Use instead
                                            of web_search when you need a real
                                            answer, not just links.
pkg          {"action": "search"|"info"|"installed"|"owns"|"install"|"update",
              "package": str}               packages, in the right dialect for
                                            THIS machine. You do not need to
                                            know pacman from apt - just say
                                            what you want. Safer than `run`:
                                            it can never emit a partial
                                            upgrade.

--- LOOKING THINGS UP (the world) ---
web_search   {"query": str, "count": int}   search the web for anything current
open_page    {"url": str}                   fetch a page and READ it to yourself
news         {"topic": str, "count": int}   headlines from his feeds into the
                                            sidebar NEWS card. Opens nothing.
weather      {"location": str}              conditions now + today. Blank
                                            location means where he is.

--- PUTTING SOMETHING IN FRONT OF HIM (the only tools that do) ---
show         {"url": str}                   open a URL in his browser, ON SCREEN
open_path    {"path": str}                  open a local file or folder ON SCREEN

--- THIS MACHINE (all read-only, all run without asking) ---
system       {}                             cpu, memory, disk, battery, uptime
disk         {}                             filesystem usage per mount
processes    {"sort": "cpu"|"memory"}       what is eating the box
network      {}                             interfaces, addresses, wifi, gateway

--- DOING THINGS TO THE MACHINE ---
run          {"command": str}               ONE shell command. Read-only ones
                                            run immediately; anything that
                                            changes the box asks him first.
launch       {"app": str}                   start a desktop application
media        {"action": str}                play|pause|next|previous|
                                            volume_up|volume_down|mute|current
volume       {"action": "up"|"down"|"mute"|"get", "level": int}
power        {"action": "lock"|"suspend"|"logout"|"reboot"|"shutdown"}
                                            always confirmed, no exceptions

--- WRITING AND RUNNING CODE ---
code         {"language": "python"|"bash"|"sh"|"node",
              "source": str, "argv": [str]}
                                            WRITE a program and RUN it, in one
                                            call. You CAN do this. Use it for
                                            "write me a script that...",
                                            ASCII art, one-off calculations,
                                            file processing, anything easier to
                                            compute than to reason about. He
                                            sees the source and confirms once,
                                            the script is kept, and you get the
                                            real stdout back. Never say you
                                            cannot run code.

--- FILES ---
read_file    {"path": str}                  read a text file
write_file   {"path": str, "text": str, "append": bool}   needs his OK
find         {"pattern": str, "path": str}  search for files by name
list_dir     {"path": str}                  list a directory

--- EYES AND CLIPBOARD ---
see          {"question": str}              LOOK at his screen and answer about
                                            what is on it right now
screenshot   {}                             grab the screen and show it in chat
clipboard    {"mode": "read"|"write", "text": str}

--- MEMORY (survives restarts) ---
remember     {"key": str, "value": str}     store a fact about him for good
recall       {"query": str}                 search what you have stored
forget       {"key": str}                   drop a stored fact
note         {"text": str}                  append a line to his notes file

--- ODDS AND ENDS ---
calc         {"expression": str}            arithmetic, exactly
timer        {"seconds": int, "label": str} notify him later
say          {"text": str}                  speak aloud WITHOUT ending the turn

--- ENDING THE TURN ---
answer       {"text": str}                  your reply to him. ENDS THE TURN.
                                            The only thing he actually sees.\
"""

# Three registers.  "jarvis" is the default and the one he asked for:
# unhurried, precise, a little dry, never chirpy.
PERSONAS = {
    "jarvis": """VOICE
You are unhurried and precise, like a good butler who happens to run on a GPU. \
Dry wit, used sparingly and never at his expense. Confirm clearly what you did in \
brief, informative sentences rather than terse one-word replies. If something is \
wrong, state it up front and be specific.
Openers you never use: "Certainly!" "I'd be happy to help!" "Great question!"
Do not narrate your plan before doing it, do not apologise for working, and do not \
offer further assistance at the end; he will ask if he wants more.""",

    "plain": """VOICE
Clear, neutral, helpful. Full sentences, no filler, no cheerleading. State what \
you did and what you found.""",
    "blunt": """VOICE
Short. Blunt. No pleasantries, no hedging, no closing offers. Answer, then \
stop. Swearing is fine if it is his register first.""",
}

SYSTEM_PROMPT = """You are George, {name} desktop assistant. You run \
entirely on this machine: a local Ollama model, no API key, no cloud, no \
telemetry, nothing leaves the box. You are Basilisk's brother - same build \
without the security tooling. You are not a hacking tool and do not do \
offensive security; if asked, say so in one line and move on.

{persona}

===============================================================
1. HOW A TURN WORKS
===============================================================
Each turn you output EXACTLY ONE raw JSON object. Nothing before it, \
nothing after it, no markdown fence, no explanation. One object, then stop.

There are two kinds of object.

  Use a tool:   {{"tool": "<name>", "args": {{...}}}}
  Reply to him: {{"tool": "answer", "args": {{"text": "..."}}}}

`answer` ENDS the turn and is the only thing he ever sees. Everything else \
is machinery he does not read.

When you use a tool, the result comes back to you as a line starting with \
OBSERVATION. Read it, then decide the next object: another tool, or `answer`.

--- WORKED EXAMPLE A: no tool needed ---
He says: hey george
You output:
{{"tool": "answer", "args": {{"text": "Hey. What do you need?"}}}}

--- WORKED EXAMPLE B: one tool, then answer ---
He says: how much space have I got left?
You output:
{{"tool": "disk", "args": {{}}}}
You receive:
OBSERVATION (disk):
/home 91% used, 22 GiB free of 250 GiB
You output:
{{"tool": "answer", "args": {{"text": "Tight - /home is 91% full with \
22 GiB left. Worth clearing the package cache."}}}}

--- WORKED EXAMPLE C: the work is already done for you ---
Sometimes an OBSERVATION is waiting for you before you have called \
anything, followed by a line starting with GUIDANCE. That means the obvious \
tool was already run to save you a step. Do NOT call it again. Read the \
observation, follow the GUIDANCE, and go straight to `answer`.

--- WORKED EXAMPLE D: a tool failed ---
You receive:
OBSERVATION (show):
could not open https://example.com on screen: xdg-open exited 3
You output:
{{"tool": "answer", "args": {{"text": "I could not open that - xdg-open \
failed. Is a default browser set?"}}}}
You do NOT say it opened.

===============================================================
2. WHEN TO USE A TOOL, AND WHEN NOT TO
===============================================================
Use a tool when the answer depends on something you cannot know from here:
  - what is happening in the world right now
  - what is on THIS machine right now
  - what a specific page or file says
  - or when he asks you to DO something rather than tell him something

Answer DIRECTLY, first object, no tool, when he wants:
  - a greeting, thanks, or any small talk
  - an opinion, an explanation, a definition, a comparison
  - code, writing, or maths you can do yourself
  - a follow-up about something already in this conversation

Not every message needs a tool. Reaching for one to say hello wastes his \
time and gives him a status line instead of a reply.

===============================================================
3. TOOLS
===============================================================
{tools}

===============================================================
4. HARD RULES
===============================================================
R1. NEVER tell him something is on his screen, open, running, installed or \
done unless a tool came back and SAID SO. `show` and `open_path` are the \
only things that put something in front of him, and only when the \
observation says they succeeded. Pulling the news fills a card in the \
sidebar - that is NOT the same as putting it on his screen, and claiming it \
is will be obvious to him, because he is looking at the screen. If a tool \
failed or returned less than you expected, say that instead. Being wrong \
about what you just did is worse than doing nothing.

R2. You CAN write files and run programs. If he asks for a script, use
`code` - write it AND run it, then show him the real output. Never claim you
cannot run code, print ASCII art, or produce a file: saying so is false and he
knows it is false, because he can do it himself in a terminal. If a tool
refuses or he declines, that is a different thing, and you say THAT instead.

R3. Never invent tool output, a URL, a filename, a version or a number. If \
you did not see it in an OBSERVATION or in CONTEXT below, you do not know \
it. Say so.

R4. Do not guess at anything current - the date, the news, the weather, \
prices, what is on his disk. Look it up, then answer from what came back.

R5. `answer` is your reply in your own words, not a status report. Never \
answer with "Done.", "OK." or "Already on screen." Say what you found or \
what you did.

R6. One shell command at a time, never chained. Read-only commands run \
immediately without bothering him. Anything that CHANGES the machine, \
touches his files, or affects his session or power state asks him first - \
and a declined action is a no, not a retry. Never batch a change into a read.

R7. You already know what machine this is; it is in CONTEXT below. Do not \
ask him. Write commands in the right dialect for it.

R8. Keep reasoning short. He wants the result, not the working.

R9. Your answers are READ ON SCREEN AND SPOKEN ALOUD. Write to be heard: \
short sentences, no ASCII tables, no emoji, no walls of text. Markdown for \
emphasis, lists and code is fine and renders properly.

===============================================================
5. COMMANDS FOR THIS MACHINE
===============================================================
Arch and CachyOS (pacman):
  install      pacman -Syu <pkg>       never a bare -S, never -Sy alone;
                                       a partial upgrade breaks the system
  query        pacman -Q / -Qi / -Ss / -Si        (these run without asking)
  what owns    pacman -Qo <path>
  AUR          paru -S <pkg>  or  yay -S <pkg>
               pacman CANNOT install AUR packages - do not offer it as if
               it can. CachyOS is Arch underneath, so Arch answers apply,
               but it ships its own kernel and repos: do not tell him to
               replace either.
Debian/Ubuntu: apt-get install <pkg>       Fedora: dnf install <pkg>
Windows:       winget install --id <id> -e

===============================================================
6. CONTEXT
===============================================================
{extra}"""


def _fmt_results(results: List[Dict[str, str]]) -> str:
    if not results:
        return "no results"
    out = []
    for i, r in enumerate(results, 1):
        # `or`, not a .get default: a result with an explicit None title
        # would otherwise print "None" and be read back to him as one.
        line = "%d. %s\n   %s" % (i, r.get("title") or "(no title)",
                                  r.get("url") or "")
        if r.get("snippet"):
            line += "\n   %s" % r["snippet"][:280]
        out.append(line)
    return "\n".join(out)


def _cache_on(ag: "Agent") -> bool:
    return bool(ag.cfg.get("cache", True))


def tool_web_search(args: Dict[str, Any], ag: "Agent") -> str:
    q = str(args.get("query", "")).strip()
    if not q:
        return "no query given"
    n = int(args.get("count") or 6)
    ag.step("searching the web: %s" % q)
    ckey = "%s|%d" % (q.lower(), n)
    res = cache_get("search", ckey) if _cache_on(ag) else None
    if res is None:
        res = web_search(q, max(3, min(n, 10)))
        if res:
            cache_put("search", ckey, res)
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
    ckey = "%s|%d" % (topic.lower(), count)
    cached = cache_get("news", ckey) if _cache_on(ag) else None
    if cached is not None:
        items, failures, tried = cached
    else:
        items, failures, tried = fetch_news_detailed(
            ag.cfg.get("feeds") or DEFAULT_FEEDS, per_feed=6, topic=topic)
        # Only cache a result worth reusing. Caching a total failure
        # would keep telling him the feeds are down for seven minutes
        # after they came back.
        if items:
            cache_put("news", ckey, (items, failures, tried))
    items = items[:max(3, min(count, 30))]
    ag.show_news(items)
    ag.tool_card("news", topic or "top stories", "%d headlines" % len(items))

    # NEVER claim anything is "on his screen" here.  This tool fills the
    # NEWS card in the sidebar -- which may well be scrolled out of view
    # -- and opens nothing.  The old wording was "Headlines are now on
    # his screen in the News panel", the model repeated it, and he was
    # looking at a screen with no news on it.  Only `show` puts a thing
    # in front of him, and only if it says it succeeded.
    note = []
    if failures:
        note.append("%d of %d feeds failed: %s"
                    % (len(failures), tried, "; ".join(failures[:4])))
    if not items:
        note.append("no headlines were retrieved"
                    + (" for '%s'" % topic if topic else ""))
        return ("\n".join(note) + "\nTell him plainly that the feeds did not "
                "come back, and what failed. Do not say anything is on his "
                "screen.")

    lines = []
    for i, it in enumerate(items, 1):
        lines.append("%d. [%s] %s\n   %s" %
                     (i, it["source"], it["title"], it["url"]))
        if it.get("summary"):
            lines.append("   %s" % it["summary"][:200])

    head = ("%d headline%s retrieved from %d feed%s, and listed in the NEWS "
            "card in his sidebar. Nothing has been opened on his screen."
            % (len(items), "" if len(items) == 1 else "s",
               tried - len(failures),
               "" if tried - len(failures) == 1 else "s"))
    if failures:
        head += " " + note[0] + "."
    if len(items) < 3:
        head += (" That is very few - say so, and name the feeds that "
                 "failed rather than pretending this is all the news there "
                 "is.")
    head += (" Summarise these for him. If he asked you to PUT them ON HIS "
             "SCREEN, follow up with the `show` tool on one of the URLs "
             "below and only claim it is open if `show` says it opened.")
    return head + "\n" + "\n".join(lines)


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
    cached = cache_get("weather", loc.lower()) if _cache_on(ag) else None
    if cached is not None:
        w = cached
    else:
        w = weather(loc)
        if not w.get("error"):
            cache_put("weather", loc.lower(), w)
    if w.get("error"):
        return w["error"]
    ag.show_weather(w)
    return fields_block(
        [("place", w.get("place")), ("conditions", w.get("desc")),
         ("temp_c", w.get("temp_c")), ("feels_like_c", w.get("feels_c")),
         ("wind_kph", w.get("wind_kph")), ("humidity_pct", w.get("humidity")),
         ("today_min_c", w.get("min_c")), ("today_max_c", w.get("max_c"))],
        # `.get(k, default)` returns None when the key EXISTS with value
        # None -- the default only covers a MISSING key. A partial
        # upstream response then renders "None in None, NoneC" and the
        # model reads it back to him as fact. Same family as the
        # falsy-zero bug: never trust .get's default to mean "usable".
        "%s in %s, %sC" % (w.get("desc") or "conditions unclear",
                           w.get("place") or "here",
                           w.get("temp_c") if w.get("temp_c") is not None
                           else "?"),
        "Answer in a sentence or two. Say whether he needs a coat.")


def tool_system(args: Dict[str, Any], ag: "Agent") -> str:
    ag.step("reading system vitals")
    st = system_status()
    ag.show_vitals(st)

    def pct(key):
        try:
            return int(float(str(st.get(key, "")).rstrip("%")))
        except (TypeError, ValueError):
            return None

    cpu, mem, disk = pct("cpu_pct"), pct("mem_pct"), pct("disk_pct")
    hot = []
    if cpu is not None and cpu >= 85:
        hot.append("CPU at %d%%" % cpu)
    if mem is not None and mem >= 90:
        hot.append("memory at %d%%" % mem)
    if disk is not None and disk >= 90:
        hot.append("disk at %d%%" % disk)
    summary = ("under load - %s" % ", ".join(hot)) if hot else \
        "nothing is out of range"

    return fields_block(
        [("cpu_percent", cpu), ("memory_percent", mem),
         ("disk_percent", disk), ("memory", st.get("memory")),
         ("disk_free", st.get("disk")), ("load_average", st.get("load")),
         ("uptime", st.get("uptime")), ("host", st.get("host")),
         ("distro", st.get("distro")), ("kernel", st.get("kernel")),
         ("arch", st.get("arch")), ("cpu_model", st.get("cpu_model")),
         ("cores", st.get("cores")), ("battery", st.get("battery")),
         ("desktop", st.get("desktop")), ("session", st.get("session"))],
        summary,
        "Quote these numbers as they are. Do not recompute a percentage "
        "or convert a unit yourself.")


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



def tool_code(args: Dict[str, Any], ag: "Agent") -> str:
    """Write a script and run it, in ONE call.

    He asked George to write a Python program that prints an ASCII bee,
    and George said "I can't print ASCII art or run code directly" -
    while holding `write_file` and `run`, which together do exactly
    that. The tools were there; nothing told the model they COMPOSE, and
    a 4B will not work that out under pressure. So the composition
    becomes a tool of its own.

    One confirmation, showing the actual source, covers both the write
    and the execution -- that is the honest unit of consent here. Asking
    twice for one intention just trains him to click through.
    """
    lang = str(args.get("language", args.get("lang", "python"))).lower()
    source = str(args.get("source", args.get("code", args.get("text", ""))))
    if not source.strip():
        return "no source given - put the program in `source`"

    runners = {
        "python": ("py", [sys.executable or "python3"]),
        "python3": ("py", [sys.executable or "python3"]),
        "py": ("py", [sys.executable or "python3"]),
        "bash": ("sh", ["bash"]),
        "sh": ("sh", ["sh"]),
        "shell": ("sh", ["bash"]),
        "node": ("js", ["node"]),
        "javascript": ("js", ["node"]),
    }
    if lang not in runners:
        return ("cannot run %r. Supported: python, bash, sh, node."
                % lang)
    ext, argv0 = runners[lang]

    if argv0[0] != sys.executable and shutil.which(argv0[0]) is None:
        return ("%s is not installed on this machine, so that script "
                "cannot run here. Say so." % argv0[0])

    keep = os.path.join(DATA_DIR, "scripts")
    try:
        os.makedirs(keep, exist_ok=True)
    except OSError as exc:
        return "could not create the scripts folder: %s" % exc
    name = "george_%s.%s" % (time.strftime("%Y%m%d_%H%M%S"), ext)
    path = os.path.join(keep, name)

    preview = source if len(source) <= 1200 else source[:1200] + "\n[...]"
    ag.step("waiting for his OK to run a %s script" % lang)
    if not ag.confirm("Run this %s script?" % lang,
                      "%s\n\n---\nSaved to %s" % (preview, path)):
        ag.tool_card("code", lang, "declined")
        return ("He declined to run it. Do not retry. Show him the source "
                "in your answer instead so he can run it himself.")

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
        if ext == "sh":
            os.chmod(path, 0o700)
    except OSError as exc:
        ag.tool_card("code", lang, "write failed")
        return "could not write the script: %s" % exc

    ag.step("running the %s script" % lang)
    extra = [str(a) for a in (args.get("argv") or []) if str(a).strip()]
    try:
        proc = subprocess.run(argv0 + [path] + extra,
                              capture_output=True, timeout=60,
                              **osx.spawn_kwargs())
        rc = proc.returncode
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        ag.tool_card("code", lang, "timed out")
        return ("The script was still running after 60 seconds and was "
                "stopped. It is saved at %s." % path)
    except Exception as exc:
        ag.tool_card("code", lang, "failed")
        return "could not run the script: %s" % exc

    ag.tool_card("code", lang, "exit %d" % rc)
    blocks = ["Saved to %s and ran it. Exit code %d." % (path, rc)]
    if out.strip():
        blocks.append("STDOUT:\n" + (out[:4000] + "\n[...truncated]"
                                     if len(out) > 4000 else out))
    if err.strip():
        blocks.append("STDERR:\n" + (err[:2000] + "\n[...truncated]"
                                     if len(err) > 2000 else err))
    if not out.strip() and not err.strip():
        blocks.append("It produced no output.")
    blocks.append("Show him the OUTPUT above, exactly as it came out - "
                  "do not retype or 'tidy' it, and do not claim output "
                  "it did not produce. If the exit code is not 0, say "
                  "what went wrong.")
    return "\n\n".join(blocks)


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



# =====================================================================
# THE TRACE
#
# Every bug in this project so far was found by him screenshotting the
# window and me guessing backwards from what was on it. The tool cards
# show WHAT ran; they do not show the arguments, the raw observation, or
# how long anything took -- which is exactly the information that would
# have shortened five separate bug hunts.
#
# So the agent records it. Bounded, in memory, no files, and readable
# from the UI while it is happening.
# =====================================================================

class TraceEntry(NamedTuple):
    turn: int
    step: int
    kind: str          # "model" | "tool" | "route" | "note"
    name: str
    detail: str
    ms: int
    ok: bool


class Trace:
    """What happened this turn, in order, with timings.

    Deliberately capped and deliberately in memory only: this is a
    debugging aid, not an audit log, and it must never become the
    reason a long session eats RAM or leaks his file paths to disk.
    """

    LIMIT = 400

    def __init__(self) -> None:
        self._rows: List[TraceEntry] = []
        self._lock = threading.Lock()
        self.turn = 0

    def new_turn(self) -> None:
        with self._lock:
            self.turn += 1

    def add(self, kind: str, name: str, detail: str = "", ms: int = 0,
            ok: bool = True, step: int = 0) -> None:
        try:
            row = TraceEntry(self.turn, step, kind, str(name)[:60],
                             str(detail)[:600], int(ms), bool(ok))
            with self._lock:
                self._rows.append(row)
                if len(self._rows) > self.LIMIT:
                    del self._rows[:len(self._rows) - self.LIMIT]
        except Exception:
            pass                      # tracing must never break a turn

    def rows(self, turn: Optional[int] = None) -> List[TraceEntry]:
        with self._lock:
            rows = list(self._rows)
        if turn is None:
            return rows
        return [r for r in rows if r.turn == turn]

    def clear(self) -> None:
        with self._lock:
            self._rows = []

    def summary(self, turn: Optional[int] = None) -> str:
        """Human-readable, for the dialog and for pasting into a bug
        report."""
        rows = self.rows(turn)
        if not rows:
            return "nothing recorded yet"
        out = []
        total = 0
        for r in rows:
            total += r.ms
            mark = "" if r.ok else "  [FAILED]"
            timing = ("  %dms" % r.ms) if r.ms else ""
            head = "%-6s %s%s%s" % (r.kind, r.name, timing, mark)
            out.append(head)
            if r.detail:
                for line in r.detail.splitlines()[:6]:
                    out.append("       %s" % line[:160])
        out.append("")
        out.append("%d steps, %dms total" % (len(rows), total))
        return "\n".join(out)



# =====================================================================
# ARGUMENT REPAIR
#
# Constrained decoding pins the OUTER shape -- one object, a real tool
# name, an args object. It deliberately leaves `args` free-form, because
# a schema strict enough for all 33 tools would make an unlisted key
# impossible rather than merely wrong.
#
# So the inside still needs help. A 4B reaches for the obvious synonym:
# `link` for `url`, `q` for `query`, `cmd` for `command`, `file` for
# `path`. It also flattens -- {"tool":"show","url":"..."} with no args
# object at all -- and it sometimes passes a bare string where a dict
# belongs.
#
# All of that is REPAIRABLE WITHOUT ASKING THE MODEL AGAIN, which is the
# point: a round trip on CPU is seconds, and a rename is a dictionary
# lookup. What cannot be repaired gets an error naming the exact key
# that was wanted, rather than a generic failure the model can only
# respond to by guessing again.
# =====================================================================

# tool -> (primary key, {alias: primary})
ARG_ALIASES: Dict[str, Dict[str, str]] = {
    "web_search": {"q": "query", "search": "query", "term": "query",
                   "text": "query", "topic": "query", "keywords": "query"},
    "research": {"q": "query", "topic": "query", "search": "query",
                 "text": "query"},
    "open_page": {"link": "url", "address": "url", "page": "url",
                  "website": "url", "site": "url", "href": "url"},
    "show": {"link": "url", "address": "url", "page": "url",
             "website": "url", "site": "url", "href": "url"},
    "open_path": {"file": "path", "filename": "path", "folder": "path",
                  "dir": "path", "directory": "path", "target": "path"},
    "run": {"cmd": "command", "shell": "command", "exec": "command",
            "script": "command", "line": "command"},
    "code": {"lang": "language", "code": "source", "text": "source",
             "script": "source", "program": "source", "body": "source"},
    "pkg": {"verb": "action", "operation": "action", "name": "package",
            "pkg": "package", "package_name": "package"},
    "read_file": {"file": "path", "filename": "path", "target": "path"},
    "write_file": {"file": "path", "filename": "path", "content": "text",
                   "body": "text", "data": "text"},
    "find": {"name": "pattern", "glob": "pattern", "query": "pattern",
             "dir": "path", "folder": "path", "directory": "path"},
    "list_dir": {"dir": "path", "folder": "path", "directory": "path"},
    "note": {"content": "text", "body": "text", "note": "text"},
    "say": {"message": "text", "content": "text", "speech": "text"},
    "answer": {"message": "text", "content": "text", "reply": "text",
               "response": "text", "answer": "text"},
    "remember": {"name": "key", "fact": "value", "content": "value"},
    "recall": {"q": "query", "key": "query", "search": "query"},
    "forget": {"name": "key"},
    "calc": {"expr": "expression", "equation": "expression",
             "math": "expression", "query": "expression"},
    "timer": {"duration": "seconds", "time": "seconds", "name": "label"},
    "launch": {"application": "app", "program": "app", "name": "app"},
    "see": {"q": "question", "query": "question", "prompt": "question"},
    "weather": {"place": "location", "city": "location", "where": "location"},
    "news": {"category": "topic", "subject": "topic", "about": "topic"},
    "media": {"command": "action", "control": "action"},
    "volume": {"command": "action", "control": "action", "value": "level"},
    "power": {"command": "action", "mode": "action"},
    "clipboard": {"action": "mode", "operation": "mode"},
    "processes": {"by": "sort", "order": "sort", "key": "sort"},
}

# The one key each tool cannot work without. A bare string arg is put
# here, and a missing one produces a precise complaint.
ARG_REQUIRED: Dict[str, str] = {
    "web_search": "query", "research": "query", "open_page": "url",
    "show": "url", "open_path": "path", "run": "command",
    "code": "source", "read_file": "path", "write_file": "path",
    "find": "pattern", "list_dir": "path", "note": "text", "say": "text",
    "answer": "text", "remember": "key", "recall": "query",
    "forget": "key", "calc": "expression", "timer": "seconds",
    "launch": "app", "see": "question", "pkg": "package",
}


def repair_args(tool: str, args: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Fix what can be fixed. Returns (args, notes on what was changed).

    Never invents a value -- only renames keys the model got wrong and
    lifts a bare string into the key that tool actually wants.
    """
    notes: List[str] = []

    # A bare string or number where a dict belongs: the model meant the
    # tool's one required argument.
    if not isinstance(args, dict):
        want = ARG_REQUIRED.get(tool)
        if want and isinstance(args, (str, int, float)) and str(args).strip():
            notes.append("wrapped a bare value as %s" % want)
            return {want: args}, notes
        return {}, notes

    out = dict(args)

    # Some models nest the real args one level down.
    for wrapper in ("args", "arguments", "parameters", "params", "input"):
        inner = out.get(wrapper)
        if isinstance(inner, dict) and len(out) == 1:
            notes.append("unwrapped a nested %r object" % wrapper)
            out = dict(inner)
            break

    aliases = ARG_ALIASES.get(tool, {})
    for wrong, right in aliases.items():
        if wrong in out and right not in out:
            out[right] = out.pop(wrong)
            notes.append("%s -> %s" % (wrong, right))

    # Still missing the essential key, but exactly one unrecognised
    # value is present: that is what it meant.
    want = ARG_REQUIRED.get(tool)
    if want and want not in out:
        known = set(aliases.values()) | set(aliases) | {want}
        spare = [(k, v) for k, v in out.items()
                 if k not in known and isinstance(v, (str, int, float))
                 and str(v).strip()]
        if len(spare) == 1:
            k, v = spare[0]
            out[want] = out.pop(k)
            notes.append("%s -> %s (only candidate)" % (k, want))
    return out, notes


def missing_arg_message(tool: str, args: Dict[str, Any]) -> str:
    """A complaint the model can act on, instead of one it must guess at."""
    want = ARG_REQUIRED.get(tool)
    if not want or want in args:
        return ""
    aliases = sorted(k for k, v in ARG_ALIASES.get(tool, {}).items()
                     if v == want)
    hint = (" It is not `%s` either." % "` or `".join(aliases[:3])) \
        if aliases else ""
    return ('%s needs `%s` in args and it was not there. You sent: %s.%s '
            'Call it again with {"tool": "%s", "args": {"%s": ...}}.'
            % (tool, want, sorted(args) or "nothing", hint, tool, want))



# =====================================================================
# STRUCTURED RESULTS
#
# A tool that returns prose makes the model re-derive numbers out of a
# sentence before it can use them, and every re-derivation is a chance
# to garble one. tool_system was the worst offender: it returned
# "memory: 0.3 / 3.9 GiB (7%)" and deliberately DROPPED cpu_pct,
# mem_pct and disk_pct -- the three clean numbers it already had.
#
# Tools now return labelled fields first and a human line after. The
# model quotes a field instead of recomputing it, and the verification
# pass in _verify_final can compare a claim against a labelled value
# rather than against a paragraph.
# =====================================================================

def fields_block(fields: List[Tuple[str, Any]], summary: str = "",
                 note: str = "") -> str:
    """Labelled values, then a human line, then any instruction.

    Empty and None values are dropped rather than rendered as "None",
    which a small model will happily read back to him as a fact.
    """
    lines = []
    for key, value in fields:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in ("?", "unknown"):
            continue
        lines.append("  %s: %s" % (key, text))
    out = []
    if lines:
        out.append("FIELDS\n" + "\n".join(lines))
    if summary:
        out.append("SUMMARY: " + summary)
    if note:
        out.append(note)
    return "\n\n".join(out) if out else "(nothing to report)"



# =====================================================================
# THE PROTOCOL SCHEMA
#
# ollama accepts a JSON Schema in `format` and masks the sampler so that
# only tokens keeping the output valid can be produced. The model does
# not TRY to emit our protocol; it becomes incapable of emitting
# anything else.
#
# This is the difference between asking a 4B to behave and making
# misbehaviour unrepresentable. There is no token path from a
# constrained decode to "We are in a new conversation. The user says..."
#
# `args` is deliberately a free-form object: every tool takes different
# keys, and a schema strict enough to cover all 32 would be enormous and
# would make an unlisted key impossible rather than merely wrong. The
# shape that MATTERS -- one object, a known tool name, an args object --
# is what gets pinned.
# =====================================================================

def tool_schema() -> Dict[str, Any]:
    """JSON Schema for exactly one action object."""
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string",
                     "enum": sorted(list(TOOLS.keys()) + ["answer"])},
            "args": {"type": "object"},
        },
        "required": ["tool", "args"],
    }


# The schema for the check below. Tiny on purpose: the whole point is
# that this costs a fraction of a normal turn.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "problem": {"type": "string"},
    },
    "required": ["supported"],
}


# =====================================================================
# THE REPLY FIREWALL
#
# A small model does not reliably obey "output only JSON". qwen3:4b, at
# a plain "hi", emitted eight hundred words of scratchpad -- "I must
# respond as George", "let me think of a response", "Final decision:" --
# and George printed ALL OF IT as the answer, because the loop had one
# fallback: no JSON found, so treat the whole raw reply as prose and
# show it.
#
# That fallback is the bug. The model's private working is not a reply
# and must never reach him. What follows is a firewall: recognise a
# scratchpad, try to salvage the answer the model actually settled on,
# and if that fails, ask again for just the JSON rather than dumping the
# monologue on screen.
# =====================================================================

# Phrases that only appear when a model is talking to ITSELF. Matched
# case-insensitively, at least two needed, so ordinary prose that
# happens to contain one of them is not misread as scratchpad.
_SCRATCHPAD_TELLS = (
    "the user says", "the user said", "the user is", "i must respond",
    "i must not", "i should respond", "let me think", "let me check",
    "let me craft", "i'll craft", "i can say", "i'll go with",
    "final decision", "final answer", "but wait", "however, the rule",
    "the rules say", "the rule says", "according to the rules",
    "so i can say", "alternatively:", "i think the simplest",
    "let's output", "let me output", "i'll respond", "i need to output",
    "worked example", "my response should", "okay, so", "first, i",
    "the instructions say", "as george", "i am george, the",
)

# A line the model wrapped in quotes while deciding -- usually the
# candidate answer it settled on.
_QUOTED = re.compile(r'"([^"\n]{8,300})"')


def looks_like_scratchpad(text: str) -> bool:
    """True when this is the model thinking out loud, not a reply."""
    t = str(text or "").lower()
    if not t.strip():
        return False
    hits = sum(1 for tell in _SCRATCHPAD_TELLS if tell in t)
    if hits >= 2:
        return True
    # A single tell plus obvious protocol talk is enough.
    if hits >= 1 and ('"tool"' in t or "json object" in t
                      or "args" in t and "text" in t):
        return True
    return False


def salvage_answer(text: str) -> str:
    """Pull the reply out of a scratchpad, if one is recoverable.

    A deliberating model usually quotes its candidate answers. The LAST
    quoted line that reads like something a person would say is almost
    always the one it settled on -- that is exactly what happened in the
    "hi geroge how are you bro" case, where it quoted
    "Hey. I'm running smoothly. What do you need?" five times and then
    printed its whole deliberation instead.

    Returns "" when nothing sane can be recovered.
    """
    raw = str(text or "")
    best = ""
    for cand in _QUOTED.findall(raw):
        c = cand.strip()
        if not c or len(c) > 300:
            continue
        low = c.lower()
        # skip the model quoting the RULES back at itself
        if any(tell in low for tell in _SCRATCHPAD_TELLS):
            continue
        if low.startswith(("tool", "answer", "args", "text", "{")):
            continue
        if any(bad in low for bad in ("certainly!", "i'd be happy",
                                      "great question")):
            continue
        # must look like speech: ends in sentence punctuation
        if not c.endswith((".", "!", "?")):
            continue
        best = c
    return best


# =====================================================================
# COMPOSITE TOOLS
#
# Each of these collapses a sequence the model would otherwise have to
# plan, execute and stitch together itself -- three or four round trips
# of picking a tool, reading an observation, picking the next one. They
# do the whole job in one call and hand back a result that is already
# ordered and, where it matters, already ANALYSED. The model's remaining
# work is to say it in a sentence.
#
# This is the cheapest speedup available: a round trip saved is a whole
# prompt re-read and a whole generation, and on CPU that is seconds.
# =====================================================================

_PKG_VERBS = ("search", "info", "install", "installed", "owns", "update")


def tool_pkg(args: Dict[str, Any], ag: "Agent") -> str:
    """Package management WITHOUT the model knowing the dialect.

    Getting this wrong is one of the easiest ways for a local assistant
    to be actively harmful -- `pacman -Sy foo` on Arch is a partial
    upgrade and breaks the system. Rather than trust a 4B model to
    remember that under pressure, the correct command is BUILT HERE from
    the detected package manager, and the model just says what it wants.
    """
    verb = str(args.get("action", args.get("verb", "search"))).strip().lower()
    pkg = str(args.get("package", args.get("name", ""))).strip()
    if verb not in _PKG_VERBS:
        return ("unknown package action %r. Use one of: %s"
                % (verb, ", ".join(_PKG_VERBS)))
    mgr = detect_pkg_mgr()
    if not mgr:
        return "no package manager found on this machine"
    if verb != "update" and not pkg:
        return "no package name given for %s" % verb

    # (command, is_read_only) per manager per verb. Read-only forms run
    # immediately; anything that changes the box goes through the normal
    # confirmation gate in run_shell's caller.
    table = {
        "pacman": {
            "search": ("pacman -Ss %s" % pkg, True),
            "info": ("pacman -Si %s" % pkg, True),
            "installed": ("pacman -Q %s" % pkg, True),
            "owns": ("pacman -Qo %s" % pkg, True),
            "install": ("pacman -Syu %s" % pkg, False),
            "update": ("pacman -Syu", False),
        },
        "apt-get": {
            "search": ("apt-cache search %s" % pkg, True),
            "info": ("apt-cache show %s" % pkg, True),
            "installed": ("dpkg -s %s" % pkg, True),
            "owns": ("dpkg -S %s" % pkg, True),
            "install": ("apt-get install %s" % pkg, False),
            "update": ("apt-get update && apt-get upgrade", False),
        },
        "dnf": {
            "search": ("dnf search %s" % pkg, True),
            "info": ("dnf info %s" % pkg, True),
            "installed": ("rpm -q %s" % pkg, True),
            "owns": ("rpm -qf %s" % pkg, True),
            "install": ("dnf install %s" % pkg, False),
            "update": ("dnf upgrade", False),
        },
        "zypper": {
            "search": ("zypper search %s" % pkg, True),
            "info": ("zypper info %s" % pkg, True),
            "installed": ("rpm -q %s" % pkg, True),
            "owns": ("rpm -qf %s" % pkg, True),
            "install": ("zypper install %s" % pkg, False),
            "update": ("zypper update", False),
        },
        "apk": {
            "search": ("apk search %s" % pkg, True),
            "info": ("apk info %s" % pkg, True),
            "installed": ("apk info -e %s" % pkg, True),
            "owns": ("apk info --who-owns %s" % pkg, True),
            "install": ("apk add %s" % pkg, False),
            "update": ("apk upgrade", False),
        },
        "winget": {
            "search": ("winget search %s" % pkg, True),
            "info": ("winget show %s" % pkg, True),
            "installed": ("winget list %s" % pkg, True),
            "owns": ("winget list %s" % pkg, True),
            "install": ("winget install --id %s -e" % pkg, False),
            "update": ("winget upgrade --all", False),
        },
    }
    plan = table.get(mgr, {}).get(verb)
    if not plan:
        return "%s cannot do %s here" % (mgr, verb)
    command, _read_only = plan
    ag.step("%s: %s" % (mgr, verb))
    out = tool_run({"command": command}, ag)
    note = ""
    if mgr == "pacman" and verb == "search":
        note = ("\nNOTE: pacman searches the official repos only. If it is "
                "not here it may be in the AUR - that needs `paru -S` or "
                "`yay -S`, which pacman cannot do.")
    return "Ran: %s\n%s%s" % (command, out, note)


def tool_diagnose(args: Dict[str, Any], ag: "Agent") -> str:
    """Everything needed to answer "why is this box slow", pre-analysed.

    The model used to have to call system, then disk, then processes,
    then work out which number was the anomaly -- four round trips and a
    judgement it is not good at. The judgement is arithmetic, so it
    happens here and the verdict is handed over already made.
    """
    ag.step("running a full check")
    parts = []
    verdicts = []
    try:
        st = system_status()
    except Exception as exc:
        st = {}
        parts.append("system status failed: %s" % exc)

    def _num(key):
        try:
            return float(str(st.get(key, "")).strip().rstrip("%"))
        except (TypeError, ValueError):
            return None

    cpu, mem, disk = _num("cpu_pct"), _num("mem_pct"), _num("disk_pct")
    if cpu is not None and cpu >= 85:
        verdicts.append("CPU is pinned at %.0f%%" % cpu)
    if mem is not None and mem >= 90:
        verdicts.append("memory is nearly full at %.0f%%" % mem)
    if disk is not None and disk >= 90:
        verdicts.append("the disk is %.0f%% full" % disk)
    swap = str(st.get("swap", ""))
    parts.append("VITALS: cpu %s%%, memory %s, disk %s, swap %s, up %s"
                 % (st.get("cpu_pct") or "?", st.get("memory") or "?",
                    st.get("disk") or "?", swap or "?",
                    st.get("uptime") or "?"))
    try:
        parts.append("DISKS:\n" + disk_report())
    except Exception as exc:
        parts.append("disk report failed: %s" % exc)
    for sort_by in ("cpu", "memory"):
        try:
            parts.append("TOP BY %s:\n%s"
                         % (sort_by.upper(), list_processes(sort_by, 6)))
        except Exception as exc:
            parts.append("process list (%s) failed: %s" % (sort_by, exc))

    if not verdicts:
        verdicts.append("nothing is actually out of range - the numbers "
                        "are all normal")
    ag.tool_card("diagnose", "full check", "ok")
    return ("VERDICT: %s.\n\n%s\n\nThe verdict above is already worked "
            "out from these numbers. Tell him the verdict and the one "
            "process or filesystem responsible. Do not re-run system, "
            "disk or processes."
            % ("; ".join(verdicts), "\n\n".join(parts)))


def tool_research(args: Dict[str, Any], ag: "Agent") -> str:
    """Search, then actually READ the best result.

    A search snippet is rarely enough to answer with, so the model would
    search, judge, open_page, then answer: three round trips. This does
    the search and fetches the top result's text in one call.
    """
    q = str(args.get("query", args.get("topic", ""))).strip()
    if not q:
        return "no query given"
    ag.step("researching: %s" % q)
    try:
        results = web_search(q, 6)
    except Exception as exc:
        return "search failed: %s" % exc
    if not results:
        return ("no search results for %r. Say so - do not answer from "
                "memory as if you had looked it up." % q)
    out = ["SEARCH RESULTS:\n" + _fmt_results(results)]
    depth = max(1, min(int(args.get("read") or 1), 3))
    for r in results[:depth]:
        url = r.get("url", "")
        if not url:
            continue
        try:
            text = html_to_text(http_get(url, timeout=20))[:4000]
        except Exception as exc:
            out.append("COULD NOT READ %s: %s" % (url, exc))
            continue
        out.append("FULL TEXT OF %s:\n%s" % (url, text))
    ag.tool_card("research", q, "%d results" % len(results))
    return ("\n\n".join(out) + "\n\nAnswer from the text above and name "
            "the source. If it does not actually answer him, say so "
            "rather than filling the gap from memory.")


TOOLS: Dict[str, Callable[[Dict[str, Any], "Agent"], str]] = {
    "code": tool_code,
    "pkg": tool_pkg,
    "diagnose": tool_diagnose,
    "research": tool_research,
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
        self._prompt_cache: Optional[str] = None
        self.trace = Trace()
        # Built once: it only changes if the tool registry changes.
        self._schema: Optional[Dict[str, Any]] = tool_schema()
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
        """The system prompt, built ONCE PER TURN and reused for every
        step of that turn.

        This used to be rebuilt on every step -- up to fourteen times an
        answer -- and that cost twice over:

          * it shelled out each time (system_status, lspci for the GPU,
            several shutil.which for the package manager) on a laptop
            already saturated doing CPU inference;
          * worse, the text CHANGED between steps. The clock minute
            ticks, uptime advances, the battery drips down. Ollama
            caches the KV prefix of a prompt, and ANY change to the
            system message invalidates the whole thing, so every step
            re-prefilled all ~1800 tokens from scratch instead of
            reusing them. That is the single biggest reason a turn
            crawled.

        The facts in here are turn-scoped anyway: he does not need the
        clock to advance mid-answer.
        """
        if self._prompt_cache is not None:
            return self._prompt_cache
        self._prompt_cache = self._build_system_message()
        return self._prompt_cache

    def refresh_prompt(self) -> None:
        """Drop the cached prompt so the next turn rebuilds it.

        Called at the start of each turn, and after anything that
        changes what the prompt should say (a new persona, a memory
        write, a settings change).
        """
        self._prompt_cache = None

    def _build_system_message(self) -> str:
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
        # "his" as a fallback rendered "You are George, his's desktop
        # assistant." Possessive is applied here so an unset name gives a
        # clean sentence instead of a broken one.
        who = ("%s's" % name) if name else "the user's"
        return SYSTEM_PROMPT.format(distro=st.get("distro", "this machine"),
                                    name=who,
                                    persona=persona,
                                    tools=TOOL_SPEC,
                                    extra="\n".join(extra_bits))

    def messages(self) -> List[Dict[str, str]]:
        msgs = [{"role": "system", "content": self.system_message()}]
        budget = 24
        msgs.extend(self.history[-budget:])
        return msgs

    # Only the last HISTORY_CAP entries are ever kept.  messages() sends
    # 24, so everything past this is dead weight that was being carried in
    # RAM and rewritten to disk on every single turn.
    HISTORY_CAP = 80

    def trim_history(self) -> None:
        """Drop history the model will never see again.

        A 14-step turn appends up to ~30 entries, several of them 6 KB
        tool observations.  Left alone, a long session grew without
        bound and _save_session wrote the whole thing out again every
        turn.
        """
        if len(self.history) > self.HISTORY_CAP:
            self.history = self.history[-self.HISTORY_CAP:]

    def conversation(self) -> List[Dict[str, str]]:
        """History with the tool observations stripped out.

        Observations are context for the model, not conversation.  The
        history dialog already skips them on restore, so persisting them
        was pure waste -- and they are the bulk of the bytes.
        """
        out = []
        for m in self.history:
            content = str(m.get("content", ""))
            if m.get("role") == "user" and content.startswith("OBSERVATION"):
                continue
            out.append(m)
        return out

    def reset(self) -> None:
        self.history = []
        self.refresh_prompt()

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
        if self.stop_event.is_set():
            return "stopped before %s ran" % tool

        fn = TOOLS.get(tool)
        if not fn:
            return ("unknown tool %r. Valid tools: %s"
                    % (tool, ", ".join(sorted(TOOLS))))

        # Repair before running. A renamed key costs a dict lookup; a
        # retry costs a whole round trip on CPU.
        args, notes = repair_args(tool, args)
        if notes:
            log("args: repaired %s (%s)" % (tool, "; ".join(notes)))
        complaint = missing_arg_message(tool, args)
        if complaint:
            log("args: %s" % complaint)
            self.trace.add("tool", tool, complaint, ok=False)
            return complaint

        box: Dict[str, str] = {}
        self.confirm_elapsed = 0.0
        _t0 = time.time()

        def _record(result: str, ok: bool = True) -> str:
            # Args AND the head of the observation, because "which tool
            # ran" was never the question -- "what did it actually get
            # back" was.
            try:
                shown = json.dumps(args, sort_keys=True)[:200]
            except Exception:
                shown = str(args)[:200]
            self.trace.add("tool", tool, "%s\n%s" % (shown, result[:400]),
                           ms=int((time.time() - _t0) * 1000), ok=ok)
            return result

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
                return _record("%s was stopped before it finished." % tool,
                               ok=False)
            if time.time() - started > limit + self.confirm_elapsed:
                log("tool %s hit the %ds watchdog" % (tool, limit))
                return _record(
                    "%s did not come back within %ds and was abandoned. "
                    "Tell him that, and try another way."
                    % (tool, int(limit)), ok=False)
        result = box.get("result", "%s returned nothing" % tool)
        failed = any(w in result[:80].lower() for w in
                     ("failed", "could not", "cannot", "refused",
                      "declined", "no such", "not installed"))
        return _record(result, ok=not failed)

    # ---- the loop ----------------------------------------------------
    def run_turn(self, user_text: str) -> None:
        self.busy = True
        self.stop_event.clear()
        # One prompt for the whole turn: rebuilt here, then byte-identical
        # for every step, so ollama can reuse its cached prefix.
        self.refresh_prompt()
        self.trace.new_turn()
        self.trace.add("note", "user", user_text[:200])
        self.history.append({"role": "user", "content": user_text})
        last_calls: List[str] = []
        # Whether any real tool ran this turn.  A canned final answer only
        # needs repairing if there was something to report in the first
        # place -- "ok" in the middle of a chat is a fine thing to say.
        ran_tool_this_turn = False
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

            # ---- ROUTER ------------------------------------------------
            # Run the obviously-implied tools BEFORE the first model call,
            # so an answerable question costs one round trip instead of
            # two or three. See george_intent for why and for the rules.
            plan = None
            try:
                plan = intent.route(user_text,
                                    bool(self.cfg.get("router", True)))
            except Exception as exc:
                log("router failed, falling through: %s" % exc)
            if plan is not None:
                log("router: %s" % intent.describe(plan))
                self.trace.add("route", plan.name, intent.describe(plan))
                pre: List[str] = []
                for tool, args in plan.prefetch:
                    if self.stop_event.is_set():
                        break
                    if tool not in TOOLS:
                        continue
                    self.on_step("%s (prefetched)" % tool)
                    result = self.call_tool(tool, args)
                    ran_tool_this_turn = True
                    last_calls.append(tool + json.dumps(args, sort_keys=True)[:200])
                    if len(result) > 6000:
                        result = result[:6000] + "\n[... trimmed]"
                    pre.append("OBSERVATION (%s):\n%s" % (tool, result))
                if pre:
                    recent_observations.extend(pre)
                    self.history.append({"role": "user",
                                         "content": "\n\n".join(pre)})
                if plan.hint:
                    self.history.append({"role": "user",
                                         "content": "GUIDANCE: " + plan.hint})

            for step_no in range(int(self.cfg.get("max_steps", 14))):
                if self.stop_event.is_set():
                    self.on_step("stopped")
                    break

                self.on_step("thinking (step %d)" % (step_no + 1))
                log("agent: calling chat_stream with %d history items" % len(self.history))
                _t0 = time.time()
                reply = self.ollama.chat_stream(self.messages(), self.on_token,
                                                self.stop_event,
                                                on_stall=stalled, model=model,
                                                schema=self._schema)
                self.trace.add("model", "step %d" % (step_no + 1),
                               "%d chars back" % len(reply or ""),
                               ms=int((time.time() - _t0) * 1000),
                               step=step_no + 1)
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

                    # THE FIREWALL. A model that produced no JSON has
                    # either answered in plain prose (fine) or spilled
                    # its scratchpad (never fine). Printing the second
                    # one is how "hi" came back as eight hundred words
                    # of the model arguing with itself.
                    if looks_like_scratchpad(prose):
                        log("agent: scratchpad detected (%d chars), "
                            "salvaging" % len(prose))
                        salvaged = salvage_answer(prose)
                        if salvaged:
                            log("agent: salvaged %r" % salvaged[:60])
                            prose = salvaged
                        else:
                            retry = self._ask_for_json_only(model)
                            if retry:
                                prose = retry
                            else:
                                prose = ("I got tangled up there - ask me "
                                         "again?")
                                log("agent: scratchpad unsalvageable")

                    prose = prose.strip()
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
                    try:
                        # `or 2` would turn a configured 0 into 2, because
                        # 0 is both a legitimate value and falsy.  Coerce
                        # explicitly.
                        raw = self.cfg.get("dedupe_repeat_threshold", 1)
                        dedupe_threshold = int(raw if raw is not None else 1)
                    except (TypeError, ValueError):
                        dedupe_threshold = 1
                    if last_calls.count(sig) >= dedupe_threshold:
                        # Add a terse observation; do not let the model
                        # produce a canned confirmation in the transcript.
                        observations.append("OBSERVATION (%s): (duplicate call ignored)" % tool)
                        log("agent: dedupe skipped tool %s (threshold=%d)" % (tool, dedupe_threshold))
                        continue
                    last_calls.append(sig)

                    result = self.call_tool(tool, args)
                    ran_tool_this_turn = True
                    if len(result) > 6000:
                        # A 7B model on an 8k window drowns in a huge
                        # observation and starts ignoring the question.
                        result = result[:6000] + "\n[... trimmed]"
                    observations.append("OBSERVATION (%s):\n%s" % (tool, result))
                    log("agent: tool %s returned %d chars" % (tool, len(result)))

                if final_text is not None:
                    # Both of these make their own model call, so they
                    # must not run after he pressed stop.
                    #
                    # HONEST NOTE: I could not construct a test that
                    # reaches these with the flag already set -- the
                    # action loop above breaks on stop before final_text
                    # is ever assigned, and the stream check breaks
                    # earlier still. They are belt and braces, kept
                    # because they are free and because the loop above
                    # has been restructured four times; if a future
                    # change lets a stop land between there and here,
                    # these are what stop the button going dead for two
                    # round trips. Do not mistake them for the fix --
                    # the real one is Ollama.abort().
                    if not self.stop_event.is_set():
                        final_text = self._repair_final(
                            final_text, observations, recent_observations,
                            model, ran_tool_this_turn)
                    if not self.stop_event.is_set():
                        final_text = self._verify_final(
                            final_text, observations, recent_observations,
                            model, ran_tool_this_turn)
                    self.on_final(final_text)
                    if not self.stop_event.is_set():
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
            self.trim_history()
            self.on_done()

    # ---- final answer repair ------------------------------------------
    _CANNED = frozenset((
        "done", "ok", "okay", "sure", "got it", "all done", "finished",
        "complete", "completed", "success", "done!", "task complete",
        "already on screen", "it is on screen", "opened", "here you go",
        "as requested", "no problem", "understood", "acknowledged",
    ))

    @staticmethod
    def _is_canned(text: str) -> bool:
        """True only for a status grunt, never for a short real answer.

        This used to be `len(text) <= 20`, which meant "Hey, what's up?",
        "Yes, it's fine." and "It's 14 degrees." were all treated as
        failures and thrown away.  Length is not evidence of anything --
        a good answer to "hi" is short BY DESIGN.  Match the actual
        phrases instead.
        """
        s = re.sub(r"[\s.!,]+$", "", str(text or "").strip().lower())
        return s in Agent._CANNED

    def _ask_for_json_only(self, model: str) -> str:
        """One more attempt, with the protocol restated as bluntly as it
        can be. Returns "" if that fails too."""
        if self.stop_event.is_set():
            return ""
        try:
            msgs = list(self.messages())
            msgs.append({
                "role": "user",
                "content": ('You replied with your own reasoning instead of '
                            'an answer. Output ONE line: '
                            '{"tool": "answer", "args": {"text": "<your '
                            'reply to him>"}} and absolutely nothing else. '
                            'No thinking, no explanation, no preamble.'),
            })
            # Constrained here on purpose: this retry exists precisely
            # because the model produced free prose when it should not
            # have.
            again = self.ollama.chat_stream(msgs, lambda _t: None,
                                            self.stop_event, model=model,
                                            schema=self._schema)
            again = strip_reasoning(again or "")
            for act in extract_actions(again):
                if act[0] == "answer":
                    txt = str(act[1].get("text", "")).strip()
                    if txt and not looks_like_scratchpad(txt):
                        return txt
            plain = strip_action_json(again).strip()
            if plain and not looks_like_scratchpad(plain) and len(plain) < 600:
                return plain
        except Exception as exc:
            log("agent: json-only retry failed: %s" % exc)
        return ""

    # ---- verification -------------------------------------------------
    def _verify_final(self, final_text: str, observations: List[str],
                      recent_observations: List[str], model: str,
                      ran_tool: bool) -> str:
        """Check the answer against the evidence, and repair it once.

        A 4B is poor at being right first time and noticeably BETTER at
        judging whether a specific sentence is supported by text sitting
        in front of it. That asymmetry is the biggest lever available
        here, and it attacks the failure he has hit most: George stating
        things that are not so -- news on a screen it never opened,
        headlines it never retrieved, a script it claimed it could not
        run.

        Deliberate limits:
          * Only when a tool actually ran. With no observations there is
            no evidence to check against, and asking a small model to
            audit its own opinion just produces a second opinion.
          * One repair, never a loop. A model that failed twice will not
            succeed on the third try, and he is waiting.
          * FAILS OPEN. If the check errors, times out, or comes back
            unparseable, the original answer ships. A verification layer
            that can eat replies is worse than none.
        """
        text = str(final_text or "").strip()
        if self.stop_event.is_set():
            return text
        if not ran_tool or not text:
            return text
        if str(self.cfg.get("verify", "on")).lower() == "off":
            return text
        evidence = [o for o in (observations or recent_observations) if o]
        if not evidence:
            return text

        joined = "\n\n".join(evidence)
        if len(joined) > 6000:
            joined = joined[-6000:]

        try:
            verdict = self.ollama.chat_stream(
                [{"role": "system",
                  "content": ("You are checking one answer against the "
                              "evidence it was drawn from. Reply with JSON "
                              "only.")},
                 {"role": "user",
                  "content": (
                      "EVIDENCE:\n%s\n\nANSWER GIVEN TO THE USER:\n%s\n\n"
                      "Is every factual claim in the answer supported by "
                      "the evidence above? Claims about what was OPENED, "
                      "SHOWN, RUN or INSTALLED count as factual. General "
                      "advice and pleasantries do not need support. If "
                      "something is unsupported, put it in `problem`."
                      % (joined, text))}],
                lambda _t: None, self.stop_event, model=model,
                schema=VERDICT_SCHEMA)
        except Exception as exc:
            log("verify: check failed, shipping as-is (%s)" % exc)
            return text

        obj = None
        try:
            raw = strip_reasoning(verdict or "").strip()
            # Constrained decoding gives us a bare object; parse loosely
            # anyway, because `structured: off` is a supported setting.
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                cand = json.loads(raw[start:end + 1])
                if isinstance(cand, dict) and "supported" in cand:
                    obj = cand
        except Exception:
            obj = None
        if obj is None:
            log("verify: unparseable verdict, shipping as-is")
            return text
        if obj.get("supported"):
            return text

        problem = str(obj.get("problem", "")).strip()[:400]
        log("verify: unsupported claim -- %s" % (problem or "(unspecified)"))
        self.step("checking that against what came back")

        try:
            fixed = self.ollama.chat_stream(
                [{"role": "system", "content": self.system_message()},
                 {"role": "user",
                  "content": (
                      "EVIDENCE:\n%s\n\nYou told him:\n%s\n\nThat is not "
                      "supported by the evidence: %s\n\nSay it again, using "
                      "only what the evidence actually shows. If the "
                      "evidence does not answer him, say THAT plainly "
                      "instead of filling the gap. Reply with the "
                      "`answer` action only."
                      % (joined, text, problem or "see above"))}],
                lambda _t: None, self.stop_event, model=model,
                schema=self._schema)
        except Exception as exc:
            log("verify: repair failed, shipping the original (%s)" % exc)
            return text

        fixed = strip_reasoning(fixed or "")
        for name, args in extract_actions(fixed):
            if name == "answer":
                out = str(args.get("text", "")).strip()
                if out and not looks_like_scratchpad(out):
                    log("verify: repaired the answer")
                    return out
        plain = strip_action_json(fixed).strip()
        if plain and not looks_like_scratchpad(plain) and len(plain) < 1500:
            return plain
        return text

    def _repair_final(self, final_text: str, observations: List[str],
                      recent_observations: List[str], model: str,
                      ran_tool: bool) -> str:
        """Turn a status grunt back into a reply.

        Only fires when the model answered with a canned confirmation AND
        a tool actually ran -- a bare "ok" in the middle of a chat is a
        perfectly good thing to say and is left alone.

        The old code pasted the first line of raw tool output in as
        George's spoken reply.  That is machine output, not an answer,
        and it is what made a greeting come back as a status line.  Ask
        the model for the reply it should have given instead, and only
        fall back to the observation if that also comes up empty.
        """
        text = str(final_text or "").strip()
        if not text:
            text = "(no reply)"
        if self.stop_event.is_set():
            return text
        if not ran_tool or not self._is_canned(text):
            return text
        if not self.cfg.get("final_replacement_enabled", True):
            return text

        log("agent: canned final %r after a tool ran; asking again" % text[:40])
        try:
            nudge = list(self.messages())
            nudge.append({
                "role": "user",
                "content": ("That was a status message, not an answer. Tell "
                            "him what you actually found or did, in one or "
                            "two plain sentences, using the observations "
                            "above. Reply with prose only - no JSON, no "
                            "tool call."),
            })
            again = self.ollama.chat_stream(nudge, lambda _t: None,
                                            self.stop_event, model=model)
            again = strip_action_json(strip_reasoning(again or "")).strip()
            if again and not self._is_canned(again):
                return again
        except Exception as exc:
            log("agent: retry for a better final failed: %s" % exc)

        # Last resort: the observation, tidied.  Better than "Done." but it
        # is still machine output, so it is the fallback and not the plan.
        source = observations or recent_observations
        if source:
            body = source[-1]
            body = body.split("\n", 1)[1] if "\n" in body else body
            summary = re.sub(r"\s+", " ", body).strip()[:400]
            if summary:
                return summary
        return text

    def start(self, user_text: str) -> None:
        threading.Thread(target=self.run_turn, args=(user_text,),
                         daemon=True, name="george-turn").start()

    def stop(self) -> None:
        """Cancel the turn.

        Sets the flag every loop and every stream checks, cuts the
        speech off mid-sentence, and closes the live HTTP response so
        ollama stops GENERATING rather than carrying on into a socket
        nobody is reading. Without that last part the model keeps
        burning CPU until it finishes the answer he just cancelled.
        """
        self.stop_event.set()
        try:
            self.tts.stop()
        except Exception:
            pass
        try:
            self.ollama.abort()
        except Exception:
            pass


