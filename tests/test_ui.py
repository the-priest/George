import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
tmp = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = tmp + "/cfg"; os.environ["XDG_DATA_HOME"] = tmp + "/data"
SCRIPT = ['<think>vitals</think>{"tool":"system","args":{}}',
          '{"tool":"answer","args":{"text":"Box is fine."}}']
calls = {"n": 0}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps({"models":[{"name":"qwen3:4b","size":2600000000,"details":{"family":"qwen3"}},
                                  {"name":"qwen2.5:7b","size":4700000000,"details":{}}]}).encode()
        self.send_response(200); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0)))
        i = min(calls["n"], len(SCRIPT)-1); calls["n"] += 1; t = SCRIPT[i]
        self.send_response(200); self.end_headers()
        for j in range(0,len(t),9):
            self.wfile.write((json.dumps({"message":{"content":t[j:j+9]},"done":False})+"\n").encode()); self.wfile.flush()
        self.wfile.write((json.dumps({"message":{"content":""},"done":True})+"\n").encode()); self.wfile.flush()
srv = HTTPServer(("127.0.0.1",0), H)
os.environ["GEORGE_OLLAMA"] = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import george as GUI
from gi.repository import GLib
app = GUI.GeorgeApp(); res = {"errors": []}
def count(b):
    n, c = 0, b.get_first_child()
    while c is not None: n += 1; c = c.get_next_sibling()
    return n
def texts(b):
    out, c = [], b.get_first_child()
    while c is not None:
        out.append(c); c = c.get_next_sibling()
    return out
def p1():
    app.win._send("how is the box doing?"); GLib.timeout_add(4000, p2); return False
def p2():
    w = app.win
    res["rows"] = count(w.transcript); res["feed"] = w.feed_lbl.get_text()
    res["state"] = w.status_lbl.get_text()
    res["engine_rows"] = count(w.engine_body); res["vitals_rows"] = count(w.vitals_body)
    res["chats_rows"] = count(w.chats_body)
    # 2.0 dropped the startup note row (the greeting lives in the status
    # strip now), so a finished turn is exactly two rows: his and George's.
    if res["rows"] < 2: res["errors"].append("transcript rows=%d" % res["rows"])
    kinds = [c.get_first_child() for c in texts(w.transcript)]
    css = [" ".join(k.get_css_classes()) if k else "" for k in kinds]
    if not any("bubble-user" in c for c in css):
        res["errors"].append("no user bubble: %s" % css)
    if not any("bubble-ai" in c for c in css):
        res["errors"].append("no ai bubble: %s" % css)
    # THE BUG THIS FILE EXISTS TO CATCH NOW:
    # Gtk.CssProvider.load_from_data REPLACES the provider's contents.
    # A second load_from_data for the wallpaper rule silently threw the
    # entire stylesheet away, so every bubble and card lost its
    # background and the app fell back to stock Adwaita. Assert that the
    # live provider still holds BOTH the theme and the extra rule.
    try:
        sheet = w.css_provider.to_string()
    except Exception as exc:
        sheet = ""
        res["errors"].append("provider unreadable: %r" % exc)
    for needed in (".bubble-user", ".bubble-ai", ".hud-card", ".composer",
                   "headerbar"):
        if needed not in sheet:
            res["errors"].append("stylesheet lost %s" % needed)
    import os.path as _op
    _art = any(_op.isfile(_op.join(_op.dirname(_op.dirname(
        _op.abspath(__file__))), n)) for n in ("george-bg.png", "george.png"))
    if _art and ".chat-bg" not in sheet:
        res["errors"].append("wallpaper rule missing from the sheet")
    res["sheet_bytes"] = len(sheet)
    if len(sheet) < 4000:
        res["errors"].append("stylesheet suspiciously small: %d" % len(sheet))

    # Bubbles have to be able to align: a Gtk.Box only positions a child
    # by halign if that child expands, so pin the property that makes
    # right-aligned user bubbles work at all.
    for row in texts(w.transcript):
        kid = row.get_first_child()
        if kid is None:
            continue
        if "bubble-user" in " ".join(kid.get_css_classes()):
            if kid.get_halign() != GUI.Gtk.Align.END:
                res["errors"].append("user bubble not aligned END")
            if not kid.get_hexpand():
                res["errors"].append("user bubble cannot align: no hexpand")

    for name, fn in (("models", w._open_models), ("settings", w._open_settings),
                     ("history", w._open_history), ("about", w._open_about),
                     ("shortcuts", w._open_shortcuts)):
        try: fn()
        except Exception as exc: res["errors"].append("%s dialog: %r" % (name, exc))
    try:
        w.cfg["font_scale"] = 1.25; w.reload_css(); w.render_engine(); w.refresh_chats()
        w.render_news([{"source":"T","title":"A headline","url":"https://x.example"}])
        w.render_weather({"place":"Dublin","temp_c":"14","feels_c":"12","desc":"Rain",
                          "wind_kph":"20","humidity":"80","max_c":"16","min_c":"9"})
        w.add_tool_card("run","uptime","exit 0"); w.toast("hello")
        # rich rendering: markdown, a fenced block, a link, and text that
        # would break a naive markup pass
        w.add_ai_bubble("").finalise(
            "**Done.** 91% used on `/home` & <not a tag>.\n\n"
            "- one\n- two\n\n```sh\nsudo pacman -Sc\n```\n\n"
            "See [the wiki](https://wiki.archlinux.org/) for the rest.")
        for state in ("idle","busy","speaking","listening","down"):
            w._set_state(state)
        for accent in ("amber","violet","green","red","white","cyan"):
            w.cfg["accent"] = accent; w.reload_css()
        w.cfg["animations"] = False; w.reload_css()
        w.cfg["animations"] = True; w.cfg["ui_density"] = "compact"; w.reload_css()
        w.render_vitals({"host":"x","uptime":"1d","load":"0.1","cpu_pct":"55",
                         "mem_pct":"41","disk_pct":"88","memory":"4/8 GiB",
                         "disk":"10/100 GiB","swap":"0/2 GiB","battery":"90%",
                         "temp":"44 C"})
        w.add_image("/nonexistent/shot.png")
        w._on_new_chat(None); w._on_toggle_sidebar(None); w._on_toggle_sidebar(None)
        if w._hero is None: res["errors"].append("hero missing after new chat")
    except Exception as exc: res["errors"].append("render: %r" % exc)
    GLib.timeout_add(1200, p3); return False
def p3():
    res["saved_chats"] = len(app.win.chats.listing())
    app.teardown(); app.quit(); return False
GLib.timeout_add(2500, p1)
app.run([])
print(json.dumps(res))
sys.exit(1 if res["errors"] else 0)
