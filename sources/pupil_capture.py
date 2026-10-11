"""Pupil Labs Neon over USB, through Pupil Capture (no Companion phone needed).

Pupil Capture runs the Neon cameras, detects pupils and maps gaze onto a surface defined by
the four AprilTags the overlay draws. This source reads that from Pupil Capture's network
API (Pupil Remote, default 127.0.0.1:50020).

Gaze: `surfaces.<name>` events. Surface coordinates are normalized to the rectangle the
markers span, with y pointing up.

Blinks: 2D pupil detection confidence from the eye cameras drops toward 0 while the eye is
closed. The eye counts as closed while every eye reporting recently is below
--blink-confidence. This needs no Pupil Capture plugin and adds no filter delay.

One-time setup in Pupil Capture: calibrate, enable the Surface Tracker plugin, then with
the markers visible click "Add surface" and name it "screen" (or pass --surface).
"""
import time

from .base import GazeSource

RECENT_S = 0.1  # pupil samples older than this don't count towards open/closed
EYE_RESTART_AFTER = 5  # s without pupil data -> restart Pupil's Neon eye cameras
EYE_RESTART_EVERY = 30  # ...at most this often
MIN_BLINK_S = 0.05  # ignore single-frame confidence dips


def restart_eye_cameras(notify, sleep=time.sleep):
    """Pupil's Neon eye-camera process sometimes stalls on macOS USB ("3 seconds since last
    frame… Attempting reinit") and never recovers. Restarting its Neon manager fixes it without
    a replug. `notify(subject, **payload)` sends a Pupil Remote notification."""
    notify("stop_plugin", name="Neon_Manager")
    sleep(4)
    notify("start_plugin", name="Neon_Manager", args={})
    sleep(2)
    notify("neon_backend.shared_camera.should_start")
    sleep(4)
    notify("neon_backend.auto_activate", scene=False, eyes=True)


class PupilCaptureSource(GazeSource):
    name = "neon"
    needs_markers = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.offset = 0.0  # Pupil Capture timestamp - time.monotonic()
        self.eye_conf = {}  # eye id -> (confidence, monotonic t)
        self.closed_since = None
        self.last_surface = self.last_pupil = time.monotonic()
        self.pupil_seen = self.eyes_restarted = time.monotonic()
        self.other_surfaces = set()

    def screen_rect(self):
        """(left, top, right, bottom) in screen pixels spanned by the markers."""
        pts = [p for verts in self.marker_verts.values() for p in verts]
        if not pts:
            return 0, 0, self.screen_w, self.screen_h
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    # --- message handlers (no I/O, so tests can call them directly) -----------
    def on_surface(self, msg):
        name = msg.get("name")
        if name != self.args.surface:
            if name not in self.other_surfaces:
                self.other_surfaces.add(name)
                self.emit_status(f"neon: ignoring surface {name!r} (pass --surface {name} to use it)")
            return
        self.last_surface = time.monotonic()
        left, top, right, bottom = self.screen_rect()
        for g in msg.get("gaze_on_surfaces", []):
            if g["confidence"] < self.args.min_confidence:
                continue
            nx, ny = g["norm_pos"]
            if not (-0.1 <= nx <= 1.1 and -0.1 <= ny <= 1.1):
                continue  # looking well off the screen
            # Pupil's surface y points up; screen y points down
            if not self.args.flip_y:
                ny = 1 - ny
            self.emit_gaze(left + nx * (right - left), top + ny * (bottom - top),
                           g["timestamp"] - self.offset)

    def on_pupil(self, msg):
        self.last_pupil = self.pupil_seen = time.monotonic()
        if getattr(self.args, "no_blink_capture", False):
            return
        t = msg["timestamp"] - self.offset
        self.eye_conf[msg["id"]] = (msg["confidence"], t)
        recent = [c for c, ts in self.eye_conf.values() if t - ts < RECENT_S]
        is_closed = max(recent) < self.args.blink_confidence
        if is_closed and self.closed_since is None:
            self.closed_since = t
        elif not is_closed and self.closed_since is not None:
            if t - self.closed_since >= MIN_BLINK_S:
                self.emit_blink(self.closed_since, t)
            self.closed_since = None

    # --- network ---------------------------------------------------------------
    def run(self):
        import msgpack
        import zmq

        ctx = zmq.Context.instance()
        addr = f"tcp://{self.args.pupil_host}:{self.args.pupil_port}"
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 2000)
        req.setsockopt(zmq.LINGER, 0)
        req.connect(addr)
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        try:
            try:
                req.send_string("SUB_PORT")
                sub_port = req.recv_string()
                before = time.monotonic()
                req.send_string("t")
                pupil_now = float(req.recv_string())
                after = time.monotonic()
            except zmq.Again:
                self.emit_status(f"neon: Pupil Capture not answering at {addr}. Is it running?")
                return
            self.offset = pupil_now - (before + after) / 2
            sub.connect(f"tcp://{self.args.pupil_host}:{sub_port}")
            sub.subscribe("surfaces.")
            sub.subscribe("pupil.")
            self.emit_status("neon: connected to Pupil Capture")
            self.last_surface = self.last_pupil = time.monotonic()
            self.pupil_seen = self.eyes_restarted = time.monotonic()

            def notify(subject, **payload):
                payload["subject"] = subject
                req.send_string(f"notify.{subject}", zmq.SNDMORE)
                req.send(msgpack.packb(payload, use_bin_type=True))
                req.recv_string()

            while not self.stopped:
                now = time.monotonic()
                if now - self.pupil_seen > EYE_RESTART_AFTER and now - self.eyes_restarted > EYE_RESTART_EVERY:
                    self.eyes_restarted = now
                    self.emit_status("neon: eye cameras stalled; restarting them (about 10 s)")
                    try:
                        restart_eye_cameras(notify)
                    except zmq.Again:
                        self.emit_status("neon: Pupil Capture stopped answering")
                    self.pupil_seen = time.monotonic()
                if now - self.last_surface > 3:
                    self.emit_status(f"neon: no gaze on surface {self.args.surface!r}. In Pupil Capture: "
                                     "Surface Tracker on, surface added, markers in view, calibrated")
                    self.last_surface = now
                if now - self.last_pupil > 3:
                    self.emit_status("neon: no pupil data; are Pupil Capture's eye windows running?")
                    self.last_pupil = now
                if not sub.poll(500):
                    continue
                topic, payload, *_ = sub.recv_multipart()
                topic = topic.decode()
                msg = msgpack.unpackb(payload, raw=False)
                if topic.startswith("surfaces."):
                    self.on_surface(msg)
                elif topic.startswith("pupil.") and topic.endswith(".2d"):
                    self.on_pupil(msg)
        finally:
            req.close()
            sub.close()
