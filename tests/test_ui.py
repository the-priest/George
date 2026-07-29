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
        b = json.dumps({"models":[{"name":"deepseek-r1:7b","size":4700000000,"details":{"family":"qwen2"}},
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
    res["engine_rows"] = count(w.engine_body); res["vitals_rows"] = count(w.vitals_body)
    res["chats_rows"] = count(w.chats_body)
    if res["rows"] < 3: res["errors"].append("transcript rows=%d" % res["rows"])
    for name, fn in (("models", w._open_models), ("settings", w._open_settings),
                     ("history", w._open_history), ("about", w._open_about)):
        try: fn()
        except Exception as exc: res["errors"].append("%s dialog: %r" % (name, exc))
    try:
        w.cfg["font_scale"] = 1.25; w.reload_css(); w.render_engine(); w.refresh_chats()
        w.render_news([{"source":"T","title":"A headline","url":"https://x.example"}])
        w.render_weather({"place":"Dublin","temp_c":"14","feels_c":"12","desc":"Rain",
                          "wind_kph":"20","humidity":"80","max_c":"16","min_c":"9"})
        w.add_tool_card("run","uptime","exit 0"); w.toast("hello")
        w._on_new_chat(None); w._on_toggle_sidebar(None); w._on_toggle_sidebar(None)
    except Exception as exc: res["errors"].append("render: %r" % exc)
    GLib.timeout_add(1200, p3); return False
def p3():
    res["saved_chats"] = len(app.win.chats.listing())
    app.teardown(); app.quit(); return False
GLib.timeout_add(2500, p1)
app.run([])
print(json.dumps(res))
sys.exit(1 if res["errors"] else 0)
