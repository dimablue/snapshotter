import base64
import io
import json
import os
import queue
import sys
import threading
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import image_analyzer as module
from image_analyzer import ElevenLabsAnalyzer, AnalysisError
from audio_player import AudioPlayer


class Events(queue.Queue):
    def put(self, event, *args, **kwargs):
        if event[0] == 'speech_started':
            event[2].set()
        super().put(event, *args, **kwargs)


class Player:
    def __init__(self):
        self.pcm = bytearray()
        self.cancelled = False
        self.closed = False

    def feed(self, chunk):
        self.pcm.extend(chunk)

    def drain(self, cancelled):
        assert not cancelled()

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


def wait_finished(events):
    result = []
    while True:
        ev = events.get(timeout=5)
        result.append(ev)
        if ev[0] == 'analysis_finished':
            return result


def test_uploads_image_then_streams_and_closes_despite_pings(tmp_path, monkeypatch):
    from websockets.sync import client
    image = tmp_path / 'capture.png'
    image.write_bytes(b'PNG sample')
    calls, sent = [], []
    player = Player()
    frames = [
        {'type': 'conversation_initiation_metadata', 'conversation_initiation_metadata_event': {
            'conversation_id': 'conv-test', 'agent_output_audio_format': 'pcm_16000'}},
        {'type': 'client_tool_call', 'client_tool_call': {
            'tool_name': 'publish_snapshot_result', 'tool_call_id': 'tool-1',
            'parameters': {'stage': 'offer', 'action': 'questions', 'summary': 'Revenue rose overall.'}}},
        {'type': 'audio', 'audio_event': {'audio_base_64': base64.b64encode(b'\x01\x00' * 80).decode()}},
        {'type': 'agent_response', 'agent_response_event': {'agent_response': 'Revenue rose overall.'}},
        {'type': 'agent_response_complete'},
    ]
    class Socket:
        closed = False
        received = 0
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
        def send(self, data): sent.append(json.loads(data))
        def recv(self, timeout):
            self.received += 1
            if frames: return json.dumps(frames.pop(0))
            return json.dumps({'type': 'ping', 'ping_event': {'event_id': 42}})
    socket = Socket()
    clock = [0]
    def now():
        clock[0] += .2
        return clock[0]
    monkeypatch.setattr(module.time, 'monotonic', now)
    monkeypatch.setattr(client, 'connect', lambda *a, **kw: socket)
    def request(key, path, data=None, **kwargs):
        calls.append((path, data))
        if '/files' in path:
            assert b'PNG sample' in data
            assert sent == [{'type': 'conversation_initiation_client_data'}]
            return {'file_id': 'file-test'}
        return {'signed_url': 'wss://api.elevenlabs.io/v1/convai/conversation?token=test'}
    monkeypatch.setattr(module, 'request', request)
    events = Events()
    analyzer = ElevenLabsAnalyzer(events, 'agent-test', lambda: 'secret', lambda: player)
    try:
        analyzer.analyze_async(image)
        result = wait_finished(events)
        assert not any(ev[0] == 'analysis_failed' for ev in result)
        assert ('analysis_text', 1, 'Revenue rose overall.') in result
        structured = next(ev[2] for ev in result if ev[0] == 'analysis_result')
        assert structured['action'] == 'questions'
        reply = next(s for s in sent if s['type'] == 'client_tool_result')
        assert reply['tool_call_id'] == 'tool-1' and not reply['is_error']
        assert json.loads(reply['result'])['saved'] is False
        multimodal = next(s for s in sent if s['type'] == 'multimodal_message')
        assert multimodal['file']['file_id'] == 'file-test'
        assert player.pcm == b'\x01\x00' * 80 and player.closed
        assert socket.closed and socket.received < 30
    finally:
        analyzer.close()


def test_newest_capture_replaces_pending_work_and_discards_old_results(tmp_path):
    started, release = threading.Event(), threading.Event()
    processed = []
    class Analyzer(ElevenLabsAnalyzer):
        def _analyze(self, generation, path):
            processed.append(path.name)
            if path.name == 'first':
                started.set()
                release.wait(3)
            self._emit(generation, 'analysis_text', path.name)
    events = Events()
    analyzer = Analyzer(events, 'test')
    try:
        analyzer.analyze_async(tmp_path / 'first')
        assert started.wait(1)
        analyzer.analyze_async(tmp_path / 'second')
        newest = analyzer.analyze_async(tmp_path / 'third')
        release.set()
        result = wait_finished(events)
        assert processed == ['first', 'third']
        assert result == [('analysis_text', newest, 'third'), ('analysis_finished', newest)]
    finally:
        release.set()
        analyzer.close()


