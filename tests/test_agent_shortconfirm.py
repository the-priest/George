import sys, os, tempfile
_t = tempfile.mkdtemp()
os.environ['XDG_CONFIG_HOME'] = _t + '/cfg'; os.environ['XDG_DATA_HOME'] = _t + '/data'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import george_tools as gt
from george_tools import Agent
import george_core as gc

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
    def chat_stream(self, messages, on_token, stop, on_stall=None,
                    model="", schema=None):
        if not self.replies:
            return ""
        return self.replies.pop(0)


def test_short_confirmation_replaced():
    gt.TOOLS['news'] = lambda args, ag: 'Headlines are now on his screen in the News panel.\n1. Test headline'
    cfg = dict(gc.DEFAULTS)
    tts = DummyTTS()
    mem = gt.MemoryStore()
    ag = Agent(cfg, mem, tts)
    ag.ollama = MockOllama([
        '{"tool":"news","args":{"topic":"test"}}',
        '{"tool":"answer","args":{"text":"Done."}}'
    ])

    collected = {"final": None}
    ag.on_final = lambda s: collected.update({"final": s})
    ag.run_turn('hi')

    assert collected['final'] is not None
    assert 'headlines' in collected['final'].lower(), 'short confirmation was not replaced with observation summary'
