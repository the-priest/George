import sys, os, tempfile
_t = tempfile.mkdtemp()
os.environ['XDG_CONFIG_HOME'] = _t + '/cfg'; os.environ['XDG_DATA_HOME'] = _t + '/data'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_tools as gt
from george_tools import Agent

class DummyTTS:
    def __init__(self):
        self.spoken = []
    def speak(self, text):
        self.spoken.append(text)
    def stop(self):
        pass

class MockOllama:
    def __init__(self, replies):
        self.replies = list(replies)
    def alive(self):
        return True
    def resolve_model(self):
        return ("mock:1", "")
    def chat_stream(self, messages, on_token, stop, on_stall=None, model=""):
        # return next reply in sequence
        if not self.replies:
            return ""
        return self.replies.pop(0)


def test_greeting_flow():
    # simulate model first asking to run news tool, then answering with a prose reply
    # make news tool deterministic
    gt.TOOLS['news'] = lambda args, ag: 'Headlines are now on his screen in the News panel.\n1. Test headline'

    import george_core as gc
    cfg = dict(gc.DEFAULTS)
    tts = DummyTTS()
    mem = gt.MemoryStore()
    ag = Agent(cfg, mem, tts)
    # inject mock ollama that returns a tool call then a final answer
    ag.ollama = MockOllama([
        '{"tool":"news","args":{"topic":"test"}}',
        '{"tool":"answer","args":{"text":"Here are the headlines."}}'
    ])

    collected = {"final": None}
    ag.on_final = lambda s: collected.update({"final": s})

    # run synchronously
    ag.run_turn('hi')

    assert collected['final'] is not None, 'final reply not produced'
    assert 'headlines' in collected['final'].lower() or 'here are' in collected['final'].lower()
