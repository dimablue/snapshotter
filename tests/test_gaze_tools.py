import sys
from pathlib import Path

import msgpack
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from analyze_gaze import fit, per_dot, report, screen_gaze  # noqa: E402
from record_gaze import avoid_markers, build_phases  # noqa: E402
from replay_gaze import retime  # noqa: E402

SPAN = (100, 50, 1100, 850)  # marker area: 1000 x 800 points
OFFSET = 500.0  # Pupil clock - monotonic


def session(bias=(30, -20), noise=4.0):
    """Synthetic recording: a 'still' grid where gaze lands `bias` points off each dot."""
    left, top, right, bottom = SPAN
    targets = [(left + fx * 1000, top + fy * 800) for fy in (0.2, 0.5, 0.8) for fx in (0.2, 0.5, 0.8)]
    meta = {'span': SPAN, 'surface': 'screen', 'clock_offset': OFFSET, 'screen': [0, 0, 1200, 900],
            'physical_size_mm': [300, 225], 'correction': None, 'pupil_version': 'test',
            'phases': [{'name': 'still', 'kind': 'dots', 'targets': targets, 'settle': 1.0, 'collect': 1.0}]}
    rng = np.random.default_rng(0)
    events, messages, t = [{'phase': 'still', 'start': True, 't': 0.0}], [], 0.0
    for i, (tx, ty) in enumerate(targets):
        events.append({'phase': 'still', 'i': i, 'target': [tx, ty], 't': t})
        for k in range(200):  # 2 s at 100 Hz
            ts = t + k / 100
            x, y = tx + bias[0] + rng.normal(0, noise), ty + bias[1] + rng.normal(0, noise)
            nx, ny = (x - left) / 1000, 1 - (y - top) / 800
            msg = {'name': 'screen', 'timestamp': ts + OFFSET, 'gaze_on_surfaces': [
                {'norm_pos': [nx, ny], 'confidence': 0.9, 'timestamp': ts + OFFSET}]}
            messages.append([ts, 'surfaces.screen', msgpack.packb(msg)])
            messages.append([ts, 'pupil.0.2d', msgpack.packb({'id': 0, 'confidence': 0.9, 'timestamp': ts + OFFSET})])
        t += 2.0
    return meta, events, messages


def test_screen_gaze_undoes_the_surface_mapping():
    meta, _, messages = session(bias=(0, 0), noise=0)
    g = screen_gaze(meta, messages)
    assert np.allclose(g[0, 1:3], (300, 210))  # first dot
    assert np.isclose(g[0, 0], 0.0)  # back on the monotonic clock


def test_per_dot_measures_the_bias_and_report_runs():
    meta, events, messages = session()
    from analyze_gaze import dot_windows
    dots = per_dot(screen_gaze(meta, messages), dot_windows(meta, events))
    assert len(dots) == 9
    for _, target, med, n, precision in dots:
        assert n >= 90
        assert np.allclose(np.subtract(med, target), (30, -20), atol=3)
        assert precision < 10
    lines = []
    report(meta, events, messages, out=lines.append)
    assert any(line.startswith('still') and '9/9' in line for line in lines)


def test_affine_fit_removes_a_linear_error():
    t = np.array([(x, y) for x in (100, 500, 900) for y in (100, 400, 700)], float)
    m = t * 1.1 + (20, -30)
    assert np.allclose(fit('affine', m, t)(m), t)


def test_dots_avoid_the_markers():
    rects = [(0, 0, 200, 200)]
    assert avoid_markers([(100, 100), (500, 500), (210, 100)], rects) == [(500, 500)]
    phases = build_phases(SPAN, [(100, 50, 300, 250)])
    still = phases[0]['targets']
    assert len(still) == 23  # the corner dot and the one below it touch the marker
    assert {p['name'] for p in phases} == {'still', 'pursuit', 'head', 'end'}


def test_retime_moves_every_timestamp_gazesnap_reads():
    msg = {'timestamp': 1.0, 'gaze_on_surfaces': [{'timestamp': 1.0}], 'fixations_on_surfaces': []}
    out = retime(msg, 10.0)
    assert out['timestamp'] == 11.0 and out['gaze_on_surfaces'][0]['timestamp'] == 11.0
