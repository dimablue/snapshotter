#!/usr/bin/env python3
"""Run this first at the lab: finds the Neon, then reports gaze rate and which blink signals arrive.

    python tools/neon_check.py [--ip 192.168.x.x] [--seconds 10]

Blink a few times (some long) while it runs.
"""
import argparse
import time

from pupil_labs.realtime_api.simple import Device, discover_one_device


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ip")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--seconds", type=float, default=10)
    a = p.parse_args()

    print("Looking for Neon..." if not a.ip else f"Connecting to {a.ip}:{a.port}...")
    device = Device(address=a.ip, port=a.port) if a.ip else discover_one_device(max_search_duration_seconds=10)
    if device is None:
        print("No device found. Put the phone and laptop on the same network (a phone hotspot works), "
              "open the Companion app, or pass --ip.")
        return
    print(f"Connected: phone={device.phone_name} module={device.module_serial}")
    print(f"Recording for {a.seconds:.0f} s - blink normally, then do some long blinks...")

    n_gaze, blinks, apertures, has_eyelid = 0, [], [], None
    t_end = time.monotonic() + a.seconds
    try:
        while time.monotonic() < t_end:
            item = device.receive_matched_scene_video_frame_and_gaze(timeout_seconds=0.5)
            if item is not None:
                n_gaze += 1
                g = item.gaze
                if has_eyelid is None:
                    has_eyelid = hasattr(g, "eyelid_aperture_left")
                    print(f"gaze type: {type(g).__name__} (eyelid aperture: {'yes' if has_eyelid else 'no'})")
                if has_eyelid:
                    apertures.append((g.eyelid_aperture_left + g.eyelid_aperture_right) / 2)
            while True:
                ev = device.receive_eye_events(timeout_seconds=0)
                if ev is None:
                    break
                if type(ev).__name__ == "BlinkEventData":
                    dur = (ev.end_time_ns - ev.start_time_ns) / 1e6
                    blinks.append(dur)
                    print(f"  blink event: {dur:.0f} ms")
    finally:
        device.close()

    print("\n--- summary ---")
    print(f"matched scene+gaze rate: {n_gaze / a.seconds:.1f} Hz")
    print(f"blink events: {len(blinks)}" + (f"  (durations ms: {[round(b) for b in blinks]})" if blinks else
          "  -> none: enable real-time blink detection in the Companion app, or use --blink-mode eyelid"))
    if apertures:
        print(f"eyelid aperture mm: min {min(apertures):.1f}, median {sorted(apertures)[len(apertures)//2]:.1f}, "
              f"max {max(apertures):.1f}")
    print("\nNext: python gazesnap.py --source neon" + (f" --neon-ip {a.ip}" if a.ip else ""))


if __name__ == "__main__":
    main()
