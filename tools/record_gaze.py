#!/usr/bin/env python3
"""Record a gaze session with known targets, so gaze accuracy can be debugged without the headset.

    python tools/record_gaze.py               # about 4 min: Pupil calibration, then dots
    python tools/record_gaze.py --no-calibrate
    python tools/record_gaze.py --quick       # short run to test the recorder itself

Needs Pupil Capture running and set up (python tools/neon_setup.py). The overlay draws GazeSnap's
corner markers and a red dot at known screen positions:

  still     5 x 5 dots, head still: accuracy across the screen
  pursuit   a slowly moving dot: accuracy while moving, and the gaze latency
  head      3 x 3 dots while you turn and tilt your head a little: head-movement error
  end       3 x 3 dots, head still: drift since the start

Saved to recordings/<date_time>/:
  stream.msgpack  every Pupil message (pupil, gaze, surfaces, notify, logging) with arrival time
  events.jsonl    the dot shown at each moment (screen px, monotonic s)
  meta.json       screen and marker geometry, clock offset, saved gaze correction, phases

It also makes a Pupil Capture recording (scene + eye videos, ~/recordings/...), which Pupil
Player can re-run offline: pupil detection, calibration, surface tracking.

Then: python tools/analyze_gaze.py recordings/<date_time>
      python tools/replay_gaze.py recordings/<date_time>   # feed it to GazeSnap
"""
import argparse
import json
import math
import os
import random
import signal
import sys
import threading
import time
from pathlib import Path

import msgpack
import zmq
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from neon_setup import Remote  # noqa: E402
from overlay import DEFAULT_MARKER_SIZE, Overlay, marker_layout  # noqa: E402

TOPICS = ("pupil.", "gaze.", "surfaces.", "notify.", "logging.", "fixations", "blinks")
CORRECTION_PATH = Path("~/.cache/gazesnap/gaze_correction.json").expanduser()


class StreamRecorder(threading.Thread):
    """Writes every subscribed Pupil message as msgpack [monotonic, topic, payload bytes]."""

    def __init__(self, ctx, address, path):
        super().__init__(daemon=True)
        self.sub = ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.connect(address)
        for t in TOPICS:
            self.sub.subscribe(t)
        self.path = path
        self.stop = threading.Event()
        self.counts = {}
        self.surface_gaze = 0
        self.notifications = []  # decoded notify.* messages, for the main thread
        self.lock = threading.Lock()

    def run(self):
        with open(self.path, "wb") as f:
            while not self.stop.is_set():
                if not self.sub.poll(200):
                    continue
                topic, payload, *_ = self.sub.recv_multipart()
                now = time.monotonic()
                topic = topic.decode(errors="replace")
                f.write(msgpack.packb([now, topic, payload], use_bin_type=True))
                key = topic if topic.startswith(("pupil.", "surfaces.")) else topic.split(".")[0]
                with self.lock:
                    self.counts[key] = self.counts.get(key, 0) + 1
                if topic.startswith("surfaces."):
                    msg = msgpack.unpackb(payload, raw=False)
                    self.surface_gaze += sum(g["confidence"] >= 0.6 for g in msg.get("gaze_on_surfaces", []))
                elif topic.startswith("notify."):
                    with self.lock:
                        self.notifications.append(msgpack.unpackb(payload, raw=False))
                elif topic.startswith(("logging.error", "logging.warning")):
                    msg = msgpack.unpackb(payload, raw=False)
                    print(f"  pupil {topic.split('.')[1]}: {msg.get('msg', '')}", flush=True)
        self.sub.close()

    def take_counts(self):
        with self.lock:
            counts, self.counts = self.counts, {}
        gaze, self.surface_gaze = self.surface_gaze, 0
        return counts, gaze

    def take_notifications(self):
        with self.lock:
            out, self.notifications = self.notifications, []
        return out