def test_missing_key_reports_failure_without_a_connection(tmp_path):
    image = tmp_path / 'capture.png'
    image.write_bytes(b'png')
    events = Events()
    analyzer = ElevenLabsAnalyzer(events, 'test', lambda: None)
    try:
        analyzer.analyze_async(image)
        result = wait_finished(events)
        assert result[0] == ('analysis_failed', 1, 'ElevenLabs API key is missing')
    finally:
        analyzer.close()


def test_remote_errors_do_not_expose_key_or_payload(monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('https://api.elevenlabs.io', 403, 'secret', {}, io.BytesIO(b'secret'))
    monkeypatch.setattr(module.urllib.request, 'urlopen', fail)
    with pytest.raises(AnalysisError) as exc:
        module.request('secret', 'convai/agents')
    assert 'secret' not in str(exc.value)


def test_audio_cancel_silences_buffer_and_rejects_future_audio():
    player = AudioPlayer()
    player._stream = Mock()
    player.feed(b'\x01\x00' * 80)
    player.cancel()
    player.feed(b'\x02\x00' * 80)
    output = bytearray(160)
    player._fill(output, 80, None, None)
    assert output == b'\0' * 160 and player._bytes == 0
    player.close()


def test_voice_does_not_process_speaker_audio(monkeypatch):
    from voice_control import VoiceControl
    voice = VoiceControl(queue.Queue())
    voice.set_output_muted(True)
    voice.handle_message({'message_type': 'committed_transcript', 'text': 'hey neon take a screenshot'})
    voice.set_output_muted(False)
    voice.handle_message({'message_type': 'committed_transcript', 'text': 'capture'})
    assert voice.events.empty()  # grace interval also discards trailing speaker audio


@pytest.fixture
def app(monkeypatch):
    from gazesnap import GazeSnapApp, parse_args
    monkeypatch.setattr(sys, 'argv', ['gazesnap.py', '--wake'])
    app = GazeSnapApp(parse_args())
    yield app
    app.handle.close()
    app.resize_handle.close()
    app.overlay.close()


def test_wake_capture_automatically_starts_analysis(app, monkeypatch, tmp_path):
    image = tmp_path / 'capture.png'
    from PyQt6.QtGui import QImage
    QImage(40, 40, QImage.Format.Format_RGB32).save(str(image))
    app.analyzer = Mock()
    app.analyzer.analyze_async.return_value = 7
    app.args.no_sound = True
    monkeypatch.setattr('gazesnap.grab_region', lambda *args: str(image))
    app._finish_snap((0, 0, 40, 40))
    app.analyzer.analyze_async.assert_called_once_with(str(image))
    assert app.analysis_id == 7


def test_failed_capture_never_uploads(app, monkeypatch):
    app.analyzer = Mock()
    def fail(*args): raise RuntimeError('screen recording denied')
    monkeypatch.setattr('gazesnap.grab_region', fail)
    app._finish_snap((0, 0, 40, 40))
    app.analyzer.analyze_async.assert_not_called()


def test_playback_gating_preserves_pause_and_ignores_stale_events(app):
    app.wake = Mock(paused=True)
    app.analysis_id = 8
    ready = threading.Event()
    app.analysis_event(('speech_started', 8, ready))
    assert ready.is_set() and app.analysis_speaking
    app.wake.set_output_muted.assert_called_once_with(True)
    app.analysis_event(('analysis_finished', 7))
    assert app.analysis_speaking  # previous capture cannot unmute a newer one
    app.analysis_event(('analysis_finished', 8))
    assert not app.analysis_speaking and app.wake.paused is True
    assert app.wake.set_output_muted.call_args.args == (False,)


def test_analysis_off_cancels_and_invalidates_playback(app):
    app.analyzer = Mock()
    app.analysis_id = 9
    app.set_analysis_enabled(False)
    assert not app.analysis_enabled and app.analysis_id is None
    app.analyzer.cancel.assert_called_once()
