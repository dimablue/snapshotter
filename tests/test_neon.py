import queue
from types import SimpleNamespace
from unittest.mock import Mock

from sources.neon import NeonSource


def test_neon_closes_if_calibration_fails(monkeypatch):
    import pupil_labs.real_time_screen_gaze.gaze_mapper as module
    device = Mock(phone_name='Test phone')
    device.get_calibration.side_effect = RuntimeError('calibration unavailable')
    source = NeonSource(800, 600, queue.Queue(), SimpleNamespace(blink_mode='auto'))
    monkeypatch.setattr(source, '_connect', lambda: device)
    import pytest
    with pytest.raises(RuntimeError, match='calibration unavailable'):
        source.run()
    device.close.assert_called_once()


def test_neon_gaze_without_blink_stream(monkeypatch):
    import pupil_labs.real_time_screen_gaze.gaze_mapper as module
    events = queue.Queue()
    source = NeonSource(800, 600, events,
                        SimpleNamespace(blink_mode='auto', flip_y=False, no_blink_capture=True))
    device = Mock(phone_name='Test phone')
    mapper = Mock()
    mapper.add_surface.return_value.uid = 'screen'
    mapper.process_frame.return_value.mapped_gaze = {
        'screen': [SimpleNamespace(x=0.25, y=0.75)]}
    def receive(**kwargs):
        source.stop()
        return SimpleNamespace(frame=object(), gaze=object())
    device.receive_matched_scene_video_frame_and_gaze.side_effect = receive
    monkeypatch.setattr(source, '_connect', lambda: device)
    monkeypatch.setattr(module, 'GazeMapper', lambda calibration: mapper)
    source.run()
    received = list(events.queue)
    assert any(ev[0] == 'gaze' and ev[1:3] == (200.0, 150.0) for ev in received)
    device.receive_eye_events.assert_not_called()
    device.close.assert_called_once()
