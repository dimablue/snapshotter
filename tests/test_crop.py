"""Deterministic dwell-crop checks. Run python tests/test_crop.py."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import CropSelector

def hold(c, x, y, t):
    action = None
    for i in range(10):
        action = c.update(x, y, t + i * .1) or action
    return action

def preview():
    c = CropSelector(1440, 900)
    hold(c, 700, 600, 0)
    assert c.state == "second"
    hold(c, 200, 100, 1)
    assert c.state == "preview"
    assert c.rect == (200, 100, 500, 500)
    return c

def test_preview_frozen():
    c = preview()
    c.update(1000, 700, 2)
    assert c.rect == (200, 100, 500, 500)

def test_actions():
    for expected, rect in preview().controls():
        c = preview()
        x, y, w, h = rect
        assert hold(c, x + w/2, y + h/2, 3) == expected.lower()
        assert c.state == "done"
        assert c.update(x, y, 4) is None

def test_gap_and_motion_reset():
    c = CropSelector(1440, 900)
    c.update(100, 100, 0)
    c.update(100, 100, .7)
    assert c.progress == 0 and c.state == "first"
    c.update(200, 200, .8)
    assert c.progress == 0

def test_small_selection_rejected():
    c = CropSelector(1440, 900)
    hold(c, 100, 100, 0)
    hold(c, 110, 110, 1)
    assert c.state == "second"

def test_edges_and_reset():
    c = CropSelector(1440, 900)
    hold(c, -20, -20, 0)
    hold(c, 1600, 1000, 1)
    assert c.rect == (0, 0, 1440, 900)
    c.reset()
    assert c.state == "first" and c.rect is None and c.progress == 0

if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for test in tests:
        test()
        print("ok", test.__name__)
    print(f"{len(tests)} passed")
