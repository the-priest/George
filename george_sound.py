#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_sound.py -- the noises.

Short UI tones, synthesised on first use with the stdlib wave module and
cached as WAVs. No bundled audio assets: nothing to license, nothing to
ship, nothing to go missing on someone else's distro.

Playback is fire-and-forget through whatever player the box has. If
there is no player, or no audio at all, every call here quietly does
nothing -- a missing blip must never interrupt anything.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import threading
import wave
from typing import Any, Dict, List, Optional, Tuple

import george_platform as osx
from george_core import log

RATE = 44100

# name -> (segments, gain)  where a segment is (freq_hz, ms, shape)
# shape: "in" fades up, "out" fades down, "bell" is a struck decay
TONES: Dict[str, Tuple[List[Tuple[float, int, str]], float]] = {
    "send":   ([(660.0, 42, "out"), (990.0, 66, "out")], 0.20),
    "reply":  ([(880.0, 55, "bell"), (587.33, 120, "bell")], 0.22),
    "listen": ([(1320.0, 38, "out")], 0.18),
    "stop":   ([(440.0, 55, "out"), (330.0, 70, "out")], 0.18),
    "error":  ([(220.0, 90, "out"), (185.0, 140, "out")], 0.24),
    "notice": ([(1174.66, 60, "bell"), (1567.98, 90, "bell"),
                (1174.66, 150, "bell")], 0.16),
}


def _render(segments: List[Tuple[float, int, str]], gain: float) -> bytes:
    frames = bytearray()
    for freq, ms, shape in segments:
        n = int(RATE * ms / 1000.0)
        for i in range(n):
            t = i / float(RATE)
            pos = i / float(max(1, n - 1))
            if shape == "in":
                env = pos
            elif shape == "bell":
                env = math.exp(-4.5 * pos)
            else:
                env = 1.0 - pos
            # a touch of second harmonic keeps it from sounding like a
            # test tone
            sample = (math.sin(2 * math.pi * freq * t) * 0.82 +
                      math.sin(4 * math.pi * freq * t) * 0.18)
            # short raised-cosine edges kill the click at each boundary
            edge = min(1.0, pos * 40.0, (1.0 - pos) * 40.0)
            value = int(32767 * gain * env * edge * sample)
            frames += struct.pack("<h", max(-32768, min(32767, value)))
    return bytes(frames)


class Sounds:

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.dir = os.path.join(osx.cache_dir(), "sfx")
        self.player = self._find_player()
        self._built: Dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _find_player() -> Optional[List[str]]:
        if osx.IS_WINDOWS:
            # winsound is in the stdlib and plays a WAV without spawning
            # anything, which matters here: these are 60ms blips and
            # launching a process per blip would cost more than the blip.
            return ["winsound"]
        for exe, argv in (("pw-play", ["pw-play"]),
                          ("paplay", ["paplay"]),
                          ("aplay", ["aplay", "-q"]),
                          ("ffplay", ["ffplay", "-nodisp", "-autoexit",
                                      "-loglevel", "quiet"])):
            if shutil.which(exe):
                return argv
        return None

    @property
    def available(self) -> bool:
        return self.player is not None

    def _path(self, name: str) -> str:
        """Render on first use, then reuse the file."""
        with self._lock:
            if name in self._built:
                return self._built[name]
            spec = TONES.get(name)
            if not spec:
                return ""
            path = os.path.join(self.dir, name + ".wav")
            try:
                os.makedirs(self.dir, exist_ok=True)
                if not os.path.exists(path):
                    with wave.open(path, "wb") as fh:
                        fh.setnchannels(1)
                        fh.setsampwidth(2)
                        fh.setframerate(RATE)
                        fh.writeframes(_render(*spec))
                self._built[name] = path
                return path
            except Exception as exc:
                log("sound render failed (%s): %s" % (name, exc))
                return ""

    def play(self, name: str) -> None:
        if not self.cfg.get("sounds", True) or not self.player:
            return
        path = self._path(name)
        if not path:
            return
        try:
            if self.player == ["winsound"]:
                import winsound
                winsound.PlaySound(
                    path, winsound.SND_FILENAME | winsound.SND_ASYNC |
                    winsound.SND_NODEFAULT)
                return
            subprocess.Popen(self.player + [path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             **osx.spawn_kwargs(detach=True))
        except Exception as exc:
            log("sound playback failed: %s" % exc)

    def prebuild(self) -> None:
        """Render every tone off the UI thread so the first click is not
        the one that pays for it."""
        def work() -> None:
            for name in TONES:
                self._path(name)
        threading.Thread(target=work, daemon=True,
                         name="george-sfx").start()
