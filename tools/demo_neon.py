#!/usr/bin/env python3
"""Demo `--source neon` without the headset: plays a scripted gaze session through a
stand-in for Pupil Capture's network API, and starts Snapshotter connected to it.

    python tools/demo_neon.py                 # "hey Neon, take a screenshot", drag with gaze, "capture"
    python tools/demo_neon.py --scenario box  # box follows gaze; long blink, double blink

Voice commands are simulated: the demo sends Snapshotter the same command the microphone would.
Extra arguments go to snapshotter.py, e.g. `python tools/demo_neon.py --no-sound`.
"""
import argparse
import math
import subprocess
import sys
import threading
import time
from pathlib import Path

import msgpack
import zmq
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
CLOCK = 1000.0  # stand-in Pupil clock = monotonic + 1000, so the offset sync is exercised


def lerp(p, q, k):
    k = max(0.0, min(1.0, k))
    k = k * k * (3 - 2 * k)  # ease in/out like a saccade settling
    return p[0] + (q[0] - p[0]) * k, p[1] + (q[1] - p[1]) * k


def voice_script(W, H):
    """Say "hey Neon, take a screenshot" at a corner, drag, "retry" from a new corner, drag, "capture"."""
    a, b = (0.30 * W, 0.28 * H), (0.62 * W, 0.62 * H)
    a2, b2 = (0.22 * W, 0.35 * H), (0.55 * W, 0.7 * H)

    def wander(s):
        u = s / 2.5 * 2 * math.pi
        return 0.5 * W + 0.25 * W * math.sin(u), 0.5 * H + 0.2 * H * math.sin(2 * u)

    def at(s):
        if s < 2.5:
            return wander(s), "look around"
        if s < 3.5:
            return lerp(wander(2.5), a, (s - 2.5) / 0.4), "look at the first corner"
        if s < 5.0:
            return lerp(a, b, (s - 3.5) / 1.2), "drag with your eyes"
        if s < 6.0:
            return lerp(b, a2, (s - 5.0) / 0.4), "look at a better first corner"
        if s < 8.0:
            return lerp(a2, b2, (s - 6.0) / 1.5), "drag with your eyes"
        u = (s - 8.5) / 3 * 2 * math.pi
        return (0.5 * W + 0.2 * W * math.cos(u), 0.45 * H + 0.15 * H * math.sin(u)), "look around"
    says = [(3.2, "start", "hey Neon, take a screenshot"), (5.7, "r", "retry"), (8.0, "capture", "capture")]
    return at, [], says, 10.5


def box_script(W, H):
    """Look around, hold upper left + long blink, move lower right + double blink."""
    ul, lr = (0.3 * W, 0.3 * H), (0.7 * W, 0.68 * H)

    def wander(s):
        u = s / 6 * 2 * math.pi
        return 0.5 * W + 0.3 * W * math.sin(u), 0.5 * H + 0.25 * H * math.sin(2 * u)

    def at(s):
        if s < 6:
            return wander(s), "look around"
        if s < 10:
            return lerp(wander(6), ul, (s - 6) / 0.4), "hold upper left, long blink"
        if s < 15:
            return lerp(ul, lr, (s - 10) / 0.5), "hold lower right, double blink"
        return wander(s - 15), "look around"
    return at, [(8.5, 9.1), (13.5, 13.65), (13.95, 14.1)], [], 18


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", choices=["voice", "box"], default="voice")
    p.add_argument("--port", type=int, default=50120, help="stand-in Pupil Remote port")
    args, snapshotter_args = p.parse_known_args()

    app = QApplication(sys.argv[:1])
    screen = app.primaryScreen()
    W, H = screen.geometry().width(), screen.geometry().height()
    # same marker rectangle as Overlay.place_markers with the default marker size
    sys.path.insert(0, str(ROOT))
    from overlay import DEFAULT_MARKER_SIZE, marker_layout
    corners, _ = marker_layout(screen.availableGeometry().intersected(screen.geometry()), DEFAULT_MARKER_SIZE)
    xs = [x for x, _ in corners.values()] + [x + DEFAULT_MARKER_SIZE for x, _ in corners.values()]
    ys = [y for _, y in corners.values()] + [y + DEFAULT_MARKER_SIZE for _, y in corners.values()]
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    at, blinks, says, length = (voice_script if args.scenario == "voice" else box_script)(W, H)

    ctx = zmq.Context.instance()
    rep = ctx.socket(zmq.REP)
    rep.bind(f"tcp://127.0.0.1:{args.port}")
    pub = ctx.socket(zmq.PUB)
    pub_port = pub.bind_to_random_port("tcp://127.0.0.1")
    connected = threading.Event()

    def remote():
        while True:
            msg = rep.recv_string()
            if msg == "SUB_PORT":
                rep.send_string(str(pub_port))
                connected.set()
            elif msg.startswith("t"):
                rep.send_string(repr(time.monotonic() + CLOCK))
            else:
                rep.send_string("demo")

    threading.Thread(target=remote, daemon=True).start()
    cmd = [sys.executable, str(ROOT / "snapshotter.py"), "--source", "neon",
           "--pupil-port", str(args.port), *snapshotter_args]
    print("demo: starting", " ".join(cmd[1:]), flush=True)
    snapshotter = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, text=True)
    try:
        if not connected.wait(30):
            print("demo: Snapshotter did not connect", flush=True)
            return
        time.sleep(2)  # let Snapshotter subscribe and draw its markers
        t0 = time.monotonic()
        step = None
        while (s := time.monotonic() - t0) < length and snapshotter.poll() is None:
            (x, y), name = at(s)
            if name != step:
                step = name
                print(f"demo: {s:4.1f}s gaze -> {name}", flush=True)
            while says and s >= says[0][0]:
                _, command, phrase = says.pop(0)
                print(f"demo: {s:4.1f}s voice -> \u201c{phrase}\u201d", flush=True)
                snapshotter.stdin.write(command + "\n")
                snapshotter.stdin.flush()
            closed = any(a <= s < b for a, b in blinks)
            ts = time.monotonic() + CLOCK
            for eye in (0, 1):
                pub.send_multipart([f"pupil.{eye}.2d".encode(), msgpack.packb(
                    {"id": eye, "confidence": 0.02 if closed else 0.95, "timestamp": ts})])
            nx = (x - left) / (right - left) + 0.002 * math.sin(97 * s)
            ny = 1 - (y - top) / (bottom - top) + 0.002 * math.cos(89 * s)  # surface y points up
            pub.send_multipart([b"surfaces.screen", msgpack.packb({"name": "screen", "timestamp": ts,
                "gaze_on_surfaces": [{"norm_pos": [nx, ny], "confidence": 0.1 if closed else 0.95,
                                      "on_surf": True, "timestamp": ts}]})])
            time.sleep(1 / 120)
        print("demo: done", flush=True)
        time.sleep(1)
    finally:
        snapshotter.terminate()
        snapshotter.wait(5)


if __name__ == "__main__":
    main()
