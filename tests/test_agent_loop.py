import json, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import os, tempfile
_t = tempfile.mkdtemp()
os.environ['XDG_CONFIG_HOME'] = _t + '/cfg'
os.environ['XDG_DATA_HOME'] = _t + '/data'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import george_core, george_tools, george_voice
class _NS:
    def __getattr__(self, k):
        for m in (george_core, george_tools, george_voice):
            if hasattr(m, k):
                return getattr(m, k)
        raise AttributeError(k)
G = _NS()

SCRIPT = [
    '<think>He wants the box vitals. I should call system.</think>\n'
    '{"tool": "system", "args": {}}',
    'Here you go.\n```json\n{"tool":"answer","args":{"text":"Box is up and healthy."}}\n```',
]
calls = {"n": 0, "prompts": []}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"models": [{"name": "qwen3:4b"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        calls["prompts"].append(payload["messages"])
        i = min(calls["n"], len(SCRIPT) - 1); calls["n"] += 1
        text = SCRIPT[i]
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for ch in [text[j:j+7] for j in range(0, len(text), 7)]:
            self.wfile.write((json.dumps({"message": {"content": ch}, "done": False}) + "\n").encode())
            self.wfile.flush()
        self.wfile.write((json.dumps({"message": {"content": ""}, "done": True}) + "\n").encode())
        self.wfile.flush()

srv = HTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

cfg = dict(G.DEFAULTS)
# This file counts the LOOP's own model calls. The verification pass adds
# one on tool-backed answers, and the router removes one -- both real,
# both measured in their own files. Isolate them here.
cfg["verify"] = "off"
cfg["router"] = False
cfg["ollama_url"] = "http://127.0.0.1:%d" % port
cfg["voice_enabled"] = False
agent = G.Agent(cfg, G.MemoryStore(), G.TextToSpeech(cfg))
seen = {"steps": [], "tools": [], "final": None, "err": None, "vitals": None,
        "tokens": 0}
agent.on_step = lambda s: seen["steps"].append(s)
agent.on_token = lambda s: seen.__setitem__("tokens", seen["tokens"] + 1)
agent.on_tool = lambda n, a, r: seen["tools"].append(n)
agent.on_final = lambda s: seen.__setitem__("final", s)
agent.on_error = lambda s: seen.__setitem__("err", s)
agent.on_vitals = lambda v: seen.__setitem__("vitals", v)

agent.run_turn("how's the box doing?")

fails = []
if seen["err"]: fails.append("error raised: %s" % seen["err"])
if seen["final"] != "Box is up and healthy.": fails.append("final=%r" % seen["final"])
if seen["vitals"] is None: fails.append("system tool never rendered vitals")
if seen["tokens"] < 5: fails.append("streaming produced %d tokens" % seen["tokens"])
if calls["n"] != 2: fails.append("expected 2 model calls, got %d" % calls["n"])
# the observation must have been fed back
obs = [m for m in agent.history if m["role"] == "user" and m["content"].startswith("OBSERVATION")]
if not obs: fails.append("observation was not fed back into history")
if "host:" not in (obs[0]["content"] if obs else ""): fails.append("observation body missing vitals")
# system prompt must carry tools + date
sysmsg = calls["prompts"][0][0]["content"]
for needle in ("web_search", "answer", "show", "Basilisk's brother", "pacman -Syu"):
    if needle not in sysmsg: fails.append("system prompt missing %r" % needle)

# --- stop button honesty: hitting stop mid-stream must not answer
SCRIPT[:] = ['{"tool":"answer","args":{"text":"should never be shown"}}']
agent2 = G.Agent(cfg, G.MemoryStore(), G.TextToSpeech(cfg))
got = {"final": None, "n": 0}
agent2.on_final = lambda s: got.__setitem__("final", s)
def tok(_p):
    got["n"] += 1
    if got["n"] == 3:
        agent2.stop()
agent2.on_token = tok
t = threading.Thread(target=agent2.run_turn, args=("hello",)); t.start(); t.join(20)
if t.is_alive(): fails.append("stopped turn never returned")
if got["final"] is not None: fails.append("stopped mid-stream but still answered")

# --- loop ceiling: a model that never answers must terminate
SCRIPT[:] = ['{"tool":"calc","args":{"expression":"1+1"}}']
cfg3 = dict(cfg); cfg3["max_steps"] = 3
agent3 = G.Agent(cfg3, G.MemoryStore(), G.TextToSpeech(cfg3))
err = {"msg": None}
agent3.on_error = lambda s: err.__setitem__("msg", s)
t0 = time.time(); agent3.run_turn("loop forever"); dur = time.time() - t0
if err["msg"] is None: fails.append("runaway loop did not hit the ceiling")
if dur > 30: fails.append("ceiling took %.1fs" % dur)

print("loop test failures:", len(fails))
for f in fails: print("  FAIL", f)
print("steps observed:", seen["steps"])
sys.exit(1 if fails else 0)
