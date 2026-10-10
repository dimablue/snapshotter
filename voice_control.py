"""Opt-in microphone streaming to ElevenLabs Scribe Realtime."""
import base64
import json
import os
import queue
import re
import threading
import time


CAPTURE = {"take a screenshot", "take a screenshot please", "please take a screenshot",
           "capture this", "capture this please", "screenshot", "snap now"}
RETRY = {"retry", "try again", "redo", "start over"}
CANCEL = {"cancel", "cancel crop"}


def voice_command(text):
    """App command for an utterance: "" snap/save, "r" retry crop, "cancel", or None."""
    # Exact utterances avoid triggering on negations or discussion of screenshots.
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    normalized = " ".join(words)
    if normalized in CAPTURE:
        return ""
    if normalized in RETRY:
        return "r"
    if normalized in CANCEL:
        return "cancel"
    return None


def is_capture_command(text):
    return voice_command(text) == ""


class VoiceError(RuntimeError):
    """An ElevenLabs error whose message is safe and useful to show as-is."""


class VoiceControl:
    def __init__(self, events):
        self.events = events
        self._stop = threading.Event()
        self._thread = None
        self._socket = None
        self.last_capture = -float("inf")

    def status(self, text):
        self.events.put(("status", "voice: " + text))

    def handle_message(self, message):
        kind = message.get("message_type", "")
        if kind == "session_started":
            self.status('listening — say “take a screenshot”')
        elif kind == "committed_transcript":
            now = time.monotonic()
            text = message.get("text", "").strip()
            cmd = voice_command(text)
            if text:
                self.status(f"heard “{text}”" + ("" if cmd is not None else " (not a command)"))
            if cmd is not None and now - self.last_capture >= 1.5:
                self.last_capture = now
                self.events.put(("cmd", cmd))
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
