"""Bug hunt.

Two things this catches that the normal suite does not:

  1. Cross-module names.  pyflakes does not resolve `from x import y`, so
     a name that moved between modules stays invisible until it is run.
     This walks every import in every file and checks it for real.

  2. Swallowed exceptions.  george.py wraps every GLib callback in
     idle()/guard() so a bad frame cannot kill the main loop -- which
     means a genuine bug now writes a line to the log instead of
     crashing.  This drives the window hard and then treats any
     "failed:" line in the log as a failure.
"""
import ast
import importlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = tmp + "/cfg"
os.environ["XDG_DATA_HOME"] = tmp + "/data"
sys.path.insert(0, ROOT)

problems = []

# ------------------------------------------------------------------ 1
MODULES = ["george", "george_core", "george_tools", "george_voice",
           "george_theme", "george_hud", "george_vision", "george_sound"]

for fname in MODULES:
    path = os.path.join(ROOT, fname + ".py")
    tree = ast.parse(open(path).read(), fname)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("george"):
            continue
        try:
            mod = importlib.import_module(node.module)
        except Exception as exc:
            problems.append("%s: cannot import %s (%s)"
                            % (fname, node.module, exc))
            continue
        for alias in node.names:
            if not hasattr(mod, alias.name):
                problems.append("%s imports %s from %s, which does not exist"
                                % (fname, alias.name, node.module))

# attribute use on the objects george.py drives
import george_core as C          # noqa: E402
import george_tools as X         # noqa: E402
import george_voice as V         # noqa: E402

cfg = C.coerce_config({})
tts = V.TextToSpeech(cfg)
stt = V.SpeechToText(cfg)
agent = X.Agent(cfg, C.MemoryStore(), tts)

for obj, name, attrs in (
        (tts, "TextToSpeech", ["speak", "stop", "engine_name", "speaking",
                               "reconfigure", "on_state"]),
        (stt, "SpeechToText", ["available", "why_unavailable", "start",
                               "stop_and_transcribe", "engine"]),
        (agent, "Agent", ["on_step", "on_token", "on_tool", "on_final",
                          "on_error", "on_news", "on_weather", "on_vitals",
                          "on_image", "on_done", "ask_confirm", "start",
                          "stop", "reset", "history", "busy", "call_tool",
                          "system_message", "step", "tool_card", "confirm",
                          "show_news", "show_weather", "show_vitals",
                          "show_image", "memory", "cfg", "tts"]),
        (C.OllamaSupervisor(cfg), "OllamaSupervisor",
         ["ensure_running", "shutdown", "status_line", "state", "client"]),
        (C.ChatStore(cfg), "ChatStore",
         ["save", "get", "listing", "purge", "delete"]),
        (C.ModelManager(cfg), "ModelManager", ["installed", "pull", "delete"]),
        (C.Ollama(cfg), "Ollama",
         ["models", "alive", "chat_stream", "version", "resolve_model"])):
    for attr in attrs:
        if not hasattr(obj, attr):
            problems.append("%s has no attribute %r" % (name, attr))

# ------------------------------------------------------------------ 2
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"models": [{"name": "qwen3:4b",
                                    "size": 4700000000, "details": {}}],
                        "version": "0.5.0"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        for chunk in ('{"message":{"content":"<think>looking</think>"}}',
                      '{"message":{"content":"{\\"tool\\":\\"system\\",'
                      '\\"args\\":{}}"}}',
                      '{"message":{"content":"{\\"tool\\":\\"answer\\",'
                      '\\"args\\":{\\"text\\":\\"**Done.** 91%% on `/home` '
                      '& <x>.\\\\n\\\\n- a\\\\n- b\\\\n\\\\n```sh\\\\nls\\\\n'
                      '```\\\\n\\\\n[wiki](https://w.example)\\"}}"}}',
                      '{"done":true}'):
            self.wfile.write((chunk + "\n").encode())


srv = HTTPServer(("127.0.0.1", 0), H)
os.environ["GEORGE_OLLAMA"] = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

import george as GUI            # noqa: E402
from gi.repository import GLib  # noqa: E402

app = GUI.GeorgeApp()
stage = {"n": 0}


