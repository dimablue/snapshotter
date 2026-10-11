"""Bounded, cancellable mono PCM playback; no microphone required."""
import threading
import time
from collections import deque


class AudioPlayer:
    def __init__(self, sample_rate=16000, max_seconds=60):
        self.sample_rate = sample_rate
        self.max_bytes = sample_rate * 2 * max_seconds
        self._chunks = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self._stream = None
        self._cancelled = threading.Event()

    def _fill(self, output, frames, timing, status):
        target = memoryview(output).cast('B')
        target[:] = b'\0' * len(target)
        with self._lock:
            if self._cancelled.is_set():
                return
            offset = 0
            while self._chunks and offset < len(target):
                chunk = self._chunks.popleft()
                n = min(len(chunk), len(target) - offset)
                target[offset:offset+n] = chunk[:n]
                if n < len(chunk):
                    self._chunks.appendleft(chunk[n:])
                self._bytes -= n
                offset += n

    def feed(self, pcm):
        if self._cancelled.is_set():
            return
        if len(pcm) % 2:
            raise ValueError('invalid PCM sample length')
        with self._lock:
            if self._cancelled.is_set():
                return
            if self._bytes + len(pcm) > self.max_bytes:
                raise RuntimeError('audio buffer limit exceeded')
            self._chunks.append(pcm)
            self._bytes += len(pcm)
        if self._stream is None:
            import sounddevice as sd
            self._stream = sd.RawOutputStream(samplerate=self.sample_rate, channels=1,
                dtype='int16', blocksize=800, callback=self._fill)
            self._stream.start()

    def drain(self, cancelled, timeout=65):
        deadline = time.monotonic() + timeout
        while not cancelled() and not self._cancelled.is_set():
            with self._lock:
                remaining = self._bytes
            if not remaining:
                # Account for the last callback block and output-device latency.
                latency = float(self._stream.latency) if self._stream else 0
                end = time.monotonic() + max(0.1, latency + 0.05)
                while time.monotonic() < end and not cancelled():
                    time.sleep(0.02)
                return
            if time.monotonic() > deadline:
                raise TimeoutError('audio playback timed out')
            time.sleep(0.02)

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            self._chunks.clear()
            self._bytes = 0

    def close(self):
        self.cancel()
        if self._stream:
            self._stream.abort()
            self._stream.close()
            self._stream = None
