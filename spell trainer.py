#!/usr/bin/env python3
"""
IELTS Spelling Trainer
======================

A dictation-style spelling drill for the command line.

The app reads a word aloud, you type what you heard, and it tells you whether
you got it right. Every miss is counted. Once you have spelled a word correctly
3 times it is retired from the deck, so the pool shrinks to only the words you
still get wrong.

Requires Python 3.8+ and nothing else. Speech uses whatever the operating
system already provides (Windows SAPI via PowerShell, macOS `say`, espeak on
Linux, or pyttsx3 if you happen to have it installed).

Getting a word wrong doesn't just move on: the correct spelling is shown and
you must type it correctly yourself before advancing to the next word.

Usage
-----
    python spell_trainer.py                 # start a practice session
    python spell_trainer.py -n 30           # session of 30 words
    python spell_trainer.py --stats         # show progress, don't practise
    python spell_trainer.py --list          # list remaining / retired words
    python spell_trainer.py --voices        # list available system voices
    python spell_trainer.py --rate -3       # slower speech (-10 fast..10 slow)
    python spell_trainer.py --add-words my_words.txt
    python spell_trainer.py --reset         # wipe progress and start over

In-session commands (type these instead of an answer)
-----------------------------------------------------
    r        repeat the word
    s        repeat slowly
    h        hint (first letter + number of letters)
    c        context: the example sentence with the word blanked out
    k        skip this word (counts as a miss)
    q        quit and save
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

if sys.version_info < (3, 7):  # pragma: no cover
    sys.exit("This app needs Python 3.7 or newer. "
             "On Windows try:  py -3 spell_trainer.py")

APP_DIR = Path(__file__).resolve().parent
WORDS_FILE = APP_DIR / "words.json"
PROGRESS_FILE = APP_DIR / "progress.json"
VOICES_DIR = APP_DIR / "voices"

CORRECT_TO_RETIRE = 3
PROGRESS_VERSION = 1


# --------------------------------------------------------------------------
# Terminal colour
# --------------------------------------------------------------------------

class C:
    """ANSI colour codes; blanked out when colour is disabled."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[96m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    OFF = "\033[0m"

    @classmethod
    def disable(cls) -> None:
        for name in ("GREEN", "RED", "YELLOW", "BLUE", "GREY", "BOLD", "OFF"):
            setattr(cls, name, "")


def enable_ansi() -> None:
    """Turn on ANSI escape handling in the Windows console.

    Older consoles print the escape codes as literal junk instead, so if this
    cannot be switched on we drop colour entirely.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(ctypes.c_void_p(handle),
                                       ctypes.byref(mode)):
            C.disable()
            return
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if not kernel32.SetConsoleMode(ctypes.c_void_p(handle),
                                       mode.value | 0x0004):
            C.disable()
    except Exception:
        C.disable()


# --------------------------------------------------------------------------
# Speech backends
# --------------------------------------------------------------------------

class Speaker:
    """Speaks text out loud using whatever the system offers.

    Backends are tried in order of quality/availability:
      piper    -> local neural TTS (Piper), if installed + a voice model is
                  present in voices/ - clearer, more accurate pronunciation
                  than the legacy Windows voices
      windows  -> System.Speech through PowerShell (built into Windows)
      pyttsx3  -> cross-platform Python TTS, if installed
      say      -> macOS
      espeak   -> Linux
      silent   -> no audio available; the app falls back to written clues
    """

    def __init__(self, rate: int = 0, voice: Optional[str] = None):
        self.rate = max(-10, min(10, rate))
        self.voice = voice
        self.voice_auto = False
        self.voice_warning: Optional[str] = None
        self._linux_cmd = "espeak"
        self._piper_voice = None
        self._piper_model_name: Optional[str] = None
        self.backend = self._detect()
        if self.backend == "piper":
            self._load_piper()
        if self.backend == "windows" and not self.voice:
            self._pick_windows_voice()

    # -- detection ---------------------------------------------------------

    def _detect(self) -> str:
        if self._piper_available():
            return "piper"
        return self._detect_system()

    def _piper_available(self) -> bool:
        try:
            if importlib.util.find_spec("piper") is None:
                return False
        except (ImportError, ValueError):
            return False
        return self._find_piper_model() is not None

    def _find_piper_model(self) -> Optional[Path]:
        if not VOICES_DIR.is_dir():
            return None
        # If self.voice names a specific model (by filename or stem), use it;
        # otherwise take whichever .onnx voice file is there.
        models = sorted(VOICES_DIR.glob("*.onnx"))
        if not models:
            return None
        if self.voice:
            for m in models:
                if self.voice in (m.name, m.stem):
                    return m
        return models[0]

    def _load_piper(self) -> None:
        try:
            from piper import PiperVoice
            model = self._find_piper_model()
            self._piper_voice = PiperVoice.load(str(model))
            self._piper_model_name = model.stem
        except Exception as exc:
            self.voice_warning = (
                f"Piper voice failed to load ({exc}); falling back to the "
                f"system voice."
            )
            self._piper_voice = None
            self.voice = None  # don't let a piper voice name leak into SAPI
            self.backend = self._detect_system()

    def _detect_system(self) -> str:
        if os.name == "nt" and shutil.which("powershell"):
            return "windows"
        # pyttsx3 is optional; checked by name so editors don't flag a
        # missing import when it isn't installed.
        try:
            if importlib.util.find_spec("pyttsx3") is not None:
                return "pyttsx3"
        except (ImportError, ValueError):
            pass
        if platform.system() == "Darwin" and shutil.which("say"):
            return "say"
        for cmd in ("espeak-ng", "espeak", "spd-say"):
            if shutil.which(cmd):
                self._linux_cmd = cmd
                return "espeak"
        return "silent"

    def _windows_voices_with_culture(self) -> List[tuple]:
        """[(voice name, culture e.g. 'en-GB'), ...] for installed voices."""
        script = (
            "Add-Type -AssemblyName System.Speech\n"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "$s.GetInstalledVoices() | ForEach-Object { "
            "$_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture.Name }"
        )
        tmp = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                          encoding="utf-8-sig")
        try:
            tmp.write(script)
            tmp.close()
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", tmp.name],
                capture_output=True, text=True, check=False,
            )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        pairs = []
        for ln in out.stdout.splitlines():
            ln = ln.strip()
            if "|" in ln:
                name, culture = ln.rsplit("|", 1)
                pairs.append((name, culture))
        return pairs

    def _pick_windows_voice(self) -> None:
        """Pick an English voice instead of trusting the Windows default.

        System.Speech defaults to whatever voice matches the OS's UI
        language. On a non-English Windows install that can silently pick a
        non-English voice to read English words with - which sounds
        garbled/robotic and mispronounces things, because it *is* the wrong
        language's speech engine, not just a bad one. Prefer British English
        (closer to IELTS listening audio) if installed, else any English
        voice, else warn and fall back to whatever the system default is.
        """
        voices = self._windows_voices_with_culture()
        english = [(n, c) for n, c in voices if c.lower().startswith("en")]
        if not english:
            if voices:
                default_name, default_culture = voices[0]
                self.voice_warning = (
                    f"No English voice is installed - Windows would speak "
                    f"with {default_name!r} ({default_culture}), which "
                    f"can't pronounce English correctly. See the README "
                    f"section 'Getting a British voice' to install one."
                )
            return
        uk = [n for n, c in english if c.lower() == "en-gb"]
        self.voice = uk[0] if uk else english[0][0]
        self.voice_auto = True

    @property
    def available(self) -> bool:
        return self.backend != "silent"

    def describe(self) -> str:
        names = {
            "piper": "Piper neural TTS (offline)",
            "windows": "Windows speech (System.Speech)",
            "pyttsx3": "pyttsx3",
            "say": "macOS say",
            "espeak": "espeak",
            "silent": "no audio available - running in written-clue mode",
        }
        base = names.get(self.backend, self.backend)
        if self.backend == "piper" and self._piper_model_name:
            base += f" - voice: {self._piper_model_name}"
        if self.backend == "windows" and self.voice:
            tag = ", auto-selected" if self.voice_auto else ""
            base += f" - voice: {self.voice}{tag}"
        return base

    # -- speaking ----------------------------------------------------------

    def say(self, text: str, slow: bool = False) -> None:
        rate = self.rate - 3 if slow else self.rate
        try:
            getattr(self, f"_say_{self.backend}")(text, rate)
        except Exception as exc:  # never let a speech glitch kill the drill
            print(f"{C.GREY}(speech failed: {exc}){C.OFF}")

    def _say_silent(self, text: str, rate: int) -> None:
        del text, rate
        return

    def _say_piper(self, text: str, rate: int) -> None:
        import wave
        from piper.config import SynthesisConfig

        # Map the app's -10 (fast) .. 10 (slow) rate onto Piper's
        # length_scale (bigger = slower speech), 1.0 = the model's default.
        length_scale = max(0.5, min(2.0, 1.0 - rate * 0.04))
        cfg = SynthesisConfig(length_scale=length_scale)

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            with wave.open(tmp.name, "wb") as wav_file:
                self._piper_voice.synthesize_wav(text, wav_file, cfg)
            self._upsample_wav_if_needed(tmp.name)
            self._play_wav(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def _upsample_wav_if_needed(self, path: str, min_rate: int = 44100) -> None:
        """Piper's voice models render at an unusual rate (often 22050 Hz).

        Windows' legacy playback path (winsound/waveOut) handles that badly
        on a lot of drivers/devices - it comes out crackly and broken up,
        because that path expects a "normal" rate like 44100/48000 Hz and
        doesn't always resample cleanly. Upsampling here, once, before
        playback fixes it; 22050 -> 44100 is an exact doubling so there's no
        quality lost doing it.
        """
        import wave
        try:
            import numpy as np
            with wave.open(path, "rb") as fh:
                rate = fh.getframerate()
                channels = fh.getnchannels()
                width = fh.getsampwidth()
                frames = fh.readframes(fh.getnframes())
            if rate >= min_rate or width != 2:
                return  # already a normal rate, or not 16-bit PCM - leave it
            audio = np.frombuffer(frames, dtype="<i2").astype(np.float32)
            if channels > 1:
                audio = audio.reshape(-1, channels)
            orig_len = audio.shape[0]
            target_len = max(1, round(orig_len * min_rate / rate))
            x_old = np.linspace(0, 1, orig_len, endpoint=False)
            x_new = np.linspace(0, 1, target_len, endpoint=False)
            if channels > 1:
                resampled = np.stack(
                    [np.interp(x_new, x_old, audio[:, c])
                     for c in range(channels)], axis=1
                )
            else:
                resampled = np.interp(x_new, x_old, audio)
            resampled = np.clip(resampled, -32768, 32767).astype("<i2")
            with wave.open(path, "wb") as fh:
                fh.setnchannels(channels)
                fh.setsampwidth(width)
                fh.setframerate(min_rate)
                fh.writeframes(resampled.tobytes())
        except Exception:
            pass  # worst case: play at the original rate

    def _play_wav(self, path: str) -> None:
        if os.name == "nt":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME)
            return
        if platform.system() == "Darwin" and shutil.which("afplay"):
            subprocess.run(["afplay", path], check=False)
            return
        for cmd in ("paplay", "aplay"):
            if shutil.which(cmd):
                subprocess.run([cmd, path], check=False,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                return

    def _ps_quote(self, s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    def _say_windows(self, text: str, rate: int) -> None:
        lines = [
            "Add-Type -AssemblyName System.Speech",
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
            f"$s.Rate = {rate}",
        ]
        if self.voice:
            lines.append(
                f"try {{ $s.SelectVoice({self._ps_quote(self.voice)}) }} catch {{ }}"
            )
        # A fresh audio stream makes the output device "wake up", and
        # whatever plays during that wake-up gets clipped - normally the
        # first letters of the word. Render a short burst of silence ahead
        # of the real text, in the same stream, so the clipping lands on
        # silence instead of on the word.
        lines.append("$b = New-Object System.Speech.Synthesis.PromptBuilder")
        lines.append("$b.AppendBreak([TimeSpan]::FromMilliseconds(300))")
        lines.append(f"$b.AppendText({self._ps_quote(text)})")
        lines.append("$s.Speak($b)")
        self._run_powershell("\n".join(lines))

    def _run_powershell(self, script: str) -> None:
        # utf-8-sig: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
        # which mangles anything outside the system code page.
        tmp = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                          encoding="utf-8-sig")
        try:
            tmp.write(script)
            tmp.close()
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", tmp.name],
                check=False, creationflags=flags,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def _say_pyttsx3(self, text: str, rate: int) -> None:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 175 - rate * 12)
        if self.voice:
            for v in engine.getProperty("voices"):
                if self.voice.lower() in (v.name or "").lower():
                    engine.setProperty("voice", v.id)
                    break
        engine.say(text)
        engine.runAndWait()
        engine.stop()

    def _say_say(self, text: str, rate: int) -> None:
        cmd = ["say", "-r", str(max(90, 190 - rate * 12))]
        if self.voice:
            cmd += ["-v", self.voice]
        subprocess.run(cmd + [text], check=False)

    def _say_espeak(self, text: str, rate: int) -> None:
        cmd = getattr(self, "_linux_cmd", "espeak")
        if cmd == "spd-say":
            subprocess.run([cmd, text], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run([cmd, "-s", str(max(80, 165 - rate * 10)), text],
                           check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # -- voice listing -----------------------------------------------------

    def list_voices(self) -> List[str]:
        if self.backend == "piper":
            return [m.stem for m in sorted(VOICES_DIR.glob("*.onnx"))]
        if self.backend == "windows":
            script = (
                "Add-Type -AssemblyName System.Speech\n"
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                "$s.GetInstalledVoices() | "
                "ForEach-Object { $_.VoiceInfo.Name }"
            )
            tmp = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                              encoding="utf-8-sig")
            tmp.write(script)
            tmp.close()
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", tmp.name],
                    capture_output=True, text=True, check=False,
                )
                return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        if self.backend == "pyttsx3":
            import pyttsx3
            engine = pyttsx3.init()
            return [v.name for v in engine.getProperty("voices")]
        if self.backend == "say":
            out = subprocess.run(["say", "-v", "?"], capture_output=True,
                                 text=True, check=False)
            return [ln.split()[0] for ln in out.stdout.splitlines() if ln.strip()]
        return []


# --------------------------------------------------------------------------
# Word bank and progress
# --------------------------------------------------------------------------

def load_words() -> Dict[str, str]:
    """Return {word: example sentence}."""
    if not WORDS_FILE.exists():
        sys.exit(
            f"Word list not found: {WORDS_FILE}\n"
            "words.json must sit in the same folder as spell_trainer.py."
        )
    try:
        with WORDS_FILE.open(encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        sys.exit(
            f"words.json is not valid JSON ({exc}).\n"
            "It was probably saved or edited in a way that damaged it - "
            "download a fresh copy."
        )
    except UnicodeDecodeError as exc:
        sys.exit(f"words.json is not readable as UTF-8 text ({exc}).")
    if not isinstance(data, list) or not data:
        sys.exit("words.json should contain a non-empty list of words.")
    return {entry["w"]: entry.get("s", "") for entry in data
            if isinstance(entry, dict) and entry.get("w")}


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            with PROGRESS_FILE.open(encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "words" in data:
                return data
        except (json.JSONDecodeError, OSError):
            print(f"{C.YELLOW}Progress file was unreadable; starting fresh."
                  f"{C.OFF}")
    return {"version": PROGRESS_VERSION, "words": {}, "sessions": []}


def save_progress(progress: dict) -> None:
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(progress, fh, indent=1, ensure_ascii=False)
    tmp.replace(PROGRESS_FILE)


def record_for(progress: dict, word: str) -> dict:
    return progress["words"].setdefault(
        word, {"correct": 0, "wrong": 0, "retired": False, "last_seen": None}
    )


# --------------------------------------------------------------------------
# Answer checking
# --------------------------------------------------------------------------

def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def show_difference(expected: str, typed: str) -> str:
    """Return a marker line pointing at the first character that diverges."""
    if not typed:
        return ""
    limit = min(len(expected), len(typed))
    pos = limit
    for i in range(limit):
        if expected[i].lower() != typed[i].lower():
            pos = i
            break
    return " " * pos + "^"


# --------------------------------------------------------------------------
# Session selection
# --------------------------------------------------------------------------

def active_words(words: dict, progress: dict) -> List[str]:
    return [w for w in words
            if not progress["words"].get(w, {}).get("retired", False)]


def build_queue(words: dict, progress: dict, count: int) -> List[str]:
    """Pick words for this session, favouring ones that have been missed."""
    pool = active_words(words, progress)
    if not pool:
        return []

    def weight(w: str) -> float:
        rec = progress["words"].get(w)
        if rec is None:
            return 2.0                       # never seen
        # more misses and fewer correct answers -> higher chance of appearing
        return 2.0 + rec["wrong"] * 3.0 - rec["correct"] * 0.5

    weights = [max(0.4, weight(w)) for w in pool]
    chosen: List[str] = []
    remaining = list(pool)
    remaining_w = list(weights)
    target = min(count, len(pool))
    while len(chosen) < target and remaining:
        pick = random.choices(range(len(remaining)), weights=remaining_w, k=1)[0]
        chosen.append(remaining.pop(pick))
        remaining_w.pop(pick)
    return chosen


# --------------------------------------------------------------------------
# Display helpers
# --------------------------------------------------------------------------

def blanked_sentence(sentence: str, word: str) -> str:
    if not sentence:
        return "(no example sentence for this word)"
    return re.sub(re.escape(word), "_" * len(word), sentence, flags=re.IGNORECASE)


def hint_for(word: str) -> str:
    return f"{word[0]}{'_' * (len(word) - 1)}  ({len(word)} letters)"


def banner(speaker: Speaker, words: dict, progress: dict) -> None:
    total = len(words)
    retired = sum(1 for w in words
                  if progress["words"].get(w, {}).get("retired"))
    print(f"\n{C.BOLD}IELTS Spelling Trainer{C.OFF}")
    print(f"{C.GREY}Audio: {speaker.describe()}{C.OFF}")
    if speaker.voice_warning:
        print(f"{C.YELLOW}{speaker.voice_warning}{C.OFF}")
    left = total - retired
    print(f"{C.GREY}Deck: {left} word{'' if left == 1 else 's'} left of "
          f"{total} ({retired} retired){C.OFF}")
    print(f"{C.GREY}Commands: Enter/r=repeat  s=slow  h=hint  c=context  "
          f"k=skip  q=quit{C.OFF}\n")


# --------------------------------------------------------------------------
# The drill
# --------------------------------------------------------------------------

def practise(words: dict, progress: dict, speaker: Speaker,
             count: int, reset_on_miss: bool) -> None:
    queue = build_queue(words, progress, count)
    if not queue:
        print(f"{C.GREEN}Nothing left to practise - every word has been "
              f"spelled correctly {CORRECT_TO_RETIRE} times.{C.OFF}")
        print(f"{C.GREY}Run with --reset to start the deck again, or "
              f"--add-words to load more.{C.OFF}")
        return

    banner(speaker, words, progress)

    asked = right = 0
    newly_retired: List[str] = []
    missed: List[str] = []
    started = time.time()

    for index, word in enumerate(queue, start=1):
        sentence = words[word]
        rec = record_for(progress, word)

        print(f"{C.BOLD}[{index}/{len(queue)}]{C.OFF} ", end="")
        if rec["wrong"]:
            print(f"{C.GREY}(missed {rec['wrong']}x, "
                  f"{rec['correct']}/{CORRECT_TO_RETIRE} correct){C.OFF}")
        else:
            print()

        if speaker.available:
            speaker.say(word)
        else:
            # No audio: give the sentence with a blank instead of a recording.
            print(f"    {C.BLUE}{blanked_sentence(sentence, word)}{C.OFF}")

        answer = None
        revealed_hint = False
        while answer is None:
            try:
                raw = input("    > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raw = "q"

            cmd = raw.lower()
            if not raw:
                # Enter with nothing typed = "say it again" (same as 'r'),
                # so you never have to remember a key just to re-listen.
                speaker.say(word)
                if not speaker.available:
                    print(f"    {C.BLUE}"
                          f"{blanked_sentence(sentence, word)}{C.OFF}")
                continue
            if cmd == "q":
                finish(progress, asked, right, newly_retired, missed, started)
                return
            if cmd == "r":
                speaker.say(word)
                if not speaker.available:
                    print(f"    {C.BLUE}"
                          f"{blanked_sentence(sentence, word)}{C.OFF}")
                continue
            if cmd == "s":
                speaker.say(word, slow=True)
                continue
            if cmd == "h":
                revealed_hint = True
                print(f"    {C.YELLOW}{hint_for(word)}{C.OFF}")
                continue
            if cmd == "c":
                revealed_hint = True
                print(f"    {C.BLUE}{blanked_sentence(sentence, word)}{C.OFF}")
                continue
            if cmd == "k":
                answer = ""
                break
            answer = raw

        asked += 1
        rec["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        is_right = normalise(answer) == normalise(word)

        if is_right:
            right += 1
            rec["correct"] += 1
            note = " (hint used)" if revealed_hint else ""
            print(f"    {C.GREEN}Correct{C.OFF}  {C.BOLD}{word}{C.OFF}"
                  f"{C.GREY}{note}{C.OFF}")
            if rec["correct"] >= CORRECT_TO_RETIRE and not rec["retired"]:
                rec["retired"] = True
                newly_retired.append(word)
                print(f"    {C.GREEN}Retired - spelled correctly "
                      f"{CORRECT_TO_RETIRE} times.{C.OFF}")
            else:
                left = CORRECT_TO_RETIRE - rec["correct"]
                print(f"    {C.GREY}{left} more correct to retire this word."
                      f"{C.OFF}")
        else:
            rec["wrong"] += 1
            missed.append(word)
            if reset_on_miss:
                rec["correct"] = 0
            pad = " " * 18
            if answer == "":
                print(f"    {C.YELLOW}Skipped{C.OFF}")
                print(f"      correct     {C.BOLD}{word}{C.OFF}")
            else:
                print(f"    {C.RED}Wrong{C.OFF}")
                print(f"      you typed   {answer}")
                marker = show_difference(word, answer)
                if marker:
                    print(f"{pad}{C.GREY}{marker}{C.OFF}")
                print(f"      correct     {C.BOLD}{word}{C.OFF}")
            print(f"    {C.GREY}missed {rec['wrong']}x in total{C.OFF}")

            # Make a genuine miss (not a skip) "stick": retype the correct
            # spelling before moving on. Doesn't change the score - that's
            # already recorded above - it's just muscle memory.
            if answer != "":
                while True:
                    try:
                        confirm = input(
                            f"    type {C.BOLD}{word}{C.OFF} to continue > "
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        confirm = "q"
                    ccmd = confirm.lower()
                    if ccmd == "q":
                        finish(progress, asked, right, newly_retired, missed,
                               started)
                        return
                    if not confirm or ccmd == "r":
                        speaker.say(word)
                        continue
                    if ccmd == "s":
                        speaker.say(word, slow=True)
                        continue
                    if normalise(confirm) == normalise(word):
                        print(f"    {C.GREEN}Locked in.{C.OFF}")
                        break
                    print(f"    {C.RED}Not quite - try again.{C.OFF} "
                          f"{C.GREY}(correct: {word}){C.OFF}")

        if sentence:
            print(f"    {C.BLUE}{sentence}{C.OFF}")
        print()
        save_progress(progress)

    finish(progress, asked, right, newly_retired, missed, started)


def finish(progress: dict, asked: int, right: int,
           newly_retired: List[str], missed: List[str], started: float) -> None:
    if asked:
        progress.setdefault("sessions", []).append({
            "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "asked": asked,
            "correct": right,
            "seconds": int(time.time() - started),
        })
    save_progress(progress)

    print(f"{C.BOLD}Session summary{C.OFF}")
    if not asked:
        print("  No words attempted.")
        return
    pct = right * 100 // asked
    print(f"  Score      {right}/{asked}  ({pct}%)")
    print(f"  Time       {int(time.time() - started) // 60} min "
          f"{int(time.time() - started) % 60} s")
    if newly_retired:
        print(f"  {C.GREEN}Retired    " + ", ".join(newly_retired) + C.OFF)
    if missed:
        seen = sorted(set(missed))
        print(f"  {C.RED}To review  " + ", ".join(seen) + C.OFF)
    print()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def show_stats(words: dict, progress: dict) -> None:
    recs = progress["words"]
    total = len(words)
    seen = [w for w in words if w in recs]
    retired = [w for w in words if recs.get(w, {}).get("retired")]
    attempts = sum(r["correct"] + r["wrong"] for r in recs.values())
    correct = sum(r["correct"] for r in recs.values())

    print(f"\n{C.BOLD}Progress{C.OFF}")
    print(f"  Words in deck      {total}")
    print(f"  Practised          {len(seen)}")
    print(f"  Retired            {len(retired)}")
    print(f"  Still to master    {total - len(retired)}")
    if attempts:
        print(f"  Lifetime accuracy  {correct * 100 // attempts}% "
              f"({correct}/{attempts})")

    sessions = progress.get("sessions", [])
    if sessions:
        print(f"\n{C.BOLD}Recent sessions{C.OFF}")
        for s in sessions[-5:]:
            when = s["when"].replace("T", " ").replace("+00:00", " UTC")
            print(f"  {when}   {s['correct']}/{s['asked']}")

    troublesome = sorted(
        ((w, r) for w, r in recs.items() if r["wrong"] > 0),
        key=lambda kv: (-kv[1]["wrong"], kv[0]),
    )[:15]
    if troublesome:
        print(f"\n{C.BOLD}Most misspelled{C.OFF}")
        for w, r in troublesome:
            flag = f" {C.GREY}(retired){C.OFF}" if r["retired"] else ""
            print(f"  {r['wrong']:>3}x  {w}{flag}")
        all_missed = sum(1 for r in recs.values() if r["wrong"] > 0)
        if all_missed > len(troublesome):
            print(f"  {C.GREY}...and {all_missed - len(troublesome)} more. "
                  f"Run --missed to see all of them, or --export-missed FILE "
                  f"to save the list.{C.OFF}")
    print()


def _missed_words(progress: dict) -> List[tuple]:
    """[(word, record), ...] for every word ever misspelled, worst first."""
    recs = progress["words"]
    return sorted(
        ((w, r) for w, r in recs.items() if r["wrong"] > 0),
        key=lambda kv: (-kv[1]["wrong"], kv[0]),
    )


def show_missed(progress: dict) -> None:
    """Full list of every word ever misspelled (--stats only shows top 15)."""
    troublesome = _missed_words(progress)
    if not troublesome:
        print(f"\n{C.GREY}No misspellings recorded yet.{C.OFF}\n")
        return
    print(f"\n{C.BOLD}Every word you've ever misspelled ({len(troublesome)}){C.OFF}")
    for w, r in troublesome:
        flag = f" {C.GREY}(retired){C.OFF}" if r["retired"] else ""
        print(f"  {r['wrong']:>3}x  {w}{flag}")
    print()


def export_missed(words: dict, progress: dict, path: str) -> None:
    """Write every misspelled word + its example sentence to a text file."""
    troublesome = _missed_words(progress)
    if not troublesome:
        print("No misspellings recorded yet - nothing to export.")
        return
    out = Path(path)
    lines = [f"{w} | {words.get(w, '')}" for w, _r in troublesome]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(troublesome)} misspelled word(s) to {out}")
    print("That's the same format --add-words reads, so you can hand this "
          "file to someone else, or re-merge it into a fresh deck with:")
    print(f'  python "spell trainer.py" --add-words "{out}"')


def doctor(rate: int, voice: Optional[str]) -> None:
    """Print everything needed to diagnose a broken setup."""
    print("\n=== IELTS Spelling Trainer: self-check ===")
    print(f"Python      {sys.version.split()[0]}  ({sys.executable})")
    print(f"Platform    {platform.system()} {platform.release()} "
          f"({platform.machine()})")
    print(f"Console     encoding={sys.stdout.encoding}  "
          f"tty={sys.stdout.isatty()}")
    print(f"App folder  {APP_DIR}")

    print(f"\nwords.json  {'found' if WORDS_FILE.exists() else 'MISSING'}")
    if WORDS_FILE.exists():
        print(f"            size {WORDS_FILE.stat().st_size} bytes")
        try:
            words = load_words()
            print(f"            OK - {len(words)} words loaded")
        except Exception as exc:
            print(f"            FAILED TO PARSE: {type(exc).__name__}: {exc}")

    print(f"progress.json {'found' if PROGRESS_FILE.exists() else 'not yet created'}")
    if PROGRESS_FILE.exists():
        try:
            prog = load_progress()
            print(f"            OK - {len(prog['words'])} words tracked")
        except Exception as exc:
            print(f"            FAILED TO PARSE: {type(exc).__name__}: {exc}")

    try:
        piper_installed = importlib.util.find_spec("piper") is not None
    except (ImportError, ValueError):
        piper_installed = False
    models = sorted(VOICES_DIR.glob("*.onnx")) if VOICES_DIR.is_dir() else []
    print(f"\npiper       package {'installed' if piper_installed else 'not installed'}"
          f"  ({'pip install piper-tts' if not piper_installed else 'ok'})")
    print(f"            voice model(s) in {VOICES_DIR}: "
          + (", ".join(m.stem for m in models) if models else "none"))

    print(f"\npowershell  {shutil.which('powershell') or 'not on PATH'}")
    sp = Speaker(rate, voice)
    print(f"Backend     {sp.describe()}")
    if sp.voice_warning:
        print(f"WARNING     {sp.voice_warning}")
    voices = sp.list_voices()
    if voices:
        print("Voices      " + "\n            ".join(voices))
    else:
        print("Voices      none reported")

    if sp.available:
        print("\nSpeaking a test word now - you should hear 'testing'...")
        start = time.time()
        sp.say("testing")
        print(f"Speech call returned after {time.time() - start:.1f}s.")
        print("If you heard nothing, check the volume and your default "
              "playback device.")
    else:
        print("\nNo speech backend, so the app will run in written-clue mode.")
    print()


def show_list(words: dict, progress: dict) -> None:
    recs = progress["words"]
    remaining, retired = [], []
    for w in sorted(words):
        r = recs.get(w)
        if r and r["retired"]:
            retired.append(f"{w} ({r['wrong']} misses)")
        else:
            got = r["correct"] if r else 0
            remaining.append(f"{w} [{got}/{CORRECT_TO_RETIRE}]")
    print(f"\n{C.BOLD}Still in the deck ({len(remaining)}){C.OFF}")
    for line in remaining:
        print(f"  {line}")
    if retired:
        print(f"\n{C.BOLD}Retired ({len(retired)}){C.OFF}")
        for line in retired:
            print(f"  {C.GREY}{line}{C.OFF}")
    print()


# --------------------------------------------------------------------------
# Word bank editing
# --------------------------------------------------------------------------

def add_words(path: str) -> None:
    """Merge extra words into words.json.

    Accepts one word per line. An optional example sentence can follow the
    word after a | character:   accommodation | The accommodation was cheap.
    """
    src = Path(path)
    if not src.exists():
        sys.exit(f"File not found: {src}")

    with WORDS_FILE.open(encoding="utf-8-sig") as fh:
        data = json.load(fh)
    existing = {e["w"].lower() for e in data}

    added = 0
    for line in src.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            word, sentence = (p.strip() for p in line.split("|", 1))
        else:
            word, sentence = line, ""
        if not word or word.lower() in existing:
            continue
        data.append({"w": word, "s": sentence})
        existing.add(word.lower())
        added += 1

    with WORDS_FILE.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=0, ensure_ascii=False)
    print(f"Added {added} new word(s). Deck is now {len(data)} words.")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Dictation-style spelling practice for IELTS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-n", "--count", type=int, default=20,
                   help="how many words in this session (default 20)")
    p.add_argument("--rate", type=int, default=0,
                   help="speech speed, -10 (fast) to 10 (slow); default 0")
    p.add_argument("--voice", type=str, default=None,
                   help="voice name, e.g. 'Microsoft Hazel Desktop'")
    p.add_argument("--voices", action="store_true",
                   help="list the voices installed on this machine and exit")
    p.add_argument("--doctor", action="store_true",
                   help="run a self-check and print diagnostics, then exit")
    p.add_argument("--stats", action="store_true", help="show progress and exit")
    p.add_argument("--list", action="store_true",
                   help="list remaining and retired words and exit")
    p.add_argument("--missed", action="store_true",
                   help="list every word you've ever misspelled and exit")
    p.add_argument("--export-missed", metavar="FILE",
                   help="write every misspelled word + example sentence to a "
                        "text file and exit")
    p.add_argument("--add-words", metavar="FILE",
                   help="merge extra words from a text file into the deck")
    p.add_argument("--reset", action="store_true",
                   help="erase all progress and put every word back in the deck")
    p.add_argument("--keep-streak", action="store_true",
                   help="a wrong answer no longer resets that word's count")
    p.add_argument("--no-color", action="store_true", help="plain text output")
    # parse_known_args, not parse_args: IDEs such as Spyder, IDLE and Jupyter
    # inject their own switches into sys.argv, which would otherwise make the
    # app exit with "unrecognized arguments" before it ever starts.
    args, unknown = p.parse_known_args()
    if unknown:
        print(f"(ignoring unrecognised argument(s): {' '.join(unknown)})")

    if args.no_color or not sys.stdout.isatty():
        C.disable()
    else:
        enable_ansi()

    if args.doctor:
        doctor(args.rate, args.voice)
        return

    if args.add_words:
        add_words(args.add_words)
        return

    if args.voices:
        sp = Speaker(args.rate, args.voice)
        print(f"Speech backend: {sp.describe()}")
        voices = sp.list_voices()
        if voices:
            print("Installed voices:")
            for v in voices:
                print(f"  {v}")
            print("\nUse one with:  --voice \"<name>\"")
        else:
            print("No selectable voices reported.")
        return

    words = load_words()

    if args.reset:
        confirm = input("Erase all progress? Type yes to confirm: ").strip()
        if confirm.lower() == "yes":
            if PROGRESS_FILE.exists():
                PROGRESS_FILE.unlink()
            print("Progress erased.")
        else:
            print("Cancelled.")
        return

    progress = load_progress()

    if args.stats:
        show_stats(words, progress)
        return
    if args.list:
        show_list(words, progress)
        return
    if args.missed:
        show_missed(progress)
        return
    if args.export_missed:
        export_missed(words, progress, args.export_missed)
        return

    speaker = Speaker(args.rate, args.voice)
    practise(words, progress, speaker, args.count,
             reset_on_miss=not args.keep_streak)
    show_stats(words, progress)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except BrokenPipeError:
        pass
