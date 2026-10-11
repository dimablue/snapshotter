#!/usr/bin/env python3
"""One-command Pupil Capture setup for GazeSnap with the Neon.

    python tools/neon_setup.py install   # once, BEFORE starting Pupil Capture (asks for your password)
    python tools/neon_setup.py           # with Pupil Capture running: configure it
    python tools/neon_setup.py calibrate # start the screen-marker calibration

`install` copies the macOS scene-camera plugin into Pupil Capture's plugin folder (root-owned,
because Pupil Capture runs with sudo). Configuring then, over Pupil Remote:
  1. reads the scene camera through macOS (no "Premature end of JPEG file"),
  2. starts the Neon eye cameras,
  3. turns on the Surface Tracker,
  4. selects Screen Marker calibration with 2D gaze mapping (best for a screen at a fixed distance),
and reports errors Pupil Capture logs meanwhile, plus pupil confidence per eye.
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import msgpack
import zmq

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "pupil_plugins" / "neon_scene_macos.py"


def install(pupil_dir):
    target = Path(pupil_dir).expanduser() / "capture_settings" / "plugins"
    print(f"copying {PLUGIN.name} -> {target} (needs your password: the folder belongs to root)")
    cmd = ["sudo", "mkdir", "-p", str(target)]
    if subprocess.run(cmd).returncode or subprocess.run(["sudo", "cp", str(PLUGIN), str(target)]).returncode:
        sys.exit("copy failed")
    print("installed. Now start (or restart) Pupil Capture, then run: python tools/neon_setup.py")


class Remote:
    def __init__(self, host, port):
        self.ctx = zmq.Context.instance()
        self.req = self.ctx.socket(zmq.REQ)
        self.req.setsockopt(zmq.RCVTIMEO, 3000)
        self.req.setsockopt(zmq.LINGER, 0)
        self.req.connect(f"tcp://{host}:{port}")
        self.host = host

    def ask(self, msg):
        self.req.send_string(msg)
        return self.req.recv_string()

    def notify(self, subject, **payload):
        payload["subject"] = subject
        self.req.send_string(f"notify.{subject}", zmq.SNDMORE)
        self.req.send(msgpack.packb(payload, use_bin_type=True))
        return self.req.recv_string()

    def subscribe(self, *topics):
        sub = self.ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.ask('SUB_PORT')}")
        for t in topics:
            sub.subscribe(t)
        return sub


def watch(sub, seconds):
    """Collect pupil confidence per eye and print Pupil Capture's warnings/errors."""
    conf, seen = {}, set()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not sub.poll(200):
            continue
        topic, payload, *_ = sub.recv_multipart()
        topic, msg = topic.decode(), msgpack.unpackb(payload, raw=False)
        if topic.startswith("pupil.") and topic.endswith(".2d"):
            conf.setdefault(msg["id"], []).append(msg["confidence"])
        elif topic.startswith("logging."):
            text = str(msg.get("msg", ""))
            if "Premature end of JPEG" in text or text in seen:
                continue
            seen.add(text)
            print(f"  Pupil Capture {topic[8:].lower()}: {text[:160]}")
    return conf


def configure(remote, camera, seconds):
    try:
        version = remote.ask("v")
    except zmq.Again:
        sys.exit("Pupil Capture is not answering. Start it first:\n"
                 "  cd ~/Downloads/pupil/pupil_src && sudo ../.venv/bin/python main.py capture")
    print(f"connected to Pupil Capture {version}")
    sub = remote.subscribe("pupil.", "logging.warning", "logging.error", "logging.info")
    steps = [
        ("scene camera through macOS", "start_plugin",
         dict(name="Neon_Scene_MacOS_Source", args={"camera_name": camera})),
        ("Neon eye cameras", "neon_backend.auto_activate", dict(scene=False, eyes=True)),
        ("Surface Tracker", "start_plugin", dict(name="Surface_Tracker_Online", args={})),
        ("Screen Marker calibration, 2D gaze mapping", "start_plugin",
         dict(name="ScreenMarkerChoreographyPlugin", args={"selected_gazer_class_name": "Gazer2D"})),
    ]
    for label, subject, payload in steps:
        remote.notify(subject, **payload)
        print(f"- {label}")
    print(f"watching for {seconds:.0f} s…")
    conf = watch(sub, seconds)
    ok = True
    for eye in (0, 1):
        cs = sorted(conf.get(eye, []))
        if not cs:
            print(f"eye{eye}: no pupil data (eye window open? glasses on?)")
            ok = False
        else:
            med = cs[len(cs) // 2]
            print(f"eye{eye}: {len(cs) / seconds:.0f} Hz, median confidence {med:.2f}"
                  + ("" if med >= 0.6 else "  <- low: adjust the glasses / lighting"))
            ok &= med >= 0.6
    print("\nCheck the world window: it should show the room with no gray blocks, and its Video Source "
          "menu should say “Neon Scene (macOS camera)”.")
    print("Next: python tools/neon_setup.py calibrate" if ok else "Fix the eyes first, then calibrate.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", choices=["install", "configure", "calibrate"], default="configure")
    p.add_argument("--pupil-dir", default="~/Downloads/pupil")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50020)
    p.add_argument("--camera", default="Neon", help="macOS camera name to use for the scene camera")
    p.add_argument("--seconds", type=float, default=8)
    a = p.parse_args()
    if a.action == "install":
        return install(a.pupil_dir)
    remote = Remote(a.host, a.port)
    if a.action == "calibrate":
        print(remote.ask("C"))
        print("Look at the center of each bullseye; keep your head still. Accuracy prints in Pupil Capture.")
        sub = remote.subscribe("logging.info", "logging.error", "notify.calibration")
        end = time.monotonic() + 60
        while time.monotonic() < end:
            if not sub.poll(500):
                continue
            topic, payload, *_ = sub.recv_multipart()
            msg = msgpack.unpackb(payload, raw=False)
            text = str(msg.get("msg", msg.get("subject", "")))
            if "accuracy" in text.lower() or "precision" in text.lower() or "failed" in text.lower():
                print(" ", text)
            if "precision" in text.lower() or "calibration.failed" in text:
                break
        return
    configure(remote, a.camera, a.seconds)


if __name__ == "__main__":
    main()
