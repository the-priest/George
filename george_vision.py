#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_vision.py -- eyes.

Two things live here:

  Eyes     one-shot screen reading. Takes a PNG, hands it to a local
           vision model, gets words back.

  Watcher  the ambient mode. On a timer it grabs the screen, asks what
           is happening, and decides whether that is worth saying out
           loud. It is off unless he turns it on, and the window shows
           an indicator the whole time it is running.

No GTK in here on purpose -- the watcher is a plain worker thread with
injected callbacks, so its throttling and its decision to speak or stay
quiet can be tested without a display.

The image never leaves the machine. It goes to the same local ollama
that answers everything else and nowhere near a network.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from george_core import DEFAULTS, Ollama, log

# Ollama does not advertise vision in /api/tags, so the family name is
# the only reliable signal without pulling a manifest per model.
VISION_HINTS = ("llava", "moondream", "bakllava", "vision", "-vl", "vl:",
                "gemma3", "gemma4", "minicpm", "granite3.2-vision",
                "qwen2.5vl", "qwen3vl", "pixtral", "internvl")

VISION_MODELS = [
    ("moondream", "1.7 GB", "tiny and fast - the laptop pick"),
    ("llava-phi3", "2.9 GB", "small, sharper than moondream"),
    ("gemma3:4b", "3.3 GB", "vision plus a decent chat model"),
    ("llava:7b", "4.7 GB", "the classic, most detail"),
]

DESCRIBE = ("Describe what is on this screen in one or two plain sentences. "
            "Name the application and what the person appears to be doing. "
            "Do not guess at anything you cannot actually see.")

_MODES = {
    "advice": ("Look at his screen. If you can see something genuinely "
               "useful - a mistake, a faster way, something he has missed - "
               "say it in ONE short sentence. "
               "If you have nothing useful, reply with exactly: NOTHING"),
    "banter": ("Look at his screen. If there is something worth a dry, "
               "good-natured one-liner, say it in ONE short sentence. Never "
               "mean, never about his appearance. "
               "If nothing is worth saying, reply with exactly: NOTHING"),
    "quiet": ("Look at his screen. Only speak if something looks genuinely "
              "wrong or urgent - an error, a failing build, a warning he may "
              "not have noticed. ONE short sentence. "
              "Otherwise reply with exactly: NOTHING"),
}


