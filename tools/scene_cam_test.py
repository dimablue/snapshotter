#!/usr/bin/env python3
"""Is the Neon scene camera clean through macOS's own camera stack?

Pupil Capture reads the scene camera over raw USB (libuvc), which on macOS drops parts of
frames ("Premature end of JPEG file", gray blocks). This reads it the way FaceTime does
(AVFoundation) and reports how clean the frames are.

Run with the Neon plugged in and Pupil Capture NOT running (it takes the camera over):

    python tools/scene_cam_test.py                 # 10 s from the Neon scene camera
    python tools/scene_cam_test.py --camera "MacBook"  # any camera whose name contains this

Put GazeSnap's markers on screen first (e.g. `python tools/demo_neon.py`) and look at the
screen, to also measure AprilTag detection. A few frames are saved for a visual check.
"""
import argparse
import os
import time

import cv2
import numpy as np


def avfoundation_cameras():
    """Camera names in the order OpenCV's AVFoundation backend indexes them."""
    try:
        import AVFoundation as AVF
    except ImportError:
        return []
    return [str(d.localizedName()) for d in AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeVideo)]


def gray_block_fraction(gray):
    """Share of 16x16 blocks in the frame's lower half that are flat mid-gray: what a JPEG
    decoder fills in for data that never arrived."""
    h, w = gray.shape
    lower = gray[h // 2:h // 2 + (h // 2) // 16 * 16, :w // 16 * 16].astype(np.float32)
    blocks = lower.reshape(lower.shape[0] // 16, 16, -1, 16).swapaxes(1, 2).reshape(-1, 256)
    # decoder fill is perfectly uniform; smooth real surfaces still have a little noise
    flat = (blocks.std(axis=1) < 0.3) & (np.abs(blocks.mean(axis=1) - 128) < 3)
    return float(flat.mean())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--camera", default="Neon", help="pick the camera whose name contains this")
    p.add_argument("--index", type=int, help="OpenCV camera index (skips the name lookup)")
    p.add_argument("--size", nargs=2, type=int, default=[1280, 720], metavar=("W", "H"))
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--out", default=os.path.expanduser("~/Pictures/GazeSnap/scene-test"))
    a = p.parse_args()

    names = avfoundation_cameras()
    print("cameras macOS sees:", ", ".join(f"[{i}] {n}" for i, n in enumerate(names)) or "(pyobjc missing: pass --index)")
    index = a.index
    if index is None:
        matches = [i for i, n in enumerate(names) if a.camera.lower() in n.lower()]
        if not matches:
            print(f"\nNo camera named like {a.camera!r}. If the Neon is plugged in and not listed, "
                  "macOS doesn't expose its scene camera as a regular camera, and this route won't work. "
                  "Is Pupil Capture still running? Quit it, replug the Neon, and try again.")
            return
        index = matches[0]
    print(f"using [{index}] {names[index] if index < len(names) else ''}")

    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, a.size[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, a.size[1])
    cap.set(cv2.CAP_PROP_FPS, a.fps)
    if not cap.isOpened():
        print("could not open the camera (Camera permission for this terminal app?)")
        return
    for _ in range(10):  # let exposure settle
        cap.read()

    aruco = cv2.aruco
    detector = aruco.ArucoDetector(aruco.getPredefinedDictionary(aruco.DICT_APRILTAG_36h11))
    os.makedirs(a.out, exist_ok=True)
    stamps, gray_frames, tags_any, tags_all, failed = [], 0, 0, 0, 0
    size = None
    end = time.monotonic() + a.seconds
    while time.monotonic() < end:
        ok, frame = cap.read()
        if not ok:
            failed += 1
            continue
        stamps.append(time.monotonic())
        size = frame.shape[1], frame.shape[0]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if gray_block_fraction(gray) > 0.02:
            gray_frames += 1
        _, ids, _ = detector.detectMarkers(gray)
        found = set() if ids is None else set(ids.flatten().tolist())
        tags_any += bool(found)
        tags_all += {0, 1, 2, 3} <= found
        if len(stamps) in (1, 100, 200):
            cv2.imwrite(os.path.join(a.out, f"frame_{len(stamps):03d}.jpg"), frame)
    cap.release()

    n = len(stamps)
    if n < 2:
        print(f"no frames received ({failed} failed reads)")
        return
    gaps = np.diff(stamps)
    fps = (n - 1) / (stamps[-1] - stamps[0])
    late = int((gaps > 1.5 / a.fps).sum())
    print(f"\n{n} frames at {size[0]}x{size[1]}, {fps:.1f} fps (asked {a.fps}), "
          f"{late} late gaps, {failed} failed reads")
    print(f"frames with gray blocks: {gray_frames} ({100 * gray_frames / n:.0f}%)")
    print(f"frames with any marker: {100 * tags_any / n:.0f}%, all four markers: {100 * tags_all / n:.0f}%")
    print(f"sample frames: {a.out}")
    good = gray_frames / n < 0.02 and fps > 0.8 * a.fps
    print("\nVERDICT: " + ("clean. Use this camera route for Pupil Capture (tools/neon_setup.py)."
                           if good else "not clean either; send me this output."))


if __name__ == "__main__":
    main()
