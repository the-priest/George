"""Fill the real window with representative content and hold it open so a
screenshot shows what he will actually look at."""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

tmp = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = tmp + "/cfg"
os.environ["XDG_DATA_HOME"] = tmp + "/data"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"models": [
            {"name": "deepseek-r1:7b", "size": 4700000000, "details": {}},
            {"name": "qwen2.5:7b", "size": 4700000000, "details": {}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write((json.dumps({"message": {"content": ""},
                                      "done": True}) + "\n").encode())


srv = HTTPServer(("127.0.0.1", 0), H)
os.environ["GEORGE_OLLAMA"] = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import george as GUI  # noqa: E402
from gi.repository import GLib  # noqa: E402

REPLY = ("Disk is the problem, not memory.\n\n"
         "**/home** is at 91% with 22 GiB left. The three biggest offenders "
         "are the pacman cache, old kernels, and `~/.cache/thumbnails`.\n\n"
         "- pacman cache: 8.1 GiB\n"
         "- old kernels: 2.4 GiB\n"
         "- thumbnails: 1.9 GiB\n\n"
         "Clearing the cache is the safe one:\n\n"
         "```sh\nsudo pacman -Sc\n```\n\n"
         "That gets you most of it back without touching anything you need.")

app = GUI.GeorgeApp()


def fill():
    w = app.win
    w.add_user_bubble("the box feels sluggish, what's eating it?")
    w.add_tool_card("system", "vitals", "ok")
    w.add_tool_card("disk", "filesystem usage", "ok")
    w.add_ai_bubble("").finalise(REPLY)
    w.render_weather({"place": "Dublin, Ireland", "temp_c": "14",
                      "feels_c": "12", "desc": "Light rain", "wind_kph": "23",
                      "humidity": "82", "max_c": "16", "min_c": "9"})
    w.render_news([
        {"source": "RTE", "title": "Budget talks run into a second night",
         "url": "https://example.invalid/1"},
        {"source": "ARS TECHNICA", "title": "Linux 6.19 lands with new "
         "scheduler work", "url": "https://example.invalid/2"},
        {"source": "THE REGISTER", "title": "Another cloud outage, another "
         "postmortem", "url": "https://example.invalid/3"},
    ])
    st = {"host": "cachy", "uptime": "2d 4h 11m", "load": "0.84 0.61 0.55",
          "memory": "9.2 / 32.0 GiB (29%)", "mem_pct": "29",
          "disk": "22 / 512 GiB free on home", "disk_pct": "91",
          "cpu_pct": "37", "swap": "0.0 / 8.0 GiB",
          "battery": "88% (Discharging)", "temp": "51 C"}
    w.render_vitals(st)
    for v in (0.2, 0.35, 0.28, 0.55, 0.71, 0.44, 0.38, 0.62, 0.49, 0.33,
              0.41, 0.58, 0.66, 0.37):
        w.spark_cpu.push(v)
    w.core.set_load(0.37)
    w._set_state("busy")
    w.feed_lbl.set_text("reading system vitals")
    w.think_lbl.set_text("checking whether the cache is the biggest single "
                         "win before saying anything")
    w.spinner.start()
    w._engine_ok = True
    w.render_engine()
    w._set_subtitle()
    return False


GLib.timeout_add(2500, fill)
GLib.timeout_add(60000, lambda: (app.teardown(), app.quit(), False)[2])
app.run([])