def _b64(path: str, limit: int = 8 * 1024 * 1024) -> str:
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("screenshot is over %d MB" % (limit // 1048576))
    return base64.b64encode(raw).decode("ascii")


class Eyes:
    """One local vision model, asked one question at a time."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.base = str(cfg.get("ollama_url",
                                DEFAULTS["ollama_url"])).rstrip("/")

    def installed(self) -> List[str]:
        names = Ollama(self.cfg).models()
        return [n for n in names
                if any(h in n.lower() for h in VISION_HINTS)]

    def pick(self) -> str:
        """Configured model if it is actually pulled, else anything that
        looks like it can see, else nothing."""
        want = str(self.cfg.get("vision_model", "") or "").strip()
        have = self.installed()
        if want and want in have:
            return want
        if want and not have:
            return want            # let the call fail with a real message
        return have[0] if have else ""

    def available(self) -> bool:
        return bool(self.pick())

    def look(self, image_path: str, prompt: str = DESCRIBE,
             timeout: int = 0) -> str:
        """Returns what it sees, or a line starting with 'cannot see'."""
        model = self.pick()
        if not model:
            return ("cannot see: no vision model is pulled. Settings > Eyes, "
                    "or run: ollama pull moondream")
        if not image_path or not os.path.exists(image_path):
            return "cannot see: the screenshot did not happen"
        try:
            image = _b64(image_path)
        except (OSError, ValueError) as exc:
            return "cannot see: %s" % exc

        payload = {"model": model, "prompt": prompt, "images": [image],
                   "stream": False, "think": False,
                   "keep_alive": self.cfg.get("keep_alive", "30m"),
                   "options": {"temperature": 0.3}}
        timeout = int(timeout or self.cfg.get("vision_timeout", 120))
        req = urllib.request.Request(
            self.base + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                # some builds reject `think` on a non-thinking model
                payload.pop("think", None)
                try:
                    req = urllib.request.Request(
                        self.base + "/api/generate",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        body = json.loads(
                            resp.read().decode("utf-8", "replace"))
                except Exception as exc2:
                    return "cannot see: %s" % exc2
            else:
                return "cannot see: ollama said %s" % exc.code
        except Exception as exc:
            return "cannot see: %s" % exc
        text = str(body.get("response", "")).strip()
        return text or "cannot see: the model returned nothing"


def _overlap(a: str, b: str) -> float:
    """Rough similarity, 0..1, on word sets. Good enough to notice that
    the screen has not meaningfully changed."""
    wa = set(w for w in a.lower().split() if len(w) > 3)
    wb = set(w for w in b.lower().split() if len(w) > 3)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / float(min(len(wa), len(wb)))


class Watcher:
    """Ambient mode.

    Restraint is the whole design. An assistant that pipes up every
    ninety seconds is an assistant you switch off in a day, so a comment
    has to clear three separate bars: the model has to have something to
    say at all, it must not repeat what it just said, and the rate caps
    have to allow it.
    """

    def __init__(self, cfg: Dict[str, Any], eyes: Eyes,
                 grab: Callable[[], str],
                 speak: Callable[[str], None]) -> None:
        self.cfg = cfg
        self.eyes = eyes
        self.grab = grab
        self.speak = speak
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_comment = ""
        self._last_at = 0.0
        self._hour: List[float] = []
        self.ticks = 0
        self.spoken = 0

    # -- lifecycle -----------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.running:
            return True
        if not self.eyes.available():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="george-watch")
        self._thread.start()
        log("watcher: on")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        log("watcher: off")

    def _loop(self) -> None:
        # a beat before the first look, so turning it on does not
        # immediately talk over whatever he was doing
        if self._stop.wait(8.0):
            return
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                log("watcher tick failed: %s" % exc)
            interval = max(20, int(self._num("watch_interval", 120)))
            if self._stop.wait(interval):
                return

    # -- one look ------------------------------------------------------
    def _num(self, key: str, default: float) -> float:
        """`cfg.get(k) or default` is wrong here: 0 is a legitimate value
        for the gap and it is falsy, so that idiom silently turns "no
        minimum gap" into the default. Coerce explicitly instead."""
        try:
            value = self.cfg.get(key, default)
            return float(default if value is None else value)
        except (TypeError, ValueError):
            return float(default)

    def allowed_now(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if now - self._last_at < self._num("watch_min_gap", 240):
            return False
        self._hour = [t for t in self._hour if now - t < 3600]
        return len(self._hour) < max(1, int(self._num("watch_max_per_hour", 8)))

    def tick(self, force: bool = False) -> str:
        """One look at the screen. Returns what was said, or ''."""
        self.ticks += 1
        if not force and not self.allowed_now():
            return ""
        shot = ""
        try:
            shot = self.grab()
        except Exception as exc:
            log("watcher screenshot failed: %s" % exc)
            return ""
        if not shot:
            return ""
        mode = str(self.cfg.get("watch_mode", "advice"))
        prompt = _MODES.get(mode, _MODES["advice"])
        said = self.eyes.look(shot, prompt)
        try:
            if os.path.exists(shot):
                os.remove(shot)     # do not leave his screen on disk
        except OSError:
            pass

        if said.startswith("cannot see"):
            log("watcher: %s" % said)
            return ""
        clean = said.strip().strip('"').strip()
        if not clean or "NOTHING" in clean.upper()[:40]:
            return ""
        if len(clean) > 400:
            clean = clean[:400].rsplit(" ", 1)[0] + "..."
        if _overlap(clean, self._last_comment) > 0.6:
            return ""               # it is repeating itself; stay quiet

        now = time.time()
        self._last_comment = clean
        self._last_at = now
        self._hour.append(now)
        self.spoken += 1
        try:
            self.speak(clean)
        except Exception as exc:
            log("watcher speak failed: %s" % exc)
        return clean
