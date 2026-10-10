"""Hardware-independent logic: gaze smoothing, box tracking, deliberate-blink detection.

Everything here works in screen pixels (logical points) and time.monotonic() seconds,
so it behaves the same whichever GazeSource feeds it.
"""
import math
from collections import deque


class LowPass:
    def __init__(self):
        self.y = None

    def __call__(self, x, alpha):
        self.y = x if self.y is None else alpha * x + (1 - alpha) * self.y
        return self.y


class OneEuroFilter:
    """One Euro filter (Casiez et al. 2012): heavy smoothing when still, light when moving."""

    def __init__(self, min_cutoff=1.0, beta=0.007, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x_filt, self.dx_filt = LowPass(), LowPass()
        self.t_prev = None
        self.x_prev = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.t_prev is None:
            self.t_prev, self.x_prev = t, x
            self.dx_filt(0.0, 1.0)
            return self.x_filt(x, 1.0)
        dt = max(t - self.t_prev, 1e-4)
        dx = (x - self.x_prev) / dt
        edx = self.dx_filt(dx, self._alpha(self.d_cutoff, dt))
        cutoff = self.min_cutoff + self.beta * abs(edx)
        self.t_prev, self.x_prev = t, x
        return self.x_filt(x, self._alpha(cutoff, dt))


class GazeFilter:
    def __init__(self, min_cutoff=1.0, beta=0.007):
        self.fx = OneEuroFilter(min_cutoff, beta)
        self.fy = OneEuroFilter(min_cutoff, beta)

    def __call__(self, x, y, t):
        return self.fx(x, t), self.fy(y, t)


class BoxTracker:
    """Keeps the capture box centred on filtered gaze, plus a short history for snapping."""

    def __init__(self, screen_w, screen_h, box_w=480, box_h=300,
                 stable_radius=60, stable_ms=300, history_s=1.5):
        self.screen_w, self.screen_h = screen_w, screen_h
        self.box_w, self.box_h = box_w, box_h
        self.stable_radius = stable_radius
        self.stable_s = stable_ms / 1000
        self.history = deque()  # (t, x, y) filtered
        self.history_s = history_s
        self.x, self.y = screen_w / 2, screen_h / 2
        self.stable = False

    def update(self, x, y, t):
        self.x, self.y = x, y
        self.history.append((t, x, y))
        while self.history and self.history[0][0] < t - self.history_s:
            self.history.popleft()
        recent = [(hx, hy) for ht, hx, hy in self.history if ht >= t - self.stable_s]
        span_ok = self.history and self.history[0][0] <= t - self.stable_s
        self.stable = bool(span_ok) and all(
            math.hypot(hx - x, hy - y) <= self.stable_radius for hx, hy in recent)

    def resize(self, factor):
        self.box_w = int(min(max(self.box_w * factor, 120), self.screen_w))
        self.box_h = int(min(max(self.box_h * factor, 80), self.screen_h))

    def rect_at(self, cx, cy):
        """(left, top, w, h) centred on cx, cy and clamped inside the screen."""
        w, h = self.box_w, self.box_h
        left = min(max(cx - w / 2, 0), self.screen_w - w)
        top = min(max(cy - h / 2, 0), self.screen_h - h)
        return int(left), int(top), int(w), int(h)

    def current_rect(self):
        return self.rect_at(self.x, self.y)

    def rect_before(self, t):
        """Box as it was just before time t (gaze drifts while the eyelid closes)."""
        pos = None
        for ht, hx, hy in self.history:
            if ht <= t - 0.05:
                pos = (hx, hy)
            else:
                break
        if pos is None:
            pos = (self.x, self.y)
        return self.rect_at(*pos)


class BlinkDetector:
    """Turns raw blink(start, end) events into deliberate snap gestures.

    gesture: "both" | "long" | "double"
    Returns the gesture start time when it fires, else None.
    """

    def __init__(self, long_min_ms=400, long_max_ms=1500, double_window_ms=600,
                 cooldown_ms=1500, gesture="both"):
        self.long_min = long_min_ms / 1000
        self.long_max = long_max_ms / 1000
        self.double_window = double_window_ms / 1000
        self.cooldown = cooldown_ms / 1000
        self.gesture = gesture
        self.last_short_start = None
        self.last_fire = -1e9

    def on_blink(self, t_start, t_end):
        if t_start < self.last_fire + self.cooldown:
            return None
        dur = t_end - t_start
        if dur > self.long_max:
            self.last_short_start = None  # eyes resting, not a gesture
            return None
        if dur >= self.long_min:
            if self.gesture in ("both", "long"):
                return self._fire(t_start, t_end)
            return None
        # short (natural-length) blink: candidate for a double blink
        if self.gesture in ("both", "double") and self.last_short_start is not None \
                and t_start - self.last_short_start <= self.double_window:
            first = self.last_short_start
            self._fire(first, t_end)
            return first
        self.last_short_start = t_start
        return None

    def _fire(self, t_start, t_end):
        self.last_fire = t_end
        self.last_short_start = None
        return t_start


class CropSelector:
    """Dwell selection independent of GUI and hardware. Times are monotonic seconds."""
    def __init__(self, screen_w, screen_h, dwell_ms=800, radius=35):
        self.screen_w, self.screen_h = screen_w, screen_h
        self.dwell_s, self.radius = dwell_ms / 1000, radius
        self.reset()

    def reset(self):
        self.state = "first"
        self.anchor = None
        self.rect = None
        self.point = None
        self.candidate = None
        self.started = None
        self.last_t = None
        self.progress = 0.0
        self.target = None

    def clear_dwell(self):
        self.candidate = self.started = self.target = None
        self.progress = 0.0

    def controls(self):
        width, height, gap = 140, 64, 20
        left = (self.screen_w - (3 * width + 2 * gap)) / 2
        top = max(0, self.screen_h - 170)
        return [(name, (int(left + i * (width + gap)), int(top), width, height))
                for i, name in enumerate(("Save", "Retry", "Cancel"))]

    def update(self, x, y, t):
        if self.last_t is not None and t - self.last_t > 0.25:
            self.clear_dwell()
        self.last_t = t
        x = min(max(x, 0), self.screen_w)
        y = min(max(y, 0), self.screen_h)
        self.point = (x, y)
        target = None
        if self.state == "preview":
            for name, (left, top, w, h) in self.controls():
                if left <= x < left + w and top <= y < top + h:
                    target = name
                    break
            if target is None:
                self.clear_dwell()
                return None
        elif self.state == "second":
            ax, ay = self.anchor
            self.rect = (int(min(ax, x)), int(min(ay, y)),
                         int(abs(x - ax)), int(abs(y - ay)))
            if self.rect[2] < 40 or self.rect[3] < 40:
                self.clear_dwell()
                return None
        elif self.state != "first":
            return None
        if (self.candidate is None or target != self.target
                or (target is None and math.hypot(x - self.candidate[0], y - self.candidate[1]) > self.radius)):
            self.candidate, self.started, self.target = (x, y), t, target
        self.progress = min(1.0, (t - self.started) / self.dwell_s)
        if self.progress < 1:
            return None
        point = self.candidate
        self.clear_dwell()
        if self.state == "first":
            self.anchor = point
            self.state = "second"
        elif self.state == "second":
            ax, ay = self.anchor
            px, py = point
            self.rect = (int(min(ax, px)), int(min(ay, py)), int(abs(px - ax)), int(abs(py - ay)))
            self.state = "preview"
        else:
            self.state = "done"
            return target.lower()
        return None
