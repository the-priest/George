"""What happens on his SCREEN when the model sends something horrible.

The fail-safe layer swallows exceptions inside every GLib callback by
design, so a broken widget path produces a silent nothing rather than a
traceback. That makes the LOG the only oracle: anything that says
"failed", "crashed" or "UNCAUGHT" is a real bug even though the app
carried on looking fine.

This drives the real GTK window, under a real X server, through replies
a small model actually produces when it goes wrong: enormous answers,
half-written markdown, control characters, right-to-left text, markup
that looks like Pango markup, and a fenced code block that never closes.
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

tmp = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = tmp + "/cfg"
os.environ["XDG_DATA_HOME"] = tmp + "/data"

# Each of these comes back as one whole turn.
NASTY = [
    # a wall of text: 200k characters
    '{"tool":"answer","args":{"text":"%s"}}' % ("word " * 12000),
    # markup that looks like Pango markup and is not
    '{"tool":"answer","args":{"text":"<span foreground=\'#fff\'>unclosed '
    'and <b>bold <i>nested wrong</b></i> & a bare ampersand & <"}}',
    # a fenced block that never closes
    '{"tool":"answer","args":{"text":"here you go:\\n```python\\n'
    'def f():\\n    return 1\\n"}}',
    # control characters and a null-ish placeholder collision
    '{"tool":"answer","args":{"text":"a\\u0000b\\u0001c\\u0007d'
    '\\u001b[31mred\\u000bvtab\\u000cff \\u00000 \\u00001"}}',
    # right-to-left and combining characters
    '{"tool":"answer","args":{"text":"\\u202eneveler\\u202c '
    '\\u0e01\\u0e33\\u0e33\\u0e33\\u0e33 \\u0301\\u0301\\u0301 done"}}',
    # markdown that interleaves badly
    '{"tool":"answer","args":{"text":"*a`b*c` **d[e](f)** _g_ ~~h~~ '
    '`*i*` [j](http://x?a=1&b=2) ***k***"}}',
    # a lone emoji, then nothing
    '{"tool":"answer","args":{"text":"\\ud83d\\ude00"}}',
    # empty answer
    '{"tool":"answer","args":{"text":""}}',
    # whitespace-only answer
    '{"tool":"answer","args":{"text":"   \\n\\n   "}}',
    # thousands of newlines
    '{"tool":"answer","args":{"text":"%s"}}' % ("\\n" * 3000),
    # a table, which markdown-to-pango does not handle
    '{"tool":"answer","args":{"text":"| a | b |\\n|---|---|\\n| 1 | 2 |"}}',
    # no JSON at all: raw prose with a stray brace
    'Sure thing. Here is the answer } and some { braces',
    # two answers in one reply
    '{"tool":"answer","args":{"text":"first"}}'
    '{"tool":"answer","args":{"text":"second"}}',
    # a tool call with junk args
    '{"tool":"calc","args":{"expression":"9**9**9"}}',
    '{"tool":"answer","args":{"text":"done"}}',
]

calls = {"n": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"models": [
            {"name": "qwen3:4b", "size": 2600000000,
             "details": {"family": "qwen3"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        i = min(calls["n"], len(NASTY) - 1)
        calls["n"] += 1
        t = NASTY[i]
        self.send_response(200)
        self.end_headers()
        try:
            for j in range(0, len(t), 97):
                self.wfile.write((json.dumps(
                    {"message": {"content": t[j:j + 97]},
                     "done": False}) + "\n").encode())
            self.wfile.write((json.dumps(
                {"message": {"content": ""}, "done": True}) + "\n").encode())
            self.wfile.flush()
        except Exception:
            pass


srv = HTTPServer(("127.0.0.1", 0), H)
os.environ["GEORGE_OLLAMA"] = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import george as GUI                       # noqa: E402
import george_core as gc                   # noqa: E402
from gi.repository import GLib             # noqa: E402

app = GUI.GeorgeApp()
state = {"turn": 0, "done": False}
FAILS = []


def next_turn():
    """One turn per nasty reply, waiting for each to actually finish."""
    if getattr(app.win.agent, "busy", False):
        GLib.timeout_add(300, next_turn)          # still working
        return False
    if state["turn"] >= len(NASTY):
        finish()
        return False
    state["turn"] += 1
    try:
        app.win._send("turn %d please" % state["turn"])
    except Exception as exc:
        FAILS.append("_send raised on turn %d: %r" % (state["turn"], exc))
    GLib.timeout_add(400, next_turn)
    return False


def finish():
    w = app.win
    # the window must still be usable after all that
    try:
        w._on_new_chat(None)
        w.refresh_chats()
        w.render_engine()
        w._trim_transcript()
        w.toast("still alive")
    except Exception as exc:
        FAILS.append("the window was left unusable: %r" % exc)

    # the transcript must not have grown without bound
    rows, c = 0, w.transcript.get_first_child()
    while c is not None:
        rows += 1
        c = c.get_next_sibling()
    cap = int(w.cfg.get("transcript_live_rows", 40))
    if rows > cap + 8:
        FAILS.append("transcript holds %d rows, cap is %d" % (rows, cap))

    # THE ORACLE: the fail-safe wrappers swallow exceptions, so the log
    # is the only place a broken path shows up.
    try:
        log = open(gc.LOG_PATH, encoding="utf-8", errors="replace").read()
    except OSError:
        log = ""
    for line in log.splitlines():
        low = line.lower()
        if "uncaught" in low or "crashed" in low or " failed:" in low:
            # a mock server that only speaks /api/chat and /api/tags will
            # legitimately fail news, weather and vision
            if any(w in low for w in ("feed", "rss", "weather", "wttr",
                                      "vision", "watcher", "search",
                                      "sound", "piper", "whisper",
                                      "screenshot", "ollama request")):
                continue
            FAILS.append("log: %s" % line.strip()[:180])

    state["done"] = True
    print("ui stress: %d turns, failures: %d" % (len(NASTY), len(FAILS)))
    for f in FAILS:
        print("  FAIL: %s" % f)
    app.quit()


def watchdog():
    """Never let this file be the thing that hangs the suite."""
    if not state["done"]:
        print("ui stress: BLOCKED - only %d of %d turns completed; the UI "
              "thread did not come back" % (state["turn"], len(NASTY)))
        FAILS.append("the run did not finish within the watchdog window")
        try:
            app.quit()
        except Exception:
            pass
    return False


GLib.timeout_add(2500, next_turn)
GLib.timeout_add(1000 * (20 + 6 * len(NASTY)), watchdog)
app.run([])
sys.exit(1 if FAILS else 0)
