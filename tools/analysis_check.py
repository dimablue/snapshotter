#!/usr/bin/env python3
"""Analyze a PNG and play its speech without opening the microphone or capturing the screen."""
import argparse
import getpass
import json
import queue
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_analyzer import ElevenLabsAnalyzer, agent_id
from audio_player import AudioPlayer
from voice_control import get_api_key


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('image', type=Path)
    p.add_argument('--key-stdin', action='store_true')
    p.add_argument('--save-audio', type=Path, help='also save the returned PCM as a WAV')
    args = p.parse_args()
    key = sys.stdin.readline().strip() if args.key_stdin else get_api_key()
    if not key:
        key = getpass.getpass('ElevenLabs API key (hidden): ').strip()
    events = queue.Queue()
    class RecordingPlayer(AudioPlayer):
        def __init__(self):
            super().__init__()
            self.recorded = bytearray()

        def feed(self, pcm):
            self.recorded.extend(pcm)
            super().feed(pcm)

        def close(self):
            if args.save_audio and self.recorded:
                with wave.open(str(args.save_audio), 'wb') as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(16000)
                    out.writeframes(self.recorded)
            super().close()

    analyzer = ElevenLabsAnalyzer(events, agent_id(), key_provider=lambda: key, player_factory=RecordingPlayer)
    started = time.monotonic()
    failed = False
    try:
        analyzer.analyze_async(args.image)
        while True:
            ev = events.get(timeout=95)
            kind = ev[0]
            if kind == 'analysis_text':
                print(ev[2], flush=True)
            elif kind == 'analysis_session':
                print('Conversation:', ev[2], flush=True)
            elif kind == 'speech_started':
                ev[2].set()
                print(f'First audio: {time.monotonic() - started:.2f}s', flush=True)
            elif kind == 'analysis_failed':
                failed = True
                print(ev[2], file=sys.stderr, flush=True)
            elif kind == 'analysis_finished':
                print(f'Finished: {time.monotonic() - started:.2f}s', flush=True)
                return 1 if failed else 0
    finally:
        analyzer.close()


if __name__ == '__main__':
    sys.exit(main())
