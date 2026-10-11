"""One microphone stream shared by the offline wake listener and ElevenLabs.

macOS refuses a second input stream on the same device from one process, so both
listeners subscribe here instead of opening their own. Audio is 16 kHz mono int16.
"""
import threading

RATE = 16000
BLOCK = 800  # 50 ms: small blocks keep wake detection snappy


class Microphone:
    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()
        self._stream = None

    def subscribe(self, callback):
        """callback(bytes) is called from the audio thread for every block; keep it quick."""
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _on_audio(self, data, frames, timing, status):
        chunk = bytes(data)
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            callback(chunk)

    def start(self):
        import sounddevice as sd

        if self._stream is None:
            self._stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                                             blocksize=BLOCK, callback=self._on_audio)
            self._stream.start()

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
