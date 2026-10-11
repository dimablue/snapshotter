"""Offline, free wake phrase: "hey Neon, take a screenshot", and the selection commands.

Vosk recognizes speech locally, restricted to a few phrases, so it is fast (the phrase
usually fires before you finish saying it) and nothing leaves the computer. Snapshotter only
connects to ElevenLabs after this fires.

While a selection is open ("selecting"), "capture", "retry" and "cancel" are recognized here
too: locally they fire ~0.35 s into the word, well before ElevenLabs answers, and they work
before ElevenLabs has even connected. The app drops the slower duplicate from ElevenLabs.
"""
import json
import queue
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from mic import RATE
from voice_control import commands_in

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_DIR = Path.home() / ".cache" / "snapshotter"
# Restricting Vosk to these phrases is what makes it accurate; anything else becomes [unk].
GRAMMAR = ["hey neon take a screenshot", "hey neon take screenshot", "hey neon screenshot",
           "okay neon take a screenshot", "ok neon take a screenshot", "neon take a screenshot",
           "hey neon",
           # selection commands (only acted on while a selection is open)
           "capture", "capture this", "capture it", "snap", "snap it", "screenshot", "take a screenshot",
           "retry", "redo", "try again", "cancel",
           "[unk]"]
SELECTION_COMMANDS = {"capture", "r", "cancel"}


def model_path(status):
    path = MODEL_DIR / MODEL_NAME
    if not path.exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        status("downloading the offline speech model (40 MB, once)…")
        archive = MODEL_DIR / f"{MODEL_NAME}.zip"
        from image_analyzer import tls_context
        with urllib.request.urlopen(MODEL_URL, context=tls_context(), timeout=60) as response, archive.open('wb') as out:
            shutil.copyfileobj(response, out)
        with zipfile.ZipFile(archive) as z:
            z.extractall(MODEL_DIR)
        archive.unlink()
    return path


def new_commands(cmds, fired):
    """Commands in `cmds` not already sent from an earlier partial result of the same utterance."""
    k = 0
    while k < min(len(cmds), len(fired)) and cmds[k] == fired[k]:
        k += 1
    return cmds[k:]


class WakeListener:
    def __init__(self, events, mic):
        self.events = events
        self.mic = mic
        self.paused = False  # set from the menu bar; audio is still read but ignored
        self.selecting = False  # set by the app while a selection is open
        self._output_muted = False
        self._resume_at = 0.0
        self._audio_epoch = 0
        self._stop = threading.Event()
        self._thread = None

    def status(self, text):
        self.events.put(("status", "wake: " + text))

    def set_output_muted(self, muted):
        """Drop speaker audio without changing the user's Pause listening preference."""
        self._audio_epoch += 1
        self._output_muted = muted
        self._resume_at = float('inf') if muted else time.monotonic() + 0.5

    def start(self):
        self._thread = threading.Thread(target=self.run, name="wake", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def run(self):
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError:
            self.status("off — install requirements-voice.txt (needs vosk)")
            return
        try:
            SetLogLevel(-1)
            recognizer = KaldiRecognizer(Model(str(model_path(self.status))), RATE, json.dumps(GRAMMAR))
        except Exception as exc:
            self.status(f"off — could not load the offline speech model ({type(exc).__name__})")
            return
        chunks = queue.Queue(maxsize=100)

        def on_audio(chunk):
            if self.paused or self._output_muted or time.monotonic() < self._resume_at:
                return
            try:
                chunks.put_nowait((self._audio_epoch, chunk))
            except queue.Full:
                pass  # fell behind; dropping audio only delays the wake phrase

        self.mic.subscribe(on_audio)
        self.status("listening offline for “hey Neon, take a screenshot”")
        fired = []  # commands already sent for the utterance being recognized
        epoch = self._audio_epoch
        try:
            while not self._stop.is_set():
                try:
                    chunk_epoch, chunk = chunks.get(timeout=0.1)
                except queue.Empty:
                    continue
                if epoch != self._audio_epoch:
                    recognizer.Reset()
                    fired = []
                    epoch = self._audio_epoch
                if chunk_epoch != epoch or self.paused or self._output_muted or time.monotonic() < self._resume_at:
                    continue
                done = recognizer.AcceptWaveform(chunk)
                result = json.loads(recognizer.Result() if done else recognizer.PartialResult())
                text = (result.get("text") if done else result.get("partial")) or ""
                # fire on the partial result, as soon as the phrase is complete
                cmds = [c for c in commands_in(text.replace("[unk]", " "), final=done)
                        if c == "start" or (self.selecting and c in SELECTION_COMMANDS)]
                if not self.paused:
                    for cmd in new_commands(cmds, fired):
                        self.events.put(("cmd", cmd))
                fired = [] if done else max(cmds, fired, key=len)
                if done:
                    if text.replace("[unk]", "").strip():  # for the log only; not shown on screen
                        self.events.put(("status", f"wake-heard: “{text}”"))
        finally:
            self.mic.unsubscribe(on_audio)
