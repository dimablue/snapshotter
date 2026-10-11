"""Opt-in microphone streaming to ElevenLabs Scribe Realtime."""
import base64
import json
import os
import queue
import re
import threading


# Voice commands sent to the app as ("cmd", name):
#   "start"    "hey Neon, take a screenshot": start a selection at the current gaze
#   "capture"  take the selection being dragged
#   "r"        retry the selection        "cancel"  drop it
# Commands are acted on from the live (partial) transcript, as soon as they are heard.
# Capture words and all phrases count anywhere. "retry", "redo" and "cancel" on their own
# only count as the last word of a finished sentence: mid-sentence, "I had to cancel..."
# briefly ends in "cancel".
WAKE = [["hey", "neon"], ["hi", "neon"], ["ok", "neon"], ["okay", "neon"]]
PHRASES = {
    "capture": ["take a screenshot", "take the screenshot", "take screenshot",
                "capture this", "capture it", "snap now", "snap it"],
    "r": ["try again", "start over"],
    "cancel": ["cancel crop"],
}
ANYWHERE_WORDS = {"screenshot": "capture", "capture": "capture", "snap": "capture"}
LAST_WORDS = {"retry": "r", "redo": "r", "cancel": "cancel"}
NEGATIONS = {"dont", "not", "never", "no", "stop"}


def _match(words, i, final):
    """(command, length) for a command starting at words[i], (None, length) if it is
    negated ("don't take a screenshot"), or None if nothing matches there."""
    negated = i > 0 and words[i - 1] in NEGATIONS
    for cmd, phrases in PHRASES.items():
        for p in phrases:
            n = len(p.split())
            if words[i:i + n] == p.split():
                return (None if negated else cmd), n
    cmd = ANYWHERE_WORDS.get(words[i])
    if cmd is None and final and i == len(words) - 1:
        cmd = LAST_WORDS.get(words[i])
    if cmd is not None:
        return (None if negated else cmd), 1
    return None


def commands_in(text, final=True):
    """Commands in an utterance, in the order spoken. `final` is False for a partial
    transcript, which may still grow."""
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    out, i = [], 0
    while i < len(words):
        if words[i:i + 2] in WAKE:
            # "hey Neon ... take a screenshot": the capture phrase becomes "start"
            j, started = i + 2, False
            while j < len(words):
                m = _match(words, j, final)
                if m and m[0] == "capture":
                    out.append("start")
                    i, started = j + m[1], True
                    break
                if m and m[0] is not None:
                    break  # another command, e.g. "hey Neon, cancel"
                j += m[1] if m else 1
            if not started:
                i += 2
            continue
        m = _match(words, i, final)
        if m:
            if m[0] is not None:
                out.append(m[0])
            i += m[1]
        else:
            i += 1
    return out


def voice_command(text):
    """First command in a finished utterance, or None."""
    cmds = commands_in(text)
    return cmds[0] if cmds else None


def is_capture_command(text):
    return voice_command(text) == "capture"


class VoiceError(RuntimeError):
    """An ElevenLabs error whose message is safe and useful to show as-is."""


class VoiceControl:
    def __init__(self, events):
        self.events = events
        self._stop = threading.Event()
        self._thread = None
        self._socket = None
        self.fired = []  # commands already sent for the sentence being spoken

    def status(self, text):
        self.events.put(("status", "voice: " + text))

    def handle_message(self, message):
        kind = message.get("message_type", "")
        if kind == "session_started":
            self.status('listening — say “hey Neon, take a screenshot”')
        elif kind in ("partial_transcript", "committed_transcript"):
            final = kind == "committed_transcript"
            text = message.get("text", "").strip()
            cmds = commands_in(text, final)
            # send only commands not already sent from an earlier partial of this sentence
            k = 0
            while k < min(len(cmds), len(self.fired)) and cmds[k] == self.fired[k]:
                k += 1
            for cmd in cmds[k:]:
                self.events.put(("cmd", cmd))
            if cmds[k:] and not final:
                self.status(f"heard “{text}…”")
            self.fired = [] if final else cmds
            if final and text:
                self.status(f"heard “{text}”" + ("" if cmds else " (not a command)"))
        elif kind == "auth_error":
            # Forget the rejected key so `v` asks for a new one.
            os.environ.pop("ELEVENLABS_API_KEY", None)
            raise VoiceError("ElevenLabs rejected the API key. Type v + Enter to enter another "
                             "(it needs Speech to Text access)")
        elif kind == "error" or kind.endswith("_error"):
            # Do not log arbitrary remote payloads or credentials.
            raise VoiceError(f"ElevenLabs {kind}; check your key, quota and account access")

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        self._thread = threading.Thread(target=self.run, name="voice-control", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=2)

    def run(self):
        try:
            key = os.environ.get("ELEVENLABS_API_KEY")
            if not key:
                self.status("off — set ELEVENLABS_API_KEY and restart with --voice-control")
                return
            import sounddevice as sd
            from websockets.sync.client import connect

            chunks = queue.Queue(maxsize=20)
            overflow = threading.Event()

            def audio_callback(data, frames, timing, status):
                if status:
                    overflow.set()
                try:
                    chunks.put_nowait(bytes(data))
                except queue.Full:
                    overflow.set()

            url = ("wss://api.elevenlabs.io/v1/speech-to-text/realtime"
                   "?model_id=scribe_v2_realtime&audio_format=pcm_16000"
                   "&commit_strategy=vad&language_code=en")
            self.status("connecting to ElevenLabs; microphone audio will be streamed")
            with connect(url, additional_headers={"xi-api-key": key}, open_timeout=10,
                         close_timeout=1) as ws:
                self._socket = ws
                if self._stop.is_set():
                    return
                with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                       blocksize=1600, callback=audio_callback):
                    while not self._stop.is_set():
                        if overflow.is_set():
                            raise RuntimeError("microphone stream fell behind; restart voice control")
                        try:
                            chunk = chunks.get(timeout=0.05)
                        except queue.Empty:
                            chunk = None
                        if chunk is not None:
                            ws.send(json.dumps({"message_type": "input_audio_chunk",
                                                "audio_base_64": base64.b64encode(chunk).decode(),
                                                "sample_rate": 16000}))
                        for _ in range(20):
                            try:
                                message = ws.recv(timeout=0)
                            except TimeoutError:
                                break
                            self.handle_message(json.loads(message))
        except ImportError:
            self.status("off — install requirements-voice.txt")
        except VoiceError as exc:
            if not self._stop.is_set():
                self.status(f"off — {exc}")
        except Exception as exc:
            if not self._stop.is_set():
                self.status(f"off ({type(exc).__name__}); check microphone permission, network, API key and quota")
        finally:
            self._socket = None
