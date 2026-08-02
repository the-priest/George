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

import george_platform as osx
from george_core import log, run_shell, strip_reasoning, _read_first


# =====================================================================
# VOICE  --  local only.  Piper if present, espeak-ng otherwise.
# =====================================================================

_PIPER_DIRS = ["~/.local/share/piper-voices", "~/.local/share/piper",
               "~/.cache/piper", "/usr/share/piper-voices",
               "/usr/local/share/piper-voices"]
if osx.IS_WINDOWS:
    _PIPER_DIRS = [os.path.join(osx.data_dir(), "piper-voices"),
                   os.path.join(osx.data_dir(), "piper"),
                   os.path.join(os.environ.get("LOCALAPPDATA", ""), "Piper"),
                   os.path.join(os.environ.get("ProgramFiles", ""), "Piper"),
                   "~/piper"] + _PIPER_DIRS


# SAPI is the reason voice works on a fresh Windows box with nothing
# installed: every Windows since XP ships it, and .NET's wrapper around
# it is one Add-Type away. Text arrives on stdin so no sentence can ever
# break the quoting.
_SAPI_SCRIPT = ("Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Rate = %d; $s.Volume = 100; %s "
                "$s.Speak([Console]::In.ReadToEnd())")


def _sapi_rate(speed: float) -> int:
    """SAPI rate is -10..10 where 0 is normal, not a multiplier."""
    return max(-10, min(10, int(round((speed - 1.0) * 10))))

_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+")
_MD = re.compile(r"[*_#>|]+")
_EMOJI = re.compile("[^\x00-\x7f]+")
_SENT = re.compile(r"(?<=[.!?:;])\s+")


# Things that read badly out loud.  Spoken output is not written
# output: "GiB" is "gibibytes", "~/" is "home", and a bare "%" in the
# middle of a sentence is a hiccup in every engine tested.
_SPEAK_MAP = [
    (re.compile(r"\bGiB\b"), " gigabytes"),
    (re.compile(r"\bMiB\b"), " megabytes"),
    (re.compile(r"\bKiB\b"), " kilobytes"),
    (re.compile(r"\bGB\b"), " gigabytes"),
    (re.compile(r"\bMB\b"), " megabytes"),
    (re.compile(r"\bkB\b"), " kilobytes"),
    (re.compile(r"(\d)\s*%"), r"\1 percent"),
    (re.compile(r"(\d)\s*C\b"), r"\1 degrees"),
    (re.compile(r"(\d+)\s*km/h"), r"\1 kilometres an hour"),
    (re.compile(r"\be\.g\.", re.I), "for example"),
    (re.compile(r"\bi\.e\.", re.I), "that is"),
    (re.compile(r"\betc\.", re.I), "and so on"),
    (re.compile(r"\bvs\.?\b", re.I), "versus"),
    (re.compile(r"~/"), "home folder "),
    (re.compile(r"\bCPU\b"), "C P U"),
    (re.compile(r"\bRAM\b"), "ram"),
    (re.compile(r"\bSSD\b"), "S S D"),
    (re.compile(r"\bwifi\b", re.I), "why fye"),
    (re.compile(r"\b(\w+)/(\w+)\b"), r"\1 \2"),
    (re.compile(r"[-_]{2,}"), " "),
    (re.compile(r"\s*[|>#]+\s*"), " "),
]


