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


def test_voice_only_final_positive_commands():
    events = queue.Queue()
    voice = VoiceControl(events)
    voice.handle_message({'message_type': 'partial_transcript', 'text': 'take a screenshot'})
    voice.handle_message({'message_type': 'committed_transcript', 'text': "don't take a screenshot"})
    assert events.empty()
    voice.handle_message({'message_type': 'committed_transcript', 'text': 'Take a screenshot, please!'})
    assert events.get_nowait() == ('cmd', '')
    voice.handle_message({'message_type': 'committed_transcript', 'text': 'take a screenshot'})
    assert events.empty()  # cooldown
    assert not is_capture_command('when I say screenshot do something')


def test_oversize_box_stays_on_screen():
    assert BoxTracker(800, 600, 3000, 2000).current_rect() == (0, 0, 800, 600)


def test_drag_pause_and_follow(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['gazesnap.py'])
    app = GazeSnapApp(parse_args())
    try:
        app.tick()
        assert app.manual and app.handle.isVisible()
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
