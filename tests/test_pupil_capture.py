import queue
from types import SimpleNamespace

from sources.pupil_capture import PupilCaptureSource


def make_source(**overrides):
    args = dict(surface='screen', min_confidence=0.6, blink_confidence=0.3,
                flip_y=False, no_blink_capture=False)
    args.update(overrides)
    events = queue.Queue()
    source = PupilCaptureSource(800, 600, events, SimpleNamespace(**args))
    return source, events


def gaze_msg(x, y, confidence=0.9, name='screen', ts=10.0):
    return {'name': name, 'gaze_on_surfaces': [
        {'norm_pos': [x, y], 'confidence': confidence, 'timestamp': ts}]}


def pupil(eye, confidence, ts):
    return {'id': eye, 'confidence': confidence, 'timestamp': ts}


def test_surface_gaze_maps_into_marker_rect_with_y_flipped():
    source, events = make_source()
    source.marker_verts = {0: [(100, 50), (200, 50), (200, 150), (100, 150)],
                           3: [(600, 450), (700, 450), (700, 550), (600, 550)]}
    source.offset = 4.0
    source.on_surface(gaze_msg(0.25, 0.75))
    assert list(events.queue) == [('gaze', 250.0, 175.0, 6.0)]


def test_flip_y_keeps_pupil_orientation():
    source, events = make_source(flip_y=True)
    source.on_surface(gaze_msg(0.25, 0.75))
    assert list(events.queue)[0][1:3] == (200.0, 450.0)


def test_low_confidence_offscreen_and_other_surfaces_are_dropped():
    source, events = make_source()
    source.on_surface(gaze_msg(0.5, 0.5, confidence=0.2))
    source.on_surface(gaze_msg(1.5, 0.5))
    source.on_surface(gaze_msg(0.5, 0.5, name='desk'))
    source.on_surface(gaze_msg(0.5, 0.5, name='desk'))
    received = list(events.queue)
    assert not any(ev[0] == 'gaze' for ev in received)
    assert [ev[0] for ev in received] == ['status']  # warns about 'desk' once


def test_blink_needs_both_eyes_closed():
    source, events = make_source()
    source.on_pupil(pupil(0, 0.9, 1.00))
    source.on_pupil(pupil(1, 0.9, 1.00))
    source.on_pupil(pupil(0, 0.1, 1.01))   # one eye dips: not a blink
    source.on_pupil(pupil(1, 0.1, 1.02))   # both closed from here
    source.on_pupil(pupil(0, 0.1, 1.30))
    source.on_pupil(pupil(1, 0.1, 1.50))
    source.on_pupil(pupil(0, 0.9, 1.52))   # reopened
    assert list(events.queue) == [('blink', 1.02, 1.52)]


def test_single_frame_dip_and_no_blink_capture_are_ignored():
    source, events = make_source()
    source.on_pupil(pupil(0, 0.1, 1.00))
    source.on_pupil(pupil(0, 0.9, 1.02))
    assert events.empty()
    source, events = make_source(no_blink_capture=True)
    source.on_pupil(pupil(0, 0.1, 1.00))
    source.on_pupil(pupil(0, 0.9, 1.50))
    assert events.empty()


def test_run_reports_when_pupil_capture_is_not_running():
    source, events = make_source(pupil_host='127.0.0.1', pupil_port=1)
    source.run()
    assert 'not answering' in events.get_nowait()[1]


def test_restart_eye_cameras_sequence():
    from sources.pupil_capture import restart_eye_cameras
    sent = []
    restart_eye_cameras(lambda subject, **kw: sent.append((subject, kw)), sleep=lambda s: None)
    assert [s for s, _ in sent] == ['stop_plugin', 'start_plugin', 'neon_backend.shared_camera.should_start',
                                    'neon_backend.auto_activate']
    assert sent[-1][1] == {'scene': False, 'eyes': True}  # never reopen the scene camera over raw USB
