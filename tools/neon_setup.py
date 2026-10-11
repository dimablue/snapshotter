#!/usr/bin/env python3
"""One-command Pupil Capture setup for GazeSnap with the Neon.

    python tools/neon_setup.py install   # once, BEFORE starting Pupil Capture (asks for your password)
    python tools/neon_setup.py           # with Pupil Capture running: configure it
    python tools/neon_setup.py calibrate # start the screen-marker calibration
    python tools/neon_setup.py surface   # write the exact "screen" surface (asks for your password)
    python tools/neon_setup.py eyes      # restart stalled eye cameras ("no pupil data") without a replug

`install` copies the macOS scene-camera plugin into Pupil Capture's plugin folder (root-owned,
because Pupil Capture runs with sudo). Configuring then, over Pupil Remote:
  1. reads the scene camera through macOS (no "Premature end of JPEG file"),
  2. starts the Neon eye cameras,
  3. turns on the Surface Tracker,
  4. selects Screen Marker calibration with 2D gaze mapping (best for a screen at a fixed distance),
  5. turns off the 3D pupil detector, which 2D mapping doesn't need, to free CPU for the eyes,
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
# Pupil's default halves the image before looking for markers (quad_decimate 2.0). A laptop
# screen is small in the Neon's wide scene image, and halved markers aren't found at all.
TRACKER_ARGS = {"use_high_res": 1.0, "sharpen": 1.0}


def screen_markers(marker_size):
    """Screen-pixel corners of GazeSnap's markers, exactly as the overlay draws them."""
    from PyQt6.QtWidgets import QApplication
    sys.path.insert(0, str(ROOT))
    from overlay import marker_layout

    app = QApplication.instance() or QApplication(sys.argv[:1])
    screen = app.primaryScreen()
    corners, _ = marker_layout(screen.availableGeometry().intersected(screen.geometry()), marker_size)
    return {i: [(x, y), (x + marker_size, y), (x + marker_size, y + marker_size), (x, y + marker_size)]
            for i, (x, y) in corners.items()}


def surface_definition(verts, name="screen"):
    """Pupil Capture surface (definitions v01) spanning GazeSnap's markers, which is the frame
    sources/pupil_capture.py maps back to screen pixels. Surface coordinates have y up."""
    xs = [x for v in verts.values() for x, _ in v]
    ys = [y for v in verts.values() for _, y in v]
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)

    def uv(x, y):
        return [(x - left) / (right - left), 1 - (y - top) / (bottom - top)]

    markers = []
    for i, (tl, tr, br, bl) in sorted(verts.items()):
        # Pupil stores AprilTag corners as top-right, top-left, bottom-left, bottom-right
        markers.append({"uid": f"apriltag_v3:tag36h11:{i}", "verts_uv": [uv(*tr), uv(*tl), uv(*bl), uv(*br)]})
    surface = {"version": 1, "name": name, "real_world_size": {"x": 1.0, "y": 1.0},
               "reg_markers": markers, "registered_markers_dist": markers,
               "build_up_status": 1.0, "deprecated": False}
    return {"surfaces": [surface]}