def clean_for_speech(text: str) -> str:
    """Turn a written reply into something worth hearing."""
    s = strip_reasoning(text)
    s = _CODE_FENCE.sub(" ... code on screen ... ", s)
    s = _INLINE_CODE.sub(lambda m: " " + m.group(0).strip("`") + " ", s)
    s = _URL.sub(" the link on screen ", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.S)
    s = _MD.sub(" ", s)
    s = _EMOJI.sub(" ", s)
    for rx, rep in _SPEAK_MAP:
        s = rx.sub(rep, s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
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
    """Explicit path wins.  Otherwise prefer the configured locale --
    an Irish box asking a US voice to read Irish place names is the
    single most jarring thing the old build did."""
    explicit = (cfg.get("piper_model") or "").strip()
    if explicit and os.path.exists(os.path.expanduser(explicit)):
        return os.path.expanduser(explicit)
    pref = str(cfg.get("piper_voice_pref", "en_GB") or "").lower()
    found: List[str] = []
    for d in _PIPER_DIRS:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                if f.endswith(".onnx"):
                    found.append(os.path.join(root, f))
    if not found:
        return None
    if pref:
        for path in found:
            if pref in os.path.basename(path).lower():
                return path
        lang = pref.split("_")[0]
        for path in found:
            if os.path.basename(path).lower().startswith(lang):
                return path
    return found[0]


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
        self._espeak = (osx.find_binary("espeak-ng") or
                        osx.find_binary("espeak"))
        piper_exe = osx.find_binary("piper")
        self._sapi = bool(osx.IS_WINDOWS and
                          (osx.find_binary("powershell") or
                           osx.find_binary("pwsh")))
        model = _find_piper_model(self.cfg)
        piper_ok = bool(piper_exe and model)
        if piper_ok:
            self._piper_cmd = [piper_exe]
            self._piper_model = model
        if osx.IS_WINDOWS:
            self._player = ["winsound"]
        else:
            for p in (["paplay"], ["aplay", "-q"], ["pw-play"],
                      ["ffplay", "-nodisp", "-autoexit", "-loglevel",
                       "quiet"]):
                if shutil.which(p[0]):
                    self._player = p
                    break

        if pref == "none":
            self._engine = None
        elif pref == "piper" and piper_ok and self._player:
            self._engine = "piper"
        elif pref == "espeak" and self._espeak:
            self._engine = "espeak"
        elif pref == "sapi" and self._sapi:
            self._engine = "sapi"
        elif piper_ok and self._player:
            self._engine = "piper"
        elif self._sapi:
            # Before espeak on Windows on purpose: SAPI is already
            # installed and sounds like a person, espeak sounds like 1998.
            self._engine = "sapi"
        elif self._espeak:
            self._engine = "espeak"
        elif shutil.which("spd-say"):
            self._engine = "spd"
        else:
            self._engine = None
        log("tts engine=%s piper=%s espeak=%s sapi=%s" %
            (self._engine, bool(piper_ok), bool(self._espeak), self._sapi))

    @property
    def engine_name(self) -> str:
        return self._engine or "none"

    @property
    def speaking(self) -> bool:
        with self._lock:
            return self._cur is not None

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
        if osx.IS_WINDOWS:
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        if proc and proc.poll() is None:
            osx.kill_tree(proc, force=True)
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

    def _play_wav(self, path: str, gen: int) -> None:
        """Blocking WAV playback with no external player.

        This runs on the TTS worker thread, so blocking is fine and it
        is what keeps the sentence queue in order. stop() interrupts it
        with SND_PURGE from the UI thread.
        """
        try:
            import winsound
            with self._lock:
                if gen != self._gen:
                    return
            winsound.PlaySound(path, winsound.SND_FILENAME |
                               winsound.SND_NODEFAULT)
        except Exception as exc:
            log("wav playback failed: %s" % exc)

    def _spawn(self, argv: List[str], gen: int,
               stdin_text: Optional[str] = None) -> None:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_text is not None
            else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **osx.spawn_kwargs())
        with self._lock:
            if gen != self._gen:
                osx.kill_tree(proc, force=True)
                return
            self._cur = proc
        try:
            if stdin_text is not None:
                proc.communicate(stdin_text.encode("utf-8"), timeout=120)
            else:
                proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            osx.kill_tree(proc, force=True)
        except Exception as exc:
            log("tts spawn failed: %s" % exc)
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
                if self._player == ["winsound"]:
                    self._play_wav(wav, gen)
                else:
                    self._spawn(self._player + [wav], gen)
                try:
                    os.unlink(wav)
                except OSError:
                    pass
            return
        if self._engine == "sapi":
            exe = osx.find_binary("powershell") or osx.find_binary("pwsh")
            if not exe:
                self._engine = None
                return
            voice = ""
            pref = str(self.cfg.get("piper_voice_pref", "en_GB") or "").lower()
            if pref.startswith("en_gb") or pref.startswith("en-gb"):
                # Hazel is the British voice; if it is not installed the
                # try/catch leaves the default in place rather than
                # failing the whole utterance.
                voice = ("try { $s.SelectVoice('Microsoft Hazel Desktop') } "
                         "catch { }")
            script = _SAPI_SCRIPT % (_sapi_rate(speed), voice)
            self._spawn([exe, "-NoProfile", "-NonInteractive",
                         "-ExecutionPolicy", "Bypass", "-Command", script],
                        gen, stdin_text=sentence)
            return
        if self._engine == "espeak" and self._espeak:
            rate = str(int(165 * speed))
            # espeak pitch 0 is a legitimate setting (the range is 0-99),
            # and it is falsy, so `or 38` silently ignored it.
            _p = self.cfg.get("voice_pitch", 38)
            try:
                pitch = str(int(_p if _p is not None else 38))
            except (TypeError, ValueError):
                pitch = "38"
            voice = str(self.cfg.get("piper_voice_pref", "en_GB")
                        ).lower().replace("_", "-")
            argv = [self._espeak, "-s", rate, "-p", pitch, "-a", "190"]
            if voice.startswith("en"):
                argv += ["-v", voice if "-" in voice else "en-gb"]
            self._spawn(argv + [sentence], gen)
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
        if osx.IS_WINDOWS:
            # No arecord on Windows. ffmpeg's dshow input is the only
            # thing that records to a WAV without a pip install, and
            # "audio=default" lets it pick the default capture device
            # rather than making the user name their microphone.
            exe = osx.find_binary("ffmpeg")
            if exe:
                return [exe, "-loglevel", "quiet", "-f", "dshow",
                        "-i", "audio=default", "-ar", "16000", "-ac", "1",
                        "-y"]
            return None
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
        for exe in ("whisper-cli", "whisper-cpp", "whisper", "faster-whisper",
                    "main"):
            if osx.find_binary(exe):
                return "whisper-cli" if exe == "main" else exe
        return None

    @property
    def available(self) -> bool:
        return bool(self.recorder and self.engine)

    def why_unavailable(self) -> str:
        if not self.recorder:
            if osx.IS_WINDOWS:
                return ("no recorder found - install ffmpeg "
                        "(winget install Gyan.FFmpeg) so the mic can record")
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
            self._rec = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         **osx.spawn_kwargs())
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
            if osx.IS_WINDOWS and proc.stdin is not None:
                # ffmpeg stops cleanly and finalises the WAV header on
                # "q" from stdin. A hard kill leaves a truncated file
                # whisper then refuses to read.
                try:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                except Exception:
                    osx.interrupt(proc)
            else:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            osx.kill_tree(proc, force=True)
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
        dirs = ["~/.local/share/whisper", "~/.cache/whisper",
                "/usr/share/whisper.cpp/models", "~/whisper.cpp/models"]
        if osx.IS_WINDOWS:
            dirs = [os.path.join(osx.data_dir(), "whisper"),
                    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                 "whisper")] + dirs
        for d in dirs:
            d = os.path.expanduser(d)
            if os.path.isdir(d):
                for f in sorted(os.listdir(d)):
                    if f.endswith(".bin"):
                        return os.path.join(d, f)
        return ""

