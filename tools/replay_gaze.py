#!/usr/bin/env python3
"""Replay a session from tools/record_gaze.py into Snapshotter, as if the Neon were plugged in.

A stand-in for Pupil Capture's network API publishes the recorded pupil, gaze and surface
messages with their original timing (timestamps moved to now), and Snapshotter connects to it
with --source neon. The recorded dots are drawn too, so you can see how far Snapshotter's gaze
is from where the eyes really were.

    python tools/replay_gaze.py recordings/<date_time>
    python tools/replay_gaze.py recordings/<date_time> --loop --speed 0.5
    python tools/replay_gaze.py recordings/<date_time> --no-snapshotter   # then run snapshotter.py --pupil-port 50120 yourself

Extra arguments go to snapshotter.py, e.g. `--wake` or `--no-correction`.
"""
import argparse
import signal
import subprocess
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
from analyze_gaze import load  # noqa: E402
from overlay import Overlay  # noqa: E402

REPLAYED = ("pupil.", "gaze.", "surfaces.", "fixations", "blinks")


def retime(msg, shift):
    """Move every timestamp Snapshotter reads by `shift` seconds."""
    if "timestamp" in msg:
        msg["timestamp"] += shift
    for key in ("gaze_on_surfaces", "fixations_on_surfaces"):
        for g in msg.get(key, []):
            if "timestamp" in g:
                g["timestamp"] += shift
    return msg


class Player:
    def __init__(self, meta, events, messages, speed, loop):
        self.meta, self.events, self.speed, self.loop = meta, events, speed, loop
        self.messages = [m for m in messages if m[1].startswith(REPLAYED)]
        if not self.messages:
            sys.exit("nothing to replay: the session has no pupil/gaze/surface messages")
        self.t_first = self.messages[0][0]
        self.duration = self.messages[-1][0] - self.t_first
        self.started = None  # monotonic when the current pass started
        self.position = 0.0  # recorded seconds into the session, for the dot overlay

    def recorded_now(self):
        return None if self.started is None else (time.monotonic() - self.started) * self.speed

    def run(self, pub, clock_offset_now):
        while True:
            self.started = time.monotonic()
            for t, topic, payload in self.messages:
                due = self.started + (t - self.t_first) / self.speed
                if (wait := due - time.monotonic()) > 0:
                    time.sleep(wait)
                msg = msgpack.unpackb(payload, raw=False)
                # recorded Pupil time -> stand-in Pupil time at the moment it is sent
                recorded_pupil_t = t + self.meta["clock_offset"]
                shift = (time.monotonic() + clock_offset_now) - recorded_pupil_t
                pub.send_multipart([topic.encode(), msgpack.packb(retime(msg, shift), use_bin_type=True)])
            print(f"replay: end of session ({self.duration:.0f} s)", flush=True)
            if not self.loop:
                self.started = None
                return

    def target_at(self, recorded_s):
        """The dot shown at that point of the recording, and its phase name."""
        t = self.t_first + recorded_s
        shown, phase = None, None
        for ev in self.events:
            if ev["t"] > t:
                break
            if "target" in ev:
                shown, phase = ev["target"], ev["phase"]
            elif ev.get("start") or ev.get("end"):
                shown, phase = None, ev["phase"]
        return shown, phase


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("session", help="recordings/<date_time> folder")
    p.add_argument("--port", type=int, default=50120, help="stand-in Pupil Remote port")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--no-dots", action="store_true", help="don't draw the recorded dots")
    p.add_argument("--no-snapshotter", action="store_true", help="only serve the data")
    args, snapshotter_args = p.parse_known_args()

    meta, events, messages = load(args.session)
    player = Player(meta, events, messages, args.speed, args.loop)
    app = QApplication(sys.argv[:1])
    geom = app.primaryScreen().geometry()
    if [geom.width(), geom.height()] != meta["screen"][2:]:
        print(f"replay: note, recorded on a {meta['screen'][2]}x{meta['screen'][3]} screen, this one is "
              f"{geom.width()}x{geom.height()}: gaze is replayed relative to the markers", flush=True)

    clock_offset_now = 1000.0  # stand-in Pupil clock = monotonic + 1000
    ctx = zmq.Context.instance()
    rep = ctx.socket(zmq.REP)
    rep.bind(f"tcp://127.0.0.1:{args.port}")
    pub = ctx.socket(zmq.PUB)
    pub_port = pub.bind_to_random_port("tcp://127.0.0.1")
    connected = threading.Event()

    def remote():
        while True:
            request = rep.recv_multipart()[0].decode(errors="replace")
            if request == "SUB_PORT":
                rep.send_string(str(pub_port))
                connected.set()
            elif request == "t":
                rep.send_string(repr(time.monotonic() + clock_offset_now))
            elif request == "v":
                rep.send_string(f"replay of {meta.get('pupil_version')}")
            else:
                if request.startswith("notify."):
                    print(f"replay: Snapshotter sent {request} (ignored)", flush=True)
                rep.send_string("replay")

    threading.Thread(target=remote, daemon=True).start()

    snapshotter = None
    if args.no_snapshotter:
        print(f"replay: serving on port {args.port}. Run:\n  .venv/bin/python snapshotter.py --source neon "
              f"--pupil-port {args.port}", flush=True)
    else:
        cmd = [sys.executable, str(ROOT / "snapshotter.py"), "--source", "neon", "--pupil-port", str(args.port),
               "--surface", meta.get("surface", "screen"), *snapshotter_args]
        print("replay: starting", " ".join(cmd[1:]), flush=True)
        snapshotter = subprocess.Popen(cmd, cwd=ROOT)

    def start_playing():
        if not connected.wait(60):
            print("replay: nothing connected", flush=True)
            return
        time.sleep(2)  # let Snapshotter subscribe and draw its markers
        print(f"replay: playing {player.duration:.0f} s at {args.speed}x", flush=True)
        player.run(pub, clock_offset_now)

    threading.Thread(target=start_playing, daemon=True).start()

    overlay = None
    if not args.no_dots:
        overlay = Overlay(geom, show_gaze=False)
        overlay.show()

    def tick():
        if snapshotter is not None and snapshotter.poll() is not None:
            app.quit()
            return
        if overlay is None:
            return
        s = player.recorded_now()
        target, phase = (None, None) if s is None else player.target_at(s)
        if phase is None:
            overlay.check = None
        else:
            overlay.check = (target, 0, 0, f"replay {s:5.1f} / {player.duration:.0f} s, phase {phase}: "
                                           "red = where the eyes were")
        overlay.update()

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(16)
    try:
        app.exec()
    finally:
        if snapshotter is not None and snapshotter.poll() is None:
            snapshotter.terminate()
            snapshotter.wait(5)


if __name__ == "__main__":
    main()