def write_surface(pupil_dir, host, port, marker_size):
    import tempfile
    definition = surface_definition(screen_markers(marker_size))
    target = Path(pupil_dir).expanduser() / "capture_settings" / "surface_definitions_v01"
    with tempfile.NamedTemporaryFile(suffix=".surface", delete=False) as f:
        f.write(msgpack.packb(definition, use_bin_type=True))
    remote = Remote(host, port)
    try:
        remote.ask("v")
        running = True
    except zmq.Again:
        running = False
        remote = None
    if running:  # the tracker saves its own surfaces when it stops; stop it before replacing them
        Remote(host, port).notify("stop_plugin", name="Surface_Tracker_Online")
        time.sleep(1.5)
    print(f"writing the 'screen' surface (4 markers) -> {target} (needs your password)")
    if subprocess.run(["sudo", "cp", f.name, str(target)]).returncode:
        sys.exit("copy failed")
    Path(f.name).unlink()
    if running:
        Remote(host, port).notify("start_plugin", name="Surface_Tracker_Online", args=TRACKER_ARGS)
        print("Surface Tracker restarted with the new surface.")
    else:
        print("Pupil Capture isn't running; it will load the surface when it starts.")


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
    steps = [  # eyes first: Pupil's Neon activation looks for the scene camera on raw USB
        ("Neon eye cameras", "neon_backend.auto_activate", dict(scene=False, eyes=True)),
        ("scene camera through macOS", "start_plugin",
         dict(name="Neon_Scene_MacOS_Source", args={"camera_name": camera})),
        ("Surface Tracker (full-resolution marker detection)", "start_plugin",
         dict(name="Surface_Tracker_Online", args=TRACKER_ARGS)),
        ("Screen Marker calibration, 2D gaze mapping", "start_plugin",
         dict(name="ScreenMarkerChoreographyPlugin", args={"selected_gazer_class_name": "Gazer2D"})),
    ]
    for eye in ("eye0", "eye1"):
        # 2D mapping doesn't use the 3D eye model; it only costs CPU the 2D detector needs
        steps.append((f"3D pupil detector off in {eye}", "stop_eye_plugin",
                      dict(target=eye, name="Pye3DPlugin")))
    for label, subject, payload in steps:
        remote.notify(subject, **payload)
        print(f"- {label}")
    print(f"watching for {seconds:.0f} s…")
    conf = watch(sub, seconds)
    for eye in ("eye0", "eye1"):  # again: eye processes that were still starting missed the first one
        remote.notify("stop_eye_plugin", target=eye, name="Pye3DPlugin")
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
    p.add_argument("action", nargs="?", choices=["install", "configure", "calibrate", "surface", "eyes"], default="configure")
    p.add_argument("--pupil-dir", default="~/Downloads/pupil")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50020)
    p.add_argument("--camera", default="Neon", help="macOS camera name to use for the scene camera")
    p.add_argument("--seconds", type=float, default=8)
    p.add_argument("--marker-size", type=int, default=None, help="same as gazesnap.py --marker-size")
    a = p.parse_args()
    if a.action == "install":
        return install(a.pupil_dir)
    if a.action == "surface":
        if a.marker_size is None:
            sys.path.insert(0, str(ROOT))
            from overlay import DEFAULT_MARKER_SIZE
            a.marker_size = DEFAULT_MARKER_SIZE
        return write_surface(a.pupil_dir, a.host, a.port, a.marker_size)
    remote = Remote(a.host, a.port)
    if a.action == "eyes":
        sys.path.insert(0, str(ROOT))
        from sources.pupil_capture import restart_eye_cameras
        try:
            print("restarting the Neon eye cameras (about 10 s)…")
            restart_eye_cameras(remote.notify)
        except zmq.Again:
            sys.exit("Pupil Capture is not answering. Start it first.")
        conf = watch(remote.subscribe("pupil."), 4)
        for eye in (0, 1):
            cs = sorted(conf.get(eye, []))
            print(f"eye{eye}: " + (f"{len(cs) / 4:.0f} Hz, median confidence {cs[len(cs) // 2]:.2f}" if cs
                                   else "still no data: unplug and replug the Neon"))
        return
    if a.action == "calibrate":
        try:
            # Pupil forgets the 2D choice on restart, and the 3D mapper has no data once the 3D
            # detector is off ("Not sufficient pupil data"): select 2D every time.
            remote.notify("start_plugin", name="ScreenMarkerChoreographyPlugin",
                          args={"selected_gazer_class_name": "Gazer2D"})
            time.sleep(1)
            print(remote.ask("C"))
        except zmq.Again:
            sys.exit("Pupil Capture is not answering. Start it first:\n"
                     "  cd ~/Downloads/pupil/pupil_src && sudo ../.venv/bin/python main.py capture")
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
