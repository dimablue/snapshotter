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


def test_voice_acts_on_partials_once_per_sentence():
    events = queue.Queue()
    voice = VoiceControl(events)
    for kind, text in [('partial_transcript', "don't take a"),
                       ('partial_transcript', "don't take a screenshot"),
                       ('committed_transcript', "don't take a screenshot")]:
        voice.handle_message({'message_type': kind, 'text': text})
    assert commands(events) == []
    assert list(events.queue)[-1] == ('status', 'voice: heard “don\'t take a screenshot” (not a command)')
    events.queue.clear()
    for kind, text in [('partial_transcript', 'Hey Neon'),
                       ('partial_transcript', 'Hey Neon, take a screenshot'),  # fires here, not at commit
                       ('committed_transcript', 'Hey Neon, take a screenshot.'),
                       ('partial_transcript', 'okay capture'),
                       ('partial_transcript', 'okay capture that'),
                       ('committed_transcript', 'Okay, capture that.'),
                       ('committed_transcript', 'Capture.')]:  # a new sentence fires again
        voice.handle_message({'message_type': kind, 'text': text})
    assert commands(events) == [('cmd', 'start'), ('cmd', 'capture'), ('cmd', 'capture')]
    assert not is_capture_command('when I say cheese do something')


def test_commands_anywhere_in_a_sentence():
    from voice_control import voice_command, commands_in
    assert voice_command('Okay so now I want you to take a screenshot of this chart') == 'capture'
    assert commands_in('Capture. Capture. I don\'t know why it takes its time.') == ['capture', 'capture']
    assert voice_command('hmm, that box is wrong, can we try again?') == 'r'
    assert voice_command('okay, snap it') == 'capture'
    assert voice_command('take screenshot') == 'capture'
    assert voice_command('please cancel crop mode') == 'cancel'
    assert voice_command("don't take a screenshot") is None
    assert voice_command('I do not take a screenshot of that') is None
    assert voice_command("don't capture") is None
    assert voice_command('I had to cancel the meeting') is None
    assert commands_in('ok cancel') == ['cancel']
    assert commands_in('ok cancel', final=False) == []  # lone "cancel"/"retry" wait for the end
    assert voice_command('take a screenshot, no wait, try again') == 'capture'  # earliest first


def test_hey_neon_starts_a_selection():
    from voice_control import voice_command
    assert voice_command('Hey Neon, take a screenshot.') == 'start'
    assert voice_command('okay neon take a screenshot of this') == 'start'
    assert voice_command('hey neon, screenshot') == 'start'
    assert voice_command('hey neon cancel') == 'cancel'
    assert voice_command('hey neon') is None
    assert voice_command("hey neon don't take a screenshot") is None


def test_voice_crop_commands():
    voice = VoiceControl(queue.Queue())
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


def test_voice_start_drag_capture(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['gazesnap.py'])
    app = GazeSnapApp(parse_args())
    snaps = []
    monkeypatch.setattr(app, 'snap', snaps.append)
    try:
        app.command('capture')  # nothing to capture before "hey Neon"
        assert snaps == [] and not app.crop_active
        app.gaze_xy = (100, 120)
        app.command('start')  # "hey Neon, take a screenshot": first corner at the gaze
        assert app.crop_active and app.crop.state == 'second' and app.crop.anchor == (100, 120)
        app.crop.update(130, 130, 1.0)  # too small to capture yet
        app.command('capture')
        assert snaps == []
        app.crop.update(500, 420, 1.1)  # gaze drags the opposite corner
        app.gaze_xy = (500, 420)
        app.command('r')  # "retry" re-pins the first corner at the current gaze
        assert app.crop.anchor == (500, 420) and app.crop.rect is None
        app.crop.update(200, 150, 1.2)
        app.command('capture')
        assert snaps == [(200, 150, 300, 270)]
        app.crop_by_voice, app.crop.state = False, 'second'
        app.command('cancel')
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
