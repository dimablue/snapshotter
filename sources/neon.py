"""Pupil Labs Neon source.

Gaze: the Neon scene camera sees four AprilTag markers that the overlay draws in the screen
corners. pupil-labs-real-time-screen-gaze maps scene-camera gaze into screen pixels.

Blinks, chosen with --blink-mode:
  eyelid  eyelid aperture (mm) streamed with each gaze sample; lowest latency.
          Needs eyestate/eyelid streaming switched on in the Companion app.
  events  blink events from the Companion app's on-device blink detector.
  auto    eyelid if the gaze samples carry aperture, else events (default).
"""
import time

from .base import GazeSource


class NeonSource(GazeSource):
    name = "neon"
    needs_markers = True

    def _connect(self):
        from pupil_labs.realtime_api.simple import Device, discover_one_device

        if self.args.neon_ip:
            self.emit_status(f"neon: connecting to {self.args.neon_ip}:{self.args.neon_port}")
            return Device(address=self.args.neon_ip, port=self.args.neon_port)
        self.emit_status("neon: searching the network for a device (10 s)...")
        return discover_one_device(max_search_duration_seconds=10)

    def run(self):
        from pupil_labs.real_time_screen_gaze.gaze_mapper import GazeMapper

        device = self._connect()
        if device is None:
            self.emit_status("neon: no device found. Same Wi-Fi/hotspot? Or pass --neon-ip")
            return
        self.emit_status(f"neon: connected to {device.phone_name}")

        mapper = GazeMapper(device.get_calibration())
        surface = mapper.add_surface(self.marker_verts, (self.screen_w, self.screen_h))

        mode = self.args.blink_mode
        baseline = None
        closed_since = None
        last_seen_screen = time.monotonic()
        try:
            while not self.stopped:
                item = device.receive_matched_scene_video_frame_and_gaze(timeout_seconds=1)
                if item is None:
                    continue
                now = time.monotonic()
                gaze = item.gaze

                # --- gaze -> screen ---
                result = mapper.process_frame(item.frame, gaze)
                mapped = result.mapped_gaze.get(surface.uid, []) if result else []
                for sg in mapped:
                    if not (-0.1 <= sg.x <= 1.1 and -0.1 <= sg.y <= 1.1):
                        continue  # looking well off the screen
                    # mapper output has y pointing up; screen y points down
                    y = sg.y if self.args.flip_y else 1 - sg.y
                    self.emit_gaze(sg.x * self.screen_w, y * self.screen_h, now)
                    last_seen_screen = now
                if now - last_seen_screen > 2:
                    self.emit_status("neon: screen markers not visible")
                    last_seen_screen = now

                if mode == "auto":
                    mode = "eyelid" if hasattr(gaze, "eyelid_aperture_left") else "events"
                    self.emit_status(f"neon: blink mode = {mode}")

                # --- blinks ---
                if mode == "eyelid":
                    ap = (gaze.eyelid_aperture_left + gaze.eyelid_aperture_right) / 2
                    if baseline is None:
                        baseline = max(ap, 1e-3)
                    is_closed = ap < baseline * self.args.eyelid_ratio
                    if not is_closed:
                        baseline = 0.97 * baseline + 0.03 * ap
                    if is_closed and closed_since is None:
                        closed_since = now
                    elif not is_closed and closed_since is not None:
                        self.emit_blink(closed_since, now)
                        closed_since = None
                else:
                    while True:
                        ev = device.receive_eye_events(timeout_seconds=0)
                        if ev is None:
                            break
                        if type(ev).__name__ != "BlinkEventData":
                            continue
                        dur = (ev.end_time_ns - ev.start_time_ns) / 1e9
                        # how long after the blink ended the phone sent it (same device clock)
                        delay = max(0.0, ev.rtp_ts_unix_seconds - ev.end_time_ns / 1e9)
                        t_end = now - delay
                        self.emit_blink(t_end - dur, t_end)
        finally:
            device.close()
