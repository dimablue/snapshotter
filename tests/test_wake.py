"""Offline wake phrase on real speech (macOS `say`), streamed in mic-sized blocks.

Skipped without vosk, its model (downloaded on first --wake run) or the `say` command.
"""
import queue
import shutil
import subprocess
import time
import wave

import pytest

wake = pytest.importorskip("wake")
pytest.importorskip("vosk")
if shutil.which("say") is None or not (wake.MODEL_DIR / wake.MODEL_NAME).exists():
    pytest.skip("needs macOS `say` and the offline model", allow_module_level=True)


class FakeMic:
    def __init__(self):
        self.subscribers = []

    def subscribe(self, cb):
        self.subscribers.append(cb)

    def unsubscribe(self, cb):
        self.subscribers.remove(cb)

    def play(self, pcm, block=1600):
        for i in range(0, len(pcm), block):
            for cb in list(self.subscribers):
                cb(pcm[i:i + block])
            time.sleep(0.05)  # real time: 800 samples (1600 bytes) every 50 ms


def speech(tmp_path, text):
    path = tmp_path / "speech.wav"
    subprocess.run(["say", "-v", "Samantha", "-o", str(path), "--data-format=LEI16@16000", text], check=True)
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes()) + b"\0" * 32000  # plus 1 s of silence


def listen(tmp_path, text):
    events, mic = queue.Queue(), FakeMic()
    listener = wake.WakeListener(events, mic)
    listener.start()
    for _ in range(100):  # wait for the model to load and subscribe
        if mic.subscribers:
            break
        time.sleep(0.05)
    mic.play(speech(tmp_path, text))
    time.sleep(0.3)
    listener.stop()
    return [ev for ev in events.queue if ev[0] == "cmd"]


def test_wake_phrase_fires_once(tmp_path):
    assert listen(tmp_path, "hey neon, take a screenshot") == [("cmd", "start")]


@pytest.mark.parametrize("text", ["hey siri, what time is it", "I had to cancel the meeting",
                                  "take a screenshot", "hey neon"])
def test_other_speech_does_not_wake(tmp_path, text):
    assert listen(tmp_path, text) == []
