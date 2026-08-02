"""Constrained decoding.

Everything up to now has tried to persuade a 4B model to emit the
protocol correctly: worked examples, blunt rules, a firewall to catch it
when it fails anyway. Persuasion has a ceiling.

ollama accepts a JSON Schema in `format` and masks the sampler so only
tokens that keep the output valid can be produced. The model does not
try to emit the protocol; it becomes incapable of emitting anything
else. There is no token path from a constrained decode to "We are in a
new conversation. The user says...".

This is the difference between asking it to behave and making
misbehaviour unrepresentable.
"""
import json
import os
import sys
import tempfile
import threading

_t = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = _t + "/cfg"
os.environ["XDG_DATA_HOME"] = _t + "/data"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_core as gc          # noqa: E402
import george_tools as gt         # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


# -- the schema itself --------------------------------------------------
sc = gt.tool_schema()
check(sc.get("type") == "object", "schema root is not an object")
check(set(sc.get("required", [])) == {"tool", "args"},
      "schema does not require both tool and args")
enum = sc["properties"]["tool"]["enum"]
check("answer" in enum, "the answer action is not in the enum")
for t in gt.TOOLS:
    check(t in enum, "registered tool %s is missing from the schema" % t)
check(len(enum) == len(set(enum)), "duplicate entries in the tool enum")
check(sc["properties"]["args"]["type"] == "object",
      "args is not typed as an object")
# it has to survive a round trip through json, since that is how it ships
json.loads(json.dumps(sc))


# -- it must actually be sent, and be droppable -------------------------
class FakePost:
    """Stands in for Ollama._post and records the payload."""

    def __init__(self, fail_400_on=None):
        self.payloads = []
        self.fail_400_on = fail_400_on or []

    def __call__(self, path, payload, timeout):
        self.payloads.append(dict(payload))
        for key in self.fail_400_on:
            if key in payload:
                import urllib.error
                raise urllib.error.HTTPError(
                    "u", 400, "bad request", {},
                    __import__("io").BytesIO(b'{"error":"unknown field"}'))
        return _FakeResp()


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        yield json.dumps({"message": {"content": '{"tool":"answer",'
                                                 '"args":{"text":"ok"}}'},
                          "done": True}).encode()


def client(**over):
    cfg = dict(gc.DEFAULTS)
    cfg.update(over)
    return gc.Ollama(cfg)


c = client()
fp = FakePost()
c._post = fp
out = c.chat_stream([{"role": "user", "content": "x"}], lambda t: None,
                    threading.Event(), schema=sc)
check(fp.payloads and "format" in fp.payloads[0],
      "the schema was never sent in the request")
check(fp.payloads[0]["format"] == sc, "a different schema was sent")
check('"tool":"answer"' in out, "the response was not returned: %r" % out)

# structured: off must not send it
c = client(structured="off")
fp = FakePost()
c._post = fp
c.chat_stream([{"role": "user", "content": "x"}], lambda t: None,
              threading.Event(), schema=sc)
check("format" not in fp.payloads[0],
      "structured=off still sent the schema")

# no schema passed -> no format field
c = client()
fp = FakePost()
c._post = fp
c.chat_stream([{"role": "user", "content": "x"}], lambda t: None,
              threading.Event())
check("format" not in fp.payloads[0],
      "a format field appeared with no schema given")


# -- AN OLD OLLAMA MUST STILL WORK --------------------------------------
# This path was BROKEN before: the retry fetched a good response and then
# fell through to raise anyway, so `resp` was assigned and discarded.
c = client()
fp = FakePost(fail_400_on=["format"])
c._post = fp
try:
    out = c.chat_stream([{"role": "user", "content": "x"}], lambda t: None,
                        threading.Event(), schema=sc)
except Exception as exc:
    out = ""
    FAILS.append("an ollama that rejects `format` now breaks the app: %r"
                 % exc)
check('"tool":"answer"' in out,
      "the compatibility retry did not return the response: %r" % out)
check(len(fp.payloads) == 2, "expected exactly one retry, got %d attempts"
      % len(fp.payloads))
check("format" not in fp.payloads[1] and "think" not in fp.payloads[1],
      "the retry did not drop the optional fields: %s"
      % sorted(fp.payloads[1]))

# -- the agent must actually use it -------------------------------------
ag = gt.Agent(dict(gc.DEFAULTS), gt.MemoryStore(),
              type("T", (), {"speak": lambda s, t: None,
                             "stop": lambda s: None})())
check(ag._schema is not None, "the agent carries no schema")
check(ag._schema == gt.tool_schema(),
      "the agent's schema does not match the registry")

seen = {}


class RecordingOllama:
    def alive(self):
        return True

    def resolve_model(self):
        return ("mock:1", "")

    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        seen["schema"] = schema
        return '{"tool":"answer","args":{"text":"Hey."}}'


ag.ollama = RecordingOllama()
ag.on_final = lambda s: None
ag.run_turn("tell me something")
check(seen.get("schema") is not None,
      "the agent did not constrain its main request")

# -- config surface -----------------------------------------------------
check(gc.DEFAULTS.get("structured") == "auto",
      "structured decoding is not on by default")
check(gc.CHOICES.get("structured") == ("auto", "off"),
      "structured has no validated choices")
check(gc.coerce_config({"structured": "nonsense"})["structured"] == "auto",
      "a bad structured value is not repaired")

print("structured-decoding checks; failures: %d  (%d tools in the enum)"
      % (len(FAILS), len(enum)))
for f in FAILS:
    print("  FAIL: %s" % f)
sys.exit(1 if FAILS else 0)
