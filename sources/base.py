"""GazeSource: the one interface every piece of hardware implements.

A source runs in its own thread and pushes events onto a queue that the UI drains:
    ("gaze",   x, y, t)        x, y in screen pixels (logical points, origin top-left)
    ("blink",  t_start, t_end)  seconds on the time.monotonic() clock
    ("status", message)
"""
import threading
import time


class GazeSource:
    name = "base"
    # True -> the app feeds the mouse cursor in as gaze (no gaze hardware needed)
    uses_cursor = False
    # True -> the overlay draws AprilTag markers in the screen corners
    needs_markers = False

    def __init__(self, screen_w, screen_h, events, args):
        self.screen_w, self.screen_h = screen_w, screen_h
        self.events = events
        self.args = args
        self._stop = threading.Event()
        self._thread = None
        self.marker_verts = {}  # set by the app when needs_markers is True

    # --- called by subclasses -------------------------------------------------
    def emit_gaze(self, x, y, t=None):
        self.events.put(("gaze", float(x), float(y), t if t is not None else time.monotonic()))

    def emit_blink(self, t_start, t_end):
        self.events.put(("blink", float(t_start), float(t_end)))

    def emit_status(self, msg):
        self.events.put(("status", msg))

    @property
    def stopped(self):
        return self._stop.is_set()

    # --- lifecycle ----------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run_safe, name=self.name, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run_safe(self):
        try:
            self.run()
        except Exception as e:  # surface hardware errors in the UI instead of dying silently
            self.emit_status(f"{self.name} source error: {e!r}")
            raise

    def run(self):
        """Loop until self.stopped, calling emit_gaze / emit_blink."""
        raise NotImplementedError
