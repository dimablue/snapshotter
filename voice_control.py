"""Opt-in microphone streaming to ElevenLabs Scribe Realtime."""
import base64
import json
import os
import queue
import re
import subprocess
import threading
import time


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
        if words[i] == "neon" or words[i:i + 2] in WAKE:
            # "hey Neon ... take a screenshot": the capture phrase becomes "start". Only "neon"
            # is required: speech recognition often hears "hey" as "a" or drops it.
            j = i + 1 if words[i] == "neon" else i + 2
            started = False
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
                i = j if words[i] == "neon" else i + 2
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


KEYCHAIN_SERVICE = "gazesnap-elevenlabs"


def get_api_key():
    """$ELEVENLABS_API_KEY, else the key saved in the macOS Keychain (see README)."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if key:
        return key
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


class VoiceError(RuntimeError):
    """An ElevenLabs error whose message is safe and useful to show as-is."""


class VoiceControl:
    def __init__(self, events, mic=None, skip_s=0.0,
                 ready="listening — say “hey Neon, take a screenshot”"):
        """`mic`: a shared mic.Microphone (else opens its own stream). `skip_s`: drop this much
        audio at the start, e.g. the tail of the wake phrase that started the session.
        `ready`: status shown once ElevenLabs is listening."""
        self.events = events
        self.ready = ready
        self.mic = mic
        self.skip_s = skip_s
        self._stop = threading.Event()
        self._thread = None
        self._socket = None
        self.fired = []  # commands already sent for the sentence being spoken
        self.reply_token = None
        self._output_muted = False
        self._resume_at = 0.0
        self._audio_epoch = 0

    def set_reply_mode(self, token):
        self.reply_token = token
        self.fired = []

    def set_output_muted(self, muted):
        self._audio_epoch += 1
        self._output_muted = muted
        self._resume_at = float('inf') if muted else time.monotonic() + 0.5
        self.fired = []

    def status(self, text):
        self.events.put(("status", "voice: " + text))

    def handle_message(self, message):
        if self._stop.is_set():
            return
        kind = message.get("message_type", "")
        if kind == "session_started":
            self.status(self.ready)
        elif kind in ("partial_transcript", "committed_transcript"):
            if self._output_muted or time.monotonic() < self._resume_at:
                self.fired = []
                return
            final = kind == "committed_transcript"
            text = message.get("text", "").strip()
            if self.reply_token is not None:
                # Free speech is committed once, never acted on from partial transcripts.
                if final and text:
                    self.events.put(('reply', self.reply_token, text))
                return
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

    def _audio(self, on_audio):
        """Context manager feeding mic blocks to on_audio(bytes)."""
        import contextlib

        if self.mic is not None:
            @contextlib.contextmanager
            def shared():
                self.mic.subscribe(on_audio)
                try:
                    yield
                finally:
                    self.mic.unsubscribe(on_audio)
            return shared()
        import sounddevice as sd
        return sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1600,
                                 callback=lambda data, frames, timing, status: on_audio(bytes(data)))

    def run(self):
        try:
            key = get_api_key()
            if not key:
                self.status("off — set ELEVENLABS_API_KEY (or save it in the Keychain) and restart")
                return
            from websockets.sync.client import connect
            from image_analyzer import tls_context

            chunks = queue.Queue(maxsize=200)  # 10 s, so speech during the connect isn't lost
            overflow = threading.Event()
            listen_from = time.monotonic() + self.skip_s

            def on_audio(chunk):
                if self._output_muted or time.monotonic() < max(listen_from, self._resume_at):
                    return
                try:
                    chunks.put_nowait((self._audio_epoch, chunk))
                except queue.Full:
                    overflow.set()

            url = ("wss://api.elevenlabs.io/v1/speech-to-text/realtime"
                   "?model_id=scribe_v2_realtime&audio_format=pcm_16000"
                   "&commit_strategy=vad&language_code=en")
            # record before connecting; buffered audio is sent once the socket is open
            with self._audio(on_audio):
                self.status("connecting to ElevenLabs; microphone audio will be streamed")
                with connect(url, ssl=tls_context(), additional_headers={"xi-api-key": key}, open_timeout=10,
                             close_timeout=1) as ws:
                    self._socket = ws
                    while not self._stop.is_set():
                        if overflow.is_set():
                            raise RuntimeError("microphone stream fell behind; restart voice control")
                        try:
                            epoch, chunk = chunks.get(timeout=0.02)
                            if epoch != self._audio_epoch or self._output_muted:
                                chunk = None
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
