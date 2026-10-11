#!/usr/bin/env python3
"""Create a reusable, authenticated ElevenLabs screenshot analysis agent."""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_analyzer import CONFIG_PATH, PROMPT, AnalysisError, agent_id, request
from voice_control import get_api_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voice-id', default='JBFqnCBsd6RMkjVDRZzb')
    parser.add_argument('--model', default='gpt-4o')
    parser.add_argument('--key-stdin', action='store_true', help='read key from stdin instead of a prompt')
    args = parser.parse_args()
    key = sys.stdin.readline().strip() if args.key_stdin else get_api_key()
    if not key:
        key = getpass.getpass('ElevenLabs API key (hidden): ').strip()
    if not key:
        parser.error('An API key is required')
    existing = agent_id()
    if existing:
        request(key, 'convai/agents/' + existing)
        print('Analysis agent is already configured:', existing)
        return
    config = {'name': 'Snapshotter image analysis', 'conversation_config': {
        'agent': {'first_message': '', 'language': 'en', 'prompt': {
            'prompt': PROMPT, 'llm': args.model, 'temperature': 0.2}},
        'tts': {'voice_id': args.voice_id, 'model_id': 'eleven_flash_v2',
                'agent_output_audio_format': 'pcm_16000'},
        'conversation': {'max_duration_seconds': 75,
            'client_events': ['audio', 'agent_response', 'agent_response_complete'],
            'file_input': {'enabled': True}}},
        'platform_settings': {'auth': {'enable_auth': True}}}
    created = request(key, 'convai/agents/create', config)['agent_id']
    try:
        CONFIG_PATH.write_text(json.dumps({'agent_id': created}, indent=2) + '\n')
    except OSError:
        request(key, 'convai/agents/' + created, method='DELETE')
        raise
    print('Configured analysis agent:', created)
    print('No API key was saved. Run: python gazesnap.py --source mouse --wake')


if __name__ == '__main__':
    try:
        main()
    except AnalysisError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
