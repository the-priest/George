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
    def chat_stream(self, messages, on_token, stop, on_stall=None, model=""):
        if not self.replies:
            return ""
        return self.replies.pop(0)


def test_dedupe_threshold():
    # count how many times the 'news' tool is actually invoked
    counter = {"calls": 0}
    def news_tool(args, ag):
        counter['calls'] += 1
        return '1. Test headline'
    gt.TOOLS['news'] = news_tool

    cfg = dict(gc.DEFAULTS)
    # set threshold to 1 so any repeat is deduped immediately
    cfg['dedupe_repeat_threshold'] = 1
    tts = DummyTTS()
    mem = gt.MemoryStore()
    ag = Agent(cfg, mem, tts)
    # Ollama replies: same tool twice in one turn
    ag.ollama = MockOllama([
        '{"tool":"news","args":{"topic":"a"}}\n{"tool":"news","args":{"topic":"a"}}',
        '{"tool":"answer","args":{"text":"Here"}}'
    ])

    collected = {"final": None}
    ag.on_final = lambda s: collected.update({"final": s})
    ag.run_turn('hi')

    # with threshold=1, second identical call should be ignored, so actual calls == 1
    assert counter['calls'] == 1, f"news tool called {counter['calls']} times, expected 1"
    assert collected['final'] is not None