def avoid_markers(points, marker_rects, margin=24):
    """Drop points the dot would draw on top of a marker (or its white padding)."""
    def clear(x, y):
        return all(not (l - margin <= x <= r + margin and t - margin <= y <= b + margin)
                   for l, t, r, b in marker_rects)
    return [p for p in points if clear(*p)]


def build_phases(span, marker_rects, quick=False):
    left, top, right, bottom = span
    w, h = right - left, bottom - top

    def grid(fracs_x, fracs_y, seed):
        pts = avoid_markers([(left + fx * w, top + fy * h) for fy in fracs_y for fx in fracs_x], marker_rects)
        random.Random(seed).shuffle(pts)
        return pts

    five = [0.05, 0.275, 0.5, 0.725, 0.95]
    three = [0.15, 0.5, 0.85]
    if quick:
        return [
            {"name": "still", "kind": "dots", "targets": grid(three, three, 1)[:4], "settle": 0.8, "collect": 1.0,
             "text": "Look at the red dot ({n}/{total}). Keep your head still."},
            {"name": "pursuit", "kind": "pursuit", "seconds": 6.0,
             "text": "Follow the moving dot with your eyes. Keep your head still."},
        ]
    return [
        {"name": "still", "kind": "dots", "targets": grid(five, five, 1), "settle": 1.0, "collect": 1.5,
         "text": "Look at the red dot ({n}/{total}). Keep your head still."},
        {"name": "pursuit", "kind": "pursuit", "seconds": 25.0,
         "text": "Follow the moving dot with your eyes. Keep your head still."},
        {"name": "head", "kind": "dots", "targets": grid(three, three, 2), "settle": 1.0, "collect": 2.0,
         "text": "Keep looking at the dot ({n}/{total}) while you slowly turn and tilt your head a little."},
        {"name": "end", "kind": "dots", "targets": grid(three, three, 3), "settle": 1.0, "collect": 1.5,
         "text": "Last part: look at the red dot ({n}/{total}). Keep your head still."},
    ]


def pursuit_target(span, s):
    """Slow Lissajous path over the middle 75% of the marker area."""
    left, top, right, bottom = span
    cx, cy = (left + right) / 2, (top + bottom) / 2
    rx, ry = 0.375 * (right - left), 0.375 * (bottom - top)
    return cx + rx * math.sin(2 * math.pi * s / 12.0), cy + ry * math.sin(2 * math.pi * s / 8.0)


