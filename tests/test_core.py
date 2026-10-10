"""Headless checks for core.py. Run: python tests/test_core.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import BlinkDetector, BoxTracker, GazeFilter  # noqa: E402


def test_natural_blinks_ignored():
    d = BlinkDetector()
    # natural blinks ~150 ms, 3 s apart
    assert all(d.on_blink(t, t + 0.15) is None for t in (1.0, 4.0, 7.0, 10.0))


def test_long_blink_fires():
    d = BlinkDetector()
    assert d.on_blink(5.0, 5.5) == 5.0


def test_eyes_resting_ignored():
    d = BlinkDetector()
    assert d.on_blink(5.0, 7.5) is None


def test_double_blink_fires_with_first_start():
    d = BlinkDetector()
    assert d.on_blink(2.0, 2.15) is None
    assert d.on_blink(2.4, 2.55) == 2.0


def test_cooldown():
    d = BlinkDetector()
    assert d.on_blink(5.0, 5.5) == 5.0
    assert d.on_blink(6.0, 6.5) is None   # within 1.5 s cooldown of 5.5
    assert d.on_blink(8.0, 8.5) == 8.0


def test_gesture_modes():
    long_only = BlinkDetector(gesture="long")
    long_only.on_blink(2.0, 2.15)
    assert long_only.on_blink(2.4, 2.55) is None
    double_only = BlinkDetector(gesture="double")
    assert double_only.on_blink(5.0, 5.5) is None


def test_box_clamped_and_stable():
    b = BoxTracker(1440, 900, 480, 300)
    for i in range(40):
        b.update(10, 10, i * 0.016)
    assert b.current_rect() == (0, 0, 480, 300)
    assert b.stable


def test_box_unstable_while_moving():
    b = BoxTracker(1440, 900)
    for i in range(40):
        b.update(100 + i * 20, 400, i * 0.016)
    assert not b.stable


def test_rect_before_uses_history():
    b = BoxTracker(1440, 900, 200, 100)
    for i in range(30):
        b.update(400, 400, i * 0.016)       # fixating at (400, 400)
    for i in range(30, 40):
        b.update(400, 700, i * 0.016)       # gaze drops as the eyelid closes
    assert b.rect_before(30 * 0.016) == (300, 350, 200, 100)


def test_filter_smooths_jitter():
    f = GazeFilter()
    xs = []
    for i in range(120):
        jitter = 30 if i % 2 else -30
        x, _ = f(500 + jitter, 500, i / 60)
        xs.append(x)
    assert max(abs(x - 500) for x in xs[60:]) < 15


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