def drive():
    """Every path a user can reach, hammered in order."""
    w = app.win
    n = stage["n"]
    stage["n"] += 1
    try:
        if n == 0:
            w._send("what is eating the box?")
        elif n == 1:
            for state in ("idle", "busy", "speaking", "listening", "down"):
                w._set_state(state)
            for accent in ("amber", "violet", "green", "red", "white",
                           "cyan"):
                w.cfg["accent"] = accent
                w.reload_css()
            w.cfg["ui_density"] = "compact"
            w.cfg["animations"] = False
            w.reload_css()
            w.cfg["ui_density"] = "comfortable"
            w.cfg["animations"] = True
            w.reload_css()
        elif n == 2:
            # rendering with junk, missing keys and hostile text
            w.render_vitals({})
            w.render_weather({})
            w.render_weather({"error": "nope"})
            w.render_news([])
            w.render_news([{"title": "no url"}, {"source": "S", "url": "x"}])
            w.render_engine()
            w.refresh_chats()
            for junk in ("", "<b>unclosed", "100% & <script>", "a" * 4000,
                         "```\nno lang\n```", "[l](https://x.y)",
                         "\x00\x01 control chars"):
                w.add_ai_bubble("").finalise(junk)
            w.add_user_bubble("<not a tag> & 'quotes'")
            w.add_tool_card("run", "x" * 500, "REFUSED")
            w.add_image("/nope/missing.png")
            w.copy("clipboard test")
        elif n == 3:
            for fn in (w._open_models, w._open_settings, w._open_history,
                       w._open_about, w._open_shortcuts):
                fn()
        elif n == 4:
            # settings apply path, then the theme reload it triggers
            w._open_settings()
        elif n == 5:
            for win in list(app.get_windows()):
                if win is not w:
                    win.close()
        elif n == 6:
            w._save_session()
            w.refresh_chats()
            rows = w.chats.listing()
            if rows:
                w._load_session(rows[0][0])
                w._delete_chat(rows[0][0])
            w._on_new_chat(None)
            w._on_toggle_sidebar(None)
            w._on_toggle_sidebar(None)
            w._on_voice_toggle(w.voice_btn)
            w._on_voice_toggle(w.voice_btn)
            w._on_mic(None)
            w._greet()
            w._set_subtitle()
            w._tick_clock()
            w._refresh_vitals()
        elif n == 7:
            # ambient mode: toggle on with no vision model available,
            # which must refuse cleanly rather than spin a thread
            w.watch_btn.set_active(True)
            w.watch_btn.set_active(False)
            w._watch_said("You have left a build failing in that terminal.")
            w._sync_watcher()
            for tone in ("send", "reply", "error", "listen", "notice"):
                w.sfx.play(tone)
        elif n == 8:
            # keyboard paths
            from gi.repository import Gdk
            for key in (Gdk.KEY_Escape, Gdk.KEY_F9, Gdk.KEY_F5, Gdk.KEY_k,
                        Gdk.KEY_n, Gdk.KEY_m, Gdk.KEY_h, Gdk.KEY_comma):
                w._on_window_key(None, key, 0, Gdk.ModifierType.CONTROL_MASK)
            w._on_toggle_sidebar(None)
            w._on_toggle_sidebar(None)
        else:
            app.teardown()
            app.quit()
            return False
    except Exception as exc:
        import traceback
        problems.append("stage %d raised: %r\n%s"
                        % (n, exc, traceback.format_exc()[-900:]))
    return True


GLib.timeout_add(1500, drive)
GLib.timeout_add(45000, lambda: (app.teardown(), app.quit(), False)[2])
app.run([])

# any exception the fail-safe wrappers ate is a bug, not a pass
log = os.path.join(os.environ["XDG_DATA_HOME"], "george", "george.log")
if os.path.exists(log):
    for line in open(log):
        if ("failed:" in line or "UNCAUGHT" in line or "css:" in line) \
                and "weather" not in line and "news failed" not in line \
                and "model list failed" not in line \
                and "feed " not in line:
            problems.append("log: " + line.strip()[:200])

print("AUDIT problems: %d" % len(problems))
for p in problems:
    print(" *", p)
sys.exit(1 if problems else 0)
