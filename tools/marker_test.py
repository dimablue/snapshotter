#!/usr/bin/env python3
"""How faint can the corner markers be? Draws them at falling opacity (and a slimmer white
border) and measures, for each, how often Pupil Capture finds the screen surface and how much
its position jitters. Wear the Neon, look at the screen and keep your head still (~45 s).

    python tools/marker_test.py
"""
import sys
import threading
import time
from pathlib import Path

import msgpack
import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from neon_setup import Remote  # noqa: E402
from overlay import DEFAULT_MARKER_SIZE, Overlay  # noqa: E402

# (opacity, white border px or None for the full quiet zone)
CONFIGS = [(1.0, None), (1.0, 12), (0.6, 12), (0.4, 12), (0.25, 12), (0.15, 12), (0.6, None), (1.0, None)]
SECONDS, SETTLE = 4.0, 1.0


def corners_in_image(msg):
    """The surface's corners in scene-camera pixels."""
    h = np.array(msg["surf_to_img_trans"])
    pts = np.array([[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) @ h.T
    return pts[:, :2] / pts[:, 2:]


def main():
    app = QApplication(sys.argv[:1])
    screen = app.primaryScreen()
    overlay = Overlay(screen.geometry(), show_gaze=False)
    overlay.show()
    overlay.place_markers(DEFAULT_MARKER_SIZE, screen.availableGeometry())
    remote = Remote("127.0.0.1", 50020)
    sub = remote.subscribe("surfaces.screen")
    seen = []  # (monotonic, corners)
    stop = threading.Event()

    def listen():
        while not stop.is_set():
            if sub.poll(100):
                _, payload, *_ = sub.recv_multipart()
                seen.append((time.monotonic(), corners_in_image(msgpack.unpackb(payload, raw=False))))

    threading.Thread(target=listen, daemon=True).start()
    results, state = [], {"i": -1, "t": 0.0}

    def step():
        now = time.monotonic()
        if state["i"] >= 0 and now - state["t"] < SECONDS + SETTLE:
            return
        if state["i"] >= 0:
            t0 = state["t"] + SETTLE
            got = [c for t, c in seen if t0 <= t < t0 + SECONDS]
            results.append((CONFIGS[state["i"]], got))
        state["i"] += 1
        state["t"] = now
        if state["i"] >= len(CONFIGS):
            stop.set()
            app.quit()
            return
        overlay.marker_opacity, overlay.marker_pad = CONFIGS[state["i"]]
        overlay.check = (None, 0, 0, f"marker test {state['i'] + 1}/{len(CONFIGS)}: look at the screen, head still")
        overlay.update()

    timer = QTimer()
    timer.timeout.connect(step)
    timer.start(50)
    app.exec()

    base = [c for (cfg, got) in results[:1] for c in got]
    ref = np.median(base, axis=0) if base else None
    print("opacity  border   found    jitter (scene px)   shift from first")
    for (opacity, pad), got in results:
        rate = len(got) / SECONDS
        if got:
            arr = np.array(got)
            jitter = np.median(np.linalg.norm(arr - np.median(arr, axis=0), axis=2))
            shift = np.linalg.norm(np.median(arr, axis=0) - ref, axis=1).max() if ref is not None else float("nan")
            print(f"{opacity:6.2f}  {'full' if pad is None else f'{pad} px':>6}  {rate:5.0f}/s   {jitter:8.2f}"
                  f"            {shift:6.1f}")
        else:
            print(f"{opacity:6.2f}  {'full' if pad is None else f'{pad} px':>6}      0/s   (not found)")


if __name__ == "__main__":
    main()
