#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_voice.py -- speech out and speech in, both entirely local.

Piper if a voice model is on disk, espeak-ng otherwise, spd-say as a
last resort.  Push-to-talk uses whisper.cpp or faster-whisper if either
is installed; if neither is, the mic button says so rather than
pretending.  No network, no keys, no cloud STT.
"""

from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from george_core import log, run_shell, strip_reasoning, _read_first


# =====================================================================
# VOICE  --  local only.  Piper if present, espeak-ng otherwise.
# =====================================================================

_PIPER_DIRS = ["~/.local/share/piper-voices", "~/.local/share/piper",
               "~/.cache/piper", "/usr/share/piper-voices",
               "/usr/local/share/piper-voices"]

_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+")
_MD = re.compile(r"[*_#>|]+")
_EMOJI = re.compile("[^\x00-\x7f]+")
_SENT = re.compile(r"(?<=[.!?:;])\s+")


def clean_for_speech(text: str) -> str:
    s = strip_reasoning(text)
    s = _CODE_FENCE.sub(" (code block) ", s)
    s = _INLINE_CODE.sub(" ", s)
    s = _URL.sub(" link ", s)
    s = _MD.sub(" ", s)
    s = _EMOJI.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def split_sentences(text: str, max_len: int = 240) -> List[str]:
    out: List[str] = []
    for part in _SENT.split(text):
        part = part.strip()
        while len(part) > max_len:
            cut = part.rfind(" ", 0, max_len)
            cut = cut if cut > 40 else max_len
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            out.append(part)
    return out


def _find_piper_model(cfg: Dict[str, Any]) -> Optional[str]:
    explicit = (cfg.get("piper_model") or "").strip()
    if explicit and os.path.exists(os.path.expanduser(explicit)):
        return os.path.expanduser(explicit)
    for d in _PIPER_DIRS:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                if f.endswith(".onnx"):
                    return os.path.join(root, f)
    return None


class TextToSpeech:
    """Background worker.  speak() enqueues, stop() interrupts now."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self._q: "queue.Queue[Tuple[int, str]]" = queue.Queue()
        self._gen = 0
        self._lock = threading.Lock()
        self._cur: Optional[subprocess.Popen] = None
        self._engine: Optional[str] = None
        self._piper_cmd: Optional[List[str]] = None
        self._piper_model: Optional[str] = None
        self._espeak: Optional[str] = None
        self._player: Optional[List[str]] = None
        self.on_state: Optional[Callable[[str], None]] = None
        self.reconfigure()
        threading.Thread(target=self._worker, daemon=True,
                         name="george-tts").start()

    # ---- engine selection -------------------------------------------
    def reconfigure(self) -> None:
        pref = str(self.cfg.get("voice_engine", "auto")).lower()
        self._espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        piper_exe = shutil.which("piper")
        model = _find_piper_model(self.cfg)
        piper_ok = bool(piper_exe and model)
        if piper_ok:
            self._piper_cmd = [piper_exe]
            self._piper_model = model
        for p in (["paplay"], ["aplay", "-q"], ["pw-play"], ["ffplay", "-nodisp",
                                                             "-autoexit", "-loglevel", "quiet"]):
            if shutil.which(p[0]):
                self._player = p
                break

        if pref == "none":
            self._engine = None
        elif pref == "piper" and piper_ok and self._player:
            self._engine = "piper"
        elif pref == "espeak" and self._espeak:
            self._engine = "espeak"
        elif piper_ok and self._player:
            self._engine = "piper"
        elif self._espeak:
            self._engine = "espeak"
        elif shutil.which("spd-say"):
            self._engine = "spd"
        else:
            self._engine = None
        log("tts engine=%s piper=%s espeak=%s" %
            (self._engine, bool(piper_ok), bool(self._espeak)))

    @property
    def engine_name(self) -> str:
        return self._engine or "none"

    # ---- public ------------------------------------------------------
    def speak(self, text: str) -> None:
        if not self.cfg.get("voice_enabled") or not self._engine:
            return
        clean = clean_for_speech(text)
        if not clean:
            return
        with self._lock:
            gen = self._gen
        for sentence in split_sentences(clean):
            self._q.put((gen, sentence))

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            proc = self._cur
            self._cur = None
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._emit("idle")

    def _emit(self, state: str) -> None:
        # Fires on the TTS worker thread.  This module stays GTK-free, so
        # whoever sets on_state is responsible for hopping to their own
        # main loop (the UI wraps it in GLib.idle_add).
        if self.on_state:
            try:
                self.on_state(state)
            except Exception as exc:
                log("tts state callback failed: %s" % exc)

    # ---- worker ------------------------------------------------------
    def _worker(self) -> None:
        while True:
            gen, sentence = self._q.get()
            with self._lock:
                if gen != self._gen:
                    continue
            self._emit("speaking")
            try:
                self._utter(sentence, gen)
            except Exception as exc:
                log("tts failed: %s" % exc)
            if self._q.empty():
                self._emit("idle")

    def _spawn(self, argv: List[str], gen: int) -> None:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
        with self._lock:
            if gen != self._gen:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                return
            self._cur = proc
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        with self._lock:
            if self._cur is proc:
                self._cur = None

    def _utter(self, sentence: str, gen: int) -> None:
        speed = float(self.cfg.get("voice_speed", 1.0) or 1.0)
        if self._engine == "piper" and self._piper_cmd and self._player:
            wav = os.path.join(tempfile.gettempdir(),
                               "george-tts-%d.wav" % os.getpid())
            argv = self._piper_cmd + ["--model", self._piper_model,
                                      "--output_file", wav,
                                      "--length_scale", "%.2f" % (1.0 / speed)]
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                proc.communicate(sentence.encode("utf-8"), timeout=90)
            except Exception as exc:
                log("piper failed, falling back: %s" % exc)
                self._engine = "espeak" if self._espeak else None
                return
            if os.path.exists(wav):
                self._spawn(self._player + [wav], gen)
                try:
                    os.unlink(wav)
                except OSError:
                    pass
            return
        if self._engine == "espeak" and self._espeak:
            rate = str(int(165 * speed))
            self._spawn([self._espeak, "-s", rate, "-p", "35", "-a", "180",
                         sentence], gen)
            return
        if self._engine == "spd":
            self._spawn(["spd-say", "-w", sentence], gen)