class Session:
    def __init__(self, args):
        self.args = args
        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        screen = self.app.primaryScreen()
        self.geom, self.avail = screen.geometry(), screen.availableGeometry()
        self.overlay = Overlay(self.geom, show_gaze=False)
        self.overlay.show()
        self.verts = self.overlay.place_markers(args.marker_size, self.avail)
        self.overlay.marker_opacity = args.marker_opacity
        self.overlay.marker_pad = None if args.marker_border < 0 else args.marker_border
        _, pad = marker_layout(self.avail.intersected(self.geom), args.marker_size)
        self.marker_rects = [(v[0][0] - pad, v[0][1] - pad, v[2][0] + pad, v[2][1] + pad)
                             for v in self.verts.values()]
        xs = [x for v in self.verts.values() for x, _ in v]
        ys = [y for v in self.verts.values() for _, y in v]
        self.span = (min(xs), min(ys), max(xs), max(ys))
        self.phases = build_phases(self.span, self.marker_rects, args.quick)

        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        self.dir = Path(args.out).expanduser() / stamp
        self.dir.mkdir(parents=True)
        self.stamp = stamp

        self.remote = Remote(args.host, args.port)
        try:
            sub_port = self.remote.ask("SUB_PORT")
            before = time.monotonic()
            pupil_now = float(self.remote.ask("t"))
            after = time.monotonic()
            self.pupil_version = self.remote.ask("v")
        except zmq.Again:
            sys.exit(f"Pupil Capture is not answering at {args.host}:{args.port}. Start it first:\n"
                     "  cd ~/Downloads/pupil/pupil_src && sudo ../.venv/bin/python main.py capture")
        self.offset = pupil_now - (before + after) / 2  # Pupil time - monotonic
        self.recorder = StreamRecorder(self.remote.ctx, f"tcp://{args.host}:{sub_port}", self.dir / "stream.msgpack")
        self.recorder.start()
        self.events = open(self.dir / "events.jsonl", "w")

        self.pupil_rec_path = None
        self.calibration = None
        self.steps = self.plan()
        self.step = None
        self.step_started = None
        self.last_target = object()
        self.last_stats = time.monotonic()
        self.finished = False
        self.started_at = time.time()

    # --- the script ------------------------------------------------------------
    def plan(self):
        steps = [("ready", None)]
        if not self.args.no_pupil_recording:
            steps.append(("pupil_record", None))
        if not self.args.no_calibrate:
            steps.append(("calibrate", None))
        steps += [("phase", p) for p in self.phases]
        steps.append(("done", None))
        return steps

    def log_event(self, **ev):
        ev["t"] = time.monotonic()
        self.events.write(json.dumps(ev) + "\n")

    def show(self, target, text, n=0, total=0):
        self.overlay.check = (target, n, total, text)
        self.overlay.update()

    def next_step(self):
        self.step = self.steps.pop(0)
        self.step_started = time.monotonic()
        kind, phase = self.step
        if kind == "phase":
            print(f"phase {phase['name']}", flush=True)
            self.log_event(phase=phase["name"], start=True)
        elif kind == "pupil_record":
            reply = self.remote.ask(f"R {self.stamp}")
            print(f"Pupil Capture recording: {reply}", flush=True)
        elif kind == "calibrate":
            self.overlay.hide()  # Pupil's calibration window needs the whole screen
            self.remote.notify("start_plugin", name="ScreenMarkerChoreographyPlugin",
                               args={"selected_gazer_class_name": "Gazer2D"})
            time.sleep(1)
            print("calibrating: look at the center of each bullseye", flush=True)
            self.log_event(phase="calibrate", start=True)
            self.remote.ask("C")

    def tick(self):
        if self.finished:
            return
        for n in self.recorder.take_notifications():
            subject = n.get("subject", "")
            if subject == "recording.started":
                self.pupil_rec_path = n.get("rec_path")
                print(f"  Pupil is recording to {self.pupil_rec_path}", flush=True)
            elif subject.startswith("calibration.") and subject.split(".")[1] in ("successful", "failed"):
                self.calibration = subject.split(".")[1]
                print(f"  calibration {self.calibration}: {n.get('reason', '')}", flush=True)
        now = time.monotonic()
        if now - self.last_stats >= 5:
            counts, gaze = self.recorder.take_counts()
            dt = now - self.last_stats
            rates = "  ".join(f"{k} {v / dt:.0f}/s" for k, v in sorted(counts.items()) if not k.startswith("notify"))
            print(f"  [{rates}]  screen gaze {gaze / dt:.0f}/s", flush=True)
            if gaze == 0 and self.step and self.step[0] == "phase":
                print("  !! no confident gaze on the screen surface: markers visible? eyes running?", flush=True)
            self.last_stats = now
        if self.step is None:
            self.next_step()
        kind, phase = self.step
        s = now - self.step_started
        if kind == "ready":
            left = self.args.ready - s
            if left <= 0:
                return self.next_step()
            self.show(None, f"Get comfortable, sit as you normally would. Starting in {math.ceil(left)} s")
        elif kind == "pupil_record":
            if self.pupil_rec_path or s > 3:
                self.next_step()
        elif kind == "calibrate":
            if self.calibration or s > 120:
                self.log_event(phase="calibrate", end=True, result=self.calibration)
                self.overlay.show()
                self.next_step()
        elif kind == "phase" and phase["kind"] == "dots":
            per = phase["settle"] + phase["collect"]
            i = int(s // per)
            if i >= len(phase["targets"]):
                return self.next_step()
            target = phase["targets"][i]
            if target != self.last_target:
                self.last_target = target
                self.log_event(phase=phase["name"], i=i, target=target)
            self.show(target, phase["text"].format(n=i + 1, total=len(phase["targets"])), i + 1,
                      len(phase["targets"]))
        elif kind == "phase" and phase["kind"] == "pursuit":
            if s >= phase["seconds"]:
                return self.next_step()
            target = pursuit_target(self.span, s)
            self.log_event(phase=phase["name"], target=target)
            self.show(target, phase["text"])
        elif kind == "done":
            self.finish()

    # --- wrap up ----------------------------------------------------------------
    def finish(self, aborted=False):
        if self.finished:
            return
        self.finished = True
        self.show(None, "Done, saving…")
        self.app.processEvents()
        if not self.args.no_pupil_recording:
            try:
                self.remote.ask("r")
            except zmq.Again:
                print("Pupil Capture did not answer the stop-recording request", flush=True)
            end = time.monotonic() + 5
            while time.monotonic() < end and not any(
                    n.get("subject") == "recording.stopped" for n in self.recorder.take_notifications()):
                time.sleep(0.2)
        time.sleep(0.5)
        self.recorder.stop.set()
        self.recorder.join(3)
        self.events.close()
        correction = None
        try:
            correction = json.loads(CORRECTION_PATH.read_text())
        except (OSError, ValueError):
            pass
        meta = {
            "aborted": aborted,
            "started": self.started_at,
            "ended": time.time(),
            "clock_offset": self.offset,
            "pupil_version": self.pupil_version,
            "pupil_rec_path": self.pupil_rec_path,
            "calibration": self.calibration,
            "screen": [self.geom.x(), self.geom.y(), self.geom.width(), self.geom.height()],
            "available": [self.avail.x(), self.avail.y(), self.avail.width(), self.avail.height()],
            "device_pixel_ratio": self.app.primaryScreen().devicePixelRatio(),
            "physical_size_mm": [self.app.primaryScreen().physicalSize().width(),
                                 self.app.primaryScreen().physicalSize().height()],
            "marker_size": self.args.marker_size,
            "marker_opacity": self.args.marker_opacity,
            "marker_border": self.args.marker_border,
            "marker_verts": {str(k): v for k, v in self.verts.items()},
            "span": self.span,
            "surface": self.args.surface,
            "correction": correction,
            "phases": self.phases,
        }
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=1))
        self.overlay.hide()
        print(f"\nsaved {self.dir}" + (" (stopped early)" if aborted else ""), flush=True)
        if self.pupil_rec_path:
            print(f"Pupil recording: {self.pupil_rec_path}", flush=True)
        self.app.quit()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50020, help="Pupil Remote port")
    p.add_argument("--surface", default="screen")
    p.add_argument("--marker-size", type=int, default=DEFAULT_MARKER_SIZE)
    p.add_argument("--marker-opacity", type=float, default=1.0, help="same as gazesnap.py")
    p.add_argument("--marker-border", type=int, default=-1, help="same as gazesnap.py")
    p.add_argument("--out", default=str(ROOT / "recordings"))
    p.add_argument("--ready", type=float, default=5.0, help="countdown before starting (s)")
    p.add_argument("--no-calibrate", action="store_true", help="skip the Pupil calibration at the start")
    p.add_argument("--no-pupil-recording", action="store_true", help="don't make a Pupil Capture recording")
    p.add_argument("--quick", action="store_true", help="short run (4 dots + 6 s pursuit) to test the recorder")
    args = p.parse_args()

    session = Session(args)
    signal.signal(signal.SIGINT, lambda *_: session.finish(aborted=True))
    timer = QTimer()
    timer.timeout.connect(session.tick)
    timer.start(16)
    session.app.exec()


if __name__ == "__main__":
    main()
