#!/usr/bin/env python3
"""Exercise screenshot offers and follow-ups without activating Neon or the microphone."""
import argparse
import getpass
import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from action_flow import ActionFlow
from image_analyzer import ElevenLabsAnalyzer, agent_id
from voice_control import get_api_key


class SilentPlayer:
    def feed(self, chunk): pass
    def drain(self, cancelled): pass
    def cancel(self): pass
    def close(self): pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--agent', default=agent_id())
    parser.add_argument('--key-stdin', action='store_true')
    parser.add_argument('--reply', action='append', default=[])
    parser.add_argument('--silent', action='store_true', help='validate results without speaker output')
    args = parser.parse_args()
    key = sys.stdin.readline().strip() if args.key_stdin else get_api_key()
    if not key:
        key = getpass.getpass('ElevenLabs API key: ').strip()
    if not key or not args.agent or not args.image.is_file():
        parser.error('An image, configured agent, and API key are required')
    events = queue.Queue()
    kwargs = {'player_factory': SilentPlayer} if args.silent else {}
    analyzer = ElevenLabsAnalyzer(events, args.agent, key_provider=lambda: key, configure=True, **kwargs)
    flow = ActionFlow()
    flow.reset(args.image)
    try:
        for reply in [None] + args.reply:
            generation = analyzer.analyze_async(args.image, prompt=flow.follow_up(reply) if reply else None)
            received = False
            while True:
                event = events.get(timeout=90)
                if event[1] != generation:
                    continue
                if event[0] == 'speech_started':
                    event[2].set()
                elif event[0] == 'analysis_result':
                    flow.receive(event[2])
                    received = True
                    print(f"{flow.result['stage']} / {flow.result['action']}: {flow.result['spoken_text']}", flush=True)
                elif event[0] == 'analysis_failed':
                    raise RuntimeError(event[2])
                elif event[0] == 'analysis_finished':
                    if not received:
                        raise RuntimeError('No structured result received')
                    break
    finally:
        analyzer.close()
    print('Check complete. No draft was saved, no microphone was opened.')


if __name__ == '__main__':
    main()
