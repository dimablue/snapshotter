#!/usr/bin/env python3
"""Pupil Capture check: connects over Pupil Remote, then reports pupil confidence per eye,
blinks found at --blink-confidence, and which surfaces carry gaze.

    python tools/pupil_check.py [--host 127.0.0.1] [--port 50020] [--seconds 10]

Start Pupil Capture with the Neon plugged in first. Blink a few times (some long) while it runs.
"""
import argparse
import time

import msgpack
import zmq


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50020)
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--blink-confidence", type=float, default=0.3)
    a = p.parse_args()

    ctx = zmq.Context.instance()
    req = ctx.socket(zmq.REQ)
    req.setsockopt(zmq.RCVTIMEO, 2000)
    req.setsockopt(zmq.LINGER, 0)
    req.connect(f"tcp://{a.host}:{a.port}")
    try:
        req.send_string("SUB_PORT")
        sub_port = req.recv_string()
        req.send_string("v")
        version = req.recv_string()
    except zmq.Again:
        print(f"Pupil Capture not answering at {a.host}:{a.port}. Is it running? "
              "(Pupil Remote plugin must be on; it is by default.)")
        return
    print(f"Connected to Pupil Capture {version}. Recording for {a.seconds:.0f} s, starting now:\n"
          "  with Snapshotter's markers on screen, look at each screen corner for ~2 s "
          "(top-left, top-right, bottom-right, bottom-left), and blink a few times.")

    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://{a.host}:{sub_port}")
    sub.subscribe("pupil.")
    sub.subscribe("surfaces.")

    conf = {}       # eye id -> [confidence, ...]
    latest = {}     # eye id -> (confidence, timestamp)
    surfaces = {}   # name -> gaze samples
    reach = {}      # name -> list of confident (x, y) on the surface
    blinks = []
    closed_since = None
    end = time.monotonic() + a.seconds
    while time.monotonic() < end:
        if not sub.poll(200):
            continue
        topic, payload, *_ = sub.recv_multipart()
        topic = topic.decode()
        msg = msgpack.unpackb(payload, raw=False)
        if topic.startswith("surfaces."):
            surfaces[msg["name"]] = surfaces.get(msg["name"], 0) + len(msg["gaze_on_surfaces"])
            reach.setdefault(msg["name"], []).extend(
                g["norm_pos"] for g in msg["gaze_on_surfaces"] if g["confidence"] > 0.6)
        elif topic.endswith(".2d"):
            t = msg["timestamp"]
            conf.setdefault(msg["id"], []).append(msg["confidence"])
            latest[msg["id"]] = (msg["confidence"], t)
            is_closed = max(c for c, ts in latest.values() if t - ts < 0.1) < a.blink_confidence
            if is_closed and closed_since is None:
                closed_since = t
            elif not is_closed and closed_since is not None:
                if t - closed_since >= 0.05:
                    blinks.append(t - closed_since)
                closed_since = None

    if not conf:
        print("no pupil data -> are the eye windows open and showing your eyes?")
    for eye, cs in sorted(conf.items()):
        cs = sorted(cs)
        print(f"eye{eye}: {len(cs) / a.seconds:.0f} Hz, confidence min {cs[0]:.2f}, "
              f"median {cs[len(cs) // 2]:.2f}, max {cs[-1]:.2f}")
    print(f"blinks below {a.blink_confidence}: " +
          (", ".join(f"{d * 1000:.0f} ms" for d in blinks) if blinks else "none"))
    if surfaces:
        for name, n in surfaces.items():
            print(f"surface {name!r}: {n / a.seconds:.0f} gaze samples/s")
            pts = reach.get(name, [])
            if pts:
                xs, ys = sorted(p[0] for p in pts), sorted(p[1] for p in pts)
                lo, hi = int(len(pts) * 0.02), int(len(pts) * 0.98) - 1  # ignore stray samples
                print(f"  gaze reached x {xs[lo]:.2f}..{xs[hi]:.2f}, y {ys[lo]:.2f}..{ys[hi]:.2f} "
                      "(look at all four corners: both should span about 0..1)")
    else:
        print("no surface data (fine for now; Snapshotter's markers must be on screen for this)")
    print("\nNext: python snapshotter.py --source neon")


if __name__ == "__main__":
    main()
