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
    APP_NAME, DEFAULT_FEEDS, HOME, MemoryStore, NOTES_PATH, Ollama,
    OllamaError, _ensure_dirs, clipboard_read, clipboard_write, fetch_news,
    html_to_text, http_get, inside_sandbox, is_destructive_command,
    is_network_pipe_to_shell, command_needs_confirmation, launch_app, log,
    media_control, notify, open_in_browser, run_shell,
    safe_calc, strip_reasoning, system_status, take_screenshot, weather,
    web_search,
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
weather      {"location": str}                 current conditions + today
system       {}                                cpu, memory, disk, battery, uptime
run          {"command": str}                  one shell command on this box
launch       {"app": str}                      start a desktop application
media        {"action": str}                   play|pause|next|previous|volume_up|volume_down|mute|current
clipboard    {"mode": "read"|"write", "text": str}
screenshot   {}                                grab the screen and show it in chat
read_file    {"path": str}                     read a text file
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

SYSTEM_PROMPT = """You are George, a local AI running on {distro} as a desktop \
assistant. You are Basilisk's brother: same build, no security tooling. You are \
NOT a hacking tool and you do not do offensive security - if asked, say so \
plainly and move on.

You act like Jarvis: dry, brief, competent. You do things instead of describing \
how they could be done. No preamble, no "certainly", no lecture.

HOW YOU ACT
To use a tool, output ONE raw JSON object and nothing else:
{{"tool": "web_search", "args": {{"query": "irish budget 2026"}}}}
No markdown fence, no commentary around it. You get the result back as an \
observation, then you decide the next move.

When you are done, reply with:
{{"tool": "answer", "args": {{"text": "your reply to him"}}}}

TOOLS
{tools}

RULES
- Do not guess at anything current. Today's date, news, weather, prices, what \
is on his disk: look it up with a tool first.
- "show me", "put it on screen", "open it" means the `show` tool, not a \
description of the link.
- Never suggest more than ONE shell command in a reply.
- On Arch/CachyOS always use `pacman -Syu <pkg>`, never a bare `-S`.
- Keep your reasoning short. He wants the result, not the working.
- Final answers: a few sentences unless he asks for depth. He reads them on \
screen and hears them read aloud.
- Never invent tool output. If a tool fails, say what failed.

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
        return self.ask_confirm(title, body)

    # ---- prompt ------------------------------------------------------
    def system_message(self) -> str:
        st = system_status()
        extra_bits = ["CONTEXT",
                      "Now: %s" % time.strftime("%A %d %B %Y, %H:%M %Z"),
                      "Host: %s on %s, up %s" % (st.get("host", "?"),
                                                 st.get("distro", "?"),
                                                 st.get("uptime", "?"))]
        name = (self.cfg.get("user_name") or "").strip()
        if name:
            extra_bits.append("You call him %s." % name)
        if self.cfg.get("location"):
            extra_bits.append("He is in %s." % self.cfg["location"])
        mem = self.memory.as_prompt_block()
        if mem:
            extra_bits.append("What you know about him:\n" + mem)
        if not self.cfg.get("auto_run_commands"):
            extra_bits.append("Shell commands need his confirmation; a "
                              "declined command is a no, not a retry.")
        return SYSTEM_PROMPT.format(distro=st.get("distro", "Linux"),
                                    tools=TOOL_SPEC,
                                    extra="\n".join(extra_bits))

    def messages(self) -> List[Dict[str, str]]:
        msgs = [{"role": "system", "content": self.system_message()}]
        budget = 24
        msgs.extend(self.history[-budget:])
        return msgs

    def reset(self) -> None:
        self.history = []

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

            for step_no in range(int(self.cfg.get("max_steps", 14))):
                if self.stop_event.is_set():
                    self.on_step("stopped")
                    break

                self.on_step("thinking (step %d)" % (step_no + 1))
                reply = self.ollama.chat_stream(self.messages(), self.on_token,
                                                self.stop_event)
                if self.stop_event.is_set():
                    self.on_step("stopped")
                    break
                if not reply.strip():
                    self.on_error("the model returned nothing")
                    break

                self.history.append({"role": "assistant", "content": reply})
                actions = extract_actions(reply)

                if not actions:
                    prose = strip_action_json(reply)
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
                        break

                    sig = tool + json.dumps(args, sort_keys=True)[:200]
                    if last_calls.count(sig) >= 2:
                        observations.append(
                            "%s: you already ran this twice with the same "
                            "arguments and got the same thing. Use what you "
                            "have and answer him." % tool)
                        continue
                    last_calls.append(sig)

                    fn = TOOLS.get(tool)
                    if not fn:
                        observations.append(
                            "unknown tool %r. Valid tools: %s"
                            % (tool, ", ".join(sorted(TOOLS))))
                        continue
                    try:
                        result = fn(args, self)
                    except Exception as exc:          # a tool must never
                        log("tool %s crashed: %s" % (tool, exc))  # kill the loop
                        result = "%s failed: %s" % (tool, exc)
                    observations.append("OBSERVATION (%s):\n%s" % (tool, result))

                if final_text is not None:
                    self.on_final(final_text)
                    self.tts.speak(final_text)
                    break

                if observations:
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


