import os
import queue
import sys
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from voice_control import VoiceControl, is_capture_command
from core import BoxTracker
from gazesnap import GazeSnapApp, parse_args
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest


def commands(events):
    return [ev for ev in list(events.queue) if ev[0] == 'cmd']


def test_voice_only_final_positive_commands():
    events = queue.Queue()
    voice = VoiceControl(events)
    voice.handle_message({'message_type': 'partial_transcript', 'text': 'take a screenshot'})
    assert events.empty()
    voice.handle_message({'message_type': 'committed_transcript', 'text': "don't take a screenshot"})
    assert commands(events) == []
    assert events.get_nowait() == ('status', 'voice: heard “don\'t take a screenshot” (not a command)')
    voice.handle_message({'message_type': 'committed_transcript', 'text': 'Take a screenshot, please!'})
    assert commands(events) == [('cmd', '')]
    events.queue.clear()
    voice.handle_message({'message_type': 'committed_transcript', 'text': 'take a screenshot'})
    assert commands(events) == []  # cooldown
    assert not is_capture_command('when I say screenshot do something')


def test_voice_crop_commands(monkeypatch):
    voice = VoiceControl(queue.Queue())
    times = iter([10.0, 20.0, 30.0])
    monkeypatch.setattr('voice_control.time.monotonic', lambda: next(times))
    for text in ('Retry.', 'Cancel!', "don't cancel"):
        voice.handle_message({'message_type': 'committed_transcript', 'text': text})
    assert commands(voice.events) == [('cmd', 'r'), ('cmd', 'cancel')]


def test_rejected_key_is_forgotten(monkeypatch):
    import pytest
    from voice_control import VoiceError
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'bad-key')
    voice = VoiceControl(queue.Queue())
    with pytest.raises(VoiceError, match='rejected the API key'):
        voice.handle_message({'message_type': 'auth_error', 'error': 'You must be authenticated'})
    assert 'ELEVENLABS_API_KEY' not in os.environ


def test_crop_saved_by_voice_retry_and_cancel(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['gazesnap.py', '--selection', 'crop'])
    app = GazeSnapApp(parse_args())
    snaps = []
    monkeypatch.setattr(app, 'snap', snaps.append)
    try:
        assert app.crop_active and not app.handle.isVisible()
        app.command('')  # nothing selected yet
        assert snaps == []
        app.crop.state, app.crop.rect = 'preview', (10, 20, 300, 200)
        app.command('')  # "take a screenshot"
        assert snaps == [(10, 20, 300, 200)] and app.crop.state == 'done'
        app.crop.state = 'preview'
        app.command('r')  # "retry"
        assert app.crop.state == 'first' and app.crop.rect is None
        app.command('cancel')  # "cancel"
        assert not app.crop_active and app.overlay.crop is None
        app.command('cancel')  # ignored outside crop mode
        assert not app.crop_active
    finally:
        app.handle.close()
        app.resize_handle.close()
        app.overlay.close()


def test_oversize_box_stays_on_screen():
    assert BoxTracker(800, 600, 3000, 2000).current_rect() == (0, 0, 800, 600)


def test_drag_pause_and_follow(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['gazesnap.py'])
    app = GazeSnapApp(parse_args())
    try:
        app.tick()
        assert app.manual and app.handle.isVisible()
        assert app.overlay.windowFlags() & Qt.WindowType.NoDropShadowWindowHint
        app.log("voice: listening")
        assert app.overlay.voice_status == "voice: listening"
        app.log("saved example.png")
        assert app.overlay.voice_status == "voice: listening"
        initial = app.tracker.current_rect()
        QTest.mousePress(app.handle, Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
        QTest.mouseMove(app.handle, QPoint(70, 40))
        QTest.mouseRelease(app.handle, Qt.MouseButton.LeftButton, pos=QPoint(70, 40))
        before = app.tracker.current_rect()
        assert before != initial
        app.tick()
        QTest.mousePress(app.resize_handle, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        QTest.mouseMove(app.resize_handle, QPoint(45, 30))
        QTest.mouseRelease(app.resize_handle, Qt.MouseButton.LeftButton, pos=QPoint(45, 30))
        resized = app.tracker.current_rect()
        assert resized[:2] == before[:2]
        assert resized[2] == before[2] + 35
        assert resized[3] == before[3] + 20
        before = resized
        app.events.put(('gaze', 0, 0, 1))
        app.tick()
        assert app.tracker.current_rect() == before
        app.command('m')
        app.tick()
        assert not app.manual and not app.handle.isVisible()
        app.command('p')
        app.snap(app.tracker.current_rect())
        assert not app.snapping
        app.command('p')
        app.command('m')
        app.tick()
        app.snap(app.tracker.current_rect())
        assert app.snapping and not app.handle.isVisible()
        assert not app.resize_handle.isVisible()
        # Finish without accessing the user's screen.
        monkeypatch.setattr('gazesnap.grab_region', Mock(side_effect=RuntimeError('test')))
        QTest.qWait(100)
        assert not app.snapping and not app.overlay.hidden_for_capture
    finally:
        app.handle.close()
        app.resize_handle.close()
        app.overlay.close()


def test_resize_limits_and_anchor():
    tracker = BoxTracker(1440, 900, 480, 300)
    left, top, _, _ = tracker.current_rect()
    tracker.resize_to(5000, 5000)
    assert tracker.current_rect() == (left, top, 1440 - left, 900 - top)
    tracker.resize_to(-100, -100)
    assert tracker.current_rect() == (left, top, 120, 80)