class SpeechToText:
    """Push to talk.  Records locally, transcribes locally.  If nothing
    local is installed the mic button says so instead of pretending."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self._rec: Optional[subprocess.Popen] = None
        self._wav = ""
        self.recorder = self._find_recorder()
        self.engine = self._find_engine()

    @staticmethod
    def _find_recorder() -> Optional[List[str]]:
        if shutil.which("parecord"):
            return ["parecord", "--channels=1", "--rate=16000",
                    "--file-format=wav"]
        if shutil.which("arecord"):
            return ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1"]
        if shutil.which("ffmpeg"):
            return ["ffmpeg", "-loglevel", "quiet", "-f", "pulse", "-i",
                    "default", "-ar", "16000", "-ac", "1", "-y"]
        return None

    @staticmethod
    def _find_engine() -> Optional[str]:
        for exe in ("whisper-cli", "whisper-cpp", "whisper", "faster-whisper"):
            if shutil.which(exe):
                return exe
        return None

    @property
    def available(self) -> bool:
        return bool(self.recorder and self.engine)

    def why_unavailable(self) -> str:
        if not self.recorder:
            return "no recorder found - install pipewire-pulse (parecord) or alsa-utils"
        if not self.engine:
            return "no local transcriber - install whisper.cpp (whisper-cli) or faster-whisper"
        return ""

    def start(self) -> bool:
        if not self.available or self._rec:
            return False
        self._wav = os.path.join(tempfile.gettempdir(),
                                 "george-in-%d.wav" % int(time.time()))
        argv = list(self.recorder) + [self._wav]
        try:
            self._rec = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         start_new_session=True)
            return True
        except Exception as exc:
            log("record failed: %s" % exc)
            self._rec = None
            return False

    def stop_and_transcribe(self) -> str:
        proc, self._rec = self._rec, None
        if not proc:
            return ""
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if not os.path.exists(self._wav) or os.path.getsize(self._wav) < 2000:
            return ""
        wav = shlex.quote(self._wav)
        if self.engine in ("whisper-cli", "whisper-cpp"):
            model = self._whisper_cpp_model()
            if not model:
                return "[no whisper.cpp model found in ~/.local/share/whisper]"
            rc, out = run_shell("%s -m %s -f %s -nt -np" %
                                (self.engine, shlex.quote(model), wav),
                                timeout=180)
        elif self.engine == "faster-whisper":
            rc, out = run_shell("faster-whisper %s --model base --language en "
                                "--output_format txt --output_dir %s" %
                                (wav, shlex.quote(tempfile.gettempdir())),
                                timeout=300)
            txt = self._wav.rsplit(".", 1)[0] + ".txt"
            if os.path.exists(txt):
                out = _read_first(txt)
        else:
            rc, out = run_shell("whisper %s --model base --language en "
                                "--output_format txt --output_dir %s "
                                "--fp16 False" %
                                (wav, shlex.quote(tempfile.gettempdir())),
                                timeout=300)
            txt = self._wav.rsplit(".", 1)[0] + ".txt"
            if os.path.exists(txt):
                out = _read_first(txt)
        try:
            os.unlink(self._wav)
        except OSError:
            pass
        out = re.sub(r"\[[0-9:.\s\->]+\]", "", out or "")
        return " ".join(out.split()).strip()

    @staticmethod
    def _whisper_cpp_model() -> str:
        for d in ("~/.local/share/whisper", "~/.cache/whisper",
                  "/usr/share/whisper.cpp/models", "~/whisper.cpp/models"):
            d = os.path.expanduser(d)
            if os.path.isdir(d):
                for f in sorted(os.listdir(d)):
                    if f.endswith(".bin"):
                        return os.path.join(d, f)
        return ""

