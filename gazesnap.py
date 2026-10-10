#!/usr/bin/env python3
"""Snapshotter: look at something, blink, and it's captured (and optionally described aloud).

    python gazesnap.py --source mouse            # cursor = gaze, Enter in this terminal = snap
    python gazesnap.py --source webcam --narrate # real blinks from the laptop camera
    python gazesnap.py --source neon --narrate   # Pupil Labs Neon eye tracker

Terminal commands while running (type, then Enter):
    (empty)  snap now      +/-  resize box      p  pause/resume
    n        toggle narration                   q  quit
"""
import argparse
import os
import queue
import signal
import sys
import threading
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCursor, QImage
from PyQt6.QtWidgets import QApplication, QInputDialog, QLineEdit

from capture import grab_region, play_shutter
from core import BlinkDetector, BoxTracker, GazeFilter
from overlay import Overlay, DragHandle, ResizeHandle
from sources import SOURCE_NAMES, get_source_class


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=SOURCE_NAMES, default="mouse")
    p.add_argument("--narrate", action="store_true", help="describe each snap with Claude and speak it")
    p.add_argument("--model", default="claude-opus-5-5", help="Claude model for narration")
    p.add_argument("--voice", default=None, help="macOS `say` voice, e.g. Samantha")
    p.add_argument("--out", default=os.path.expanduser("~/Pictures/GazeSnap"))
    p.add_argument("--box", nargs=2, type=int, default=[480, 300], metavar=("W", "H"))
    p.add_argument("--no-gaze-dot", action="store_true", help="hide the red gaze dot")
    p.add_argument("--no-sound", action="store_true")
    p.add_argument("--position", choices=["manual", "follow"], default=None,
                   help="default: manual with mouse/webcam, follow with Neon")
    p.add_argument("--voice-control", action="store_true", help="listen with ElevenLabs for screenshot commands")
    p.add_argument("--no-blink-capture", action="store_true", help="capture only with Enter or voice")
    # smoothing
    p.add_argument("--min-cutoff", type=float, default=1.0, help="lower = smoother, laggier box")
    p.add_argument("--beta", type=float, default=0.007, help="higher = box catches fast glances sooner")
    # blink gesture
    p.add_argument("--blink-gesture", choices=["both", "long", "double"], default="both")
    p.add_argument("--long-blink-ms", type=int, default=400)
    p.add_argument("--long-blink-max-ms", type=int, default=1500)
    p.add_argument("--double-window-ms", type=int, default=600)
    p.add_argument("--cooldown-ms", type=int, default=1500)
    # webcam
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--ear-ratio", type=float, default=0.7, help="eye closed below this x open-eye EAR")
    # neon
    p.add_argument("--neon-ip", default=None, help="skip discovery; IP shown in the Companion app")
    p.add_argument("--neon-port", type=int, default=8080)
    p.add_argument("--blink-mode", choices=["auto", "eyelid", "events"], default="auto")
    p.add_argument("--eyelid-ratio", type=float, default=0.4, help="closed below this x open aperture")
    p.add_argument("--flip-y", action="store_true", help="use if the box moves opposite to your gaze vertically")
    p.add_argument("--marker-size", type=int, default=120)
    return p.parse_args()


class GazeSnapApp:
    def __init__(self, args):
        self.args = args
        self.app = QApplication(sys.argv)
        screen = self.app.primaryScreen()
        geom = screen.geometry()
        self.screen_w, self.screen_h = geom.width(), geom.height()

        self.overlay = Overlay(geom, show_gaze=not args.no_gaze_dot)
        self.overlay.show()
        self.app.processEvents()

        self.events = queue.Queue()
        self.source = get_source_class(args.source)(self.screen_w, self.screen_h, self.events, args)
        if self.source.needs_markers:
            self.source.marker_verts = self.overlay.place_markers(args.marker_size, screen.availableGeometry())

        self.filter = GazeFilter(args.min_cutoff, args.beta)
        self.tracker = BoxTracker(self.screen_w, self.screen_h, *args.box)
        self.manual = (args.position or ("follow" if args.source == "neon" else "manual")) == "manual"
        self.handle = DragHandle()
        self.handle.moved.connect(self.move_box)
        self.resize_handle = ResizeHandle()
        self.resize_handle.moved.connect(self.resize_box)
        self.voice_control = None
        self.detector = BlinkDetector(args.long_blink_ms, args.long_blink_max_ms,
                                      args.double_window_ms, args.cooldown_ms, args.blink_gesture)
        self.narrator = None
        self.narrate = args.narrate
        if args.narrate:
            self._make_narrator()

        self.paused = False
        self.snapping = False
        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)

    def move_box(self, left, top):
        self.manual = True
        self.tracker.history.clear()
        self.tracker.update(left + self.tracker.box_w / 2,
                            top + self.tracker.box_h / 2, time.monotonic())

    def resize_box(self, right, bottom):
        self.manual = True
        self.tracker.resize_to(right, bottom)

    def toggle_voice(self):
        if self.voice_control is not None:
            self.voice_control.stop()
            self.voice_control = None
            self.overlay.voice_status = "Voice off • type v + Enter to enable ElevenLabs"
            self.log("ElevenLabs microphone off")
            return
        if not os.environ.get("ELEVENLABS_API_KEY"):
            key, accepted = QInputDialog.getText(
                None, "Enable ElevenLabs voice commands",
                "Microphone audio streams to ElevenLabs while enabled.\n"
                "Enter your ElevenLabs API key (kept only for this session):",
                QLineEdit.EchoMode.Password)
            if not accepted or not key.strip():
                self.log("Voice off: no ElevenLabs key provided; type v + Enter to retry")
                return
            os.environ["ELEVENLABS_API_KEY"] = key.strip()
        from voice_control import VoiceControl
        self.voice_control = VoiceControl(self.events)
        self.overlay.voice_status = "Voice: connecting to ElevenLabs…"
        self.voice_control.start()

    def _make_narrator(self):
        try:
            from narrator import Narrator
            self.narrator = Narrator(self.args.model, self.args.voice,
                                     on_text=lambda t: self.events.put(("status", "“" + t + "”")))
        except Exception as e:
            self.narrate = False
            self.log(f"narration off: {e}")

    def log(self, msg):
        print(f"[gazesnap] {msg}", flush=True)
        self.overlay.set_status(msg)
        if msg.startswith("voice:"):
            self.overlay.voice_status = msg

    # --- main loop ------------------------------------------------------------
    def tick(self):
        if self.source.uses_cursor:
            p = QCursor.pos()
            self.events.put(("gaze", p.x(), p.y(), time.monotonic()))
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            kind = ev[0]
            if kind == "gaze":
                _, x, y, t = ev
                fx, fy = self.filter(x, y, t)
                if not self.manual:
                    self.tracker.update(fx, fy, t)
            elif kind == "blink":
                _, t0, t1 = ev
                if not self.paused and not self.args.no_blink_capture:
                    start = self.detector.on_blink(t0, t1)
                    if start is not None:
                        self.snap(self.tracker.rect_before(start))
            elif kind == "status":
                self.log(ev[1])
            elif kind == "cmd":
                self.command(ev[1])

        self.overlay.box = self.tracker.current_rect()
        self.overlay.gaze = (self.tracker.x, self.tracker.y)
        self.overlay.stable = self.tracker.stable
        self.overlay.paused = self.paused
        left, top, width, height = self.overlay.box
        self.handle.move(left, top)
        self.handle.setVisible(self.manual and not self.paused and not self.snapping)
        self.resize_handle.move(left + width - 26, top + height - 26)
        self.resize_handle.setVisible(self.manual and not self.paused and not self.snapping)
        self.overlay.update()

    def command(self, cmd):
        if cmd == "":
            self.snap(self.tracker.current_rect())
        elif cmd in ("+", "="):
            self.tracker.resize(1.2)
        elif cmd == "-":
            self.tracker.resize(1 / 1.2)
        elif cmd == "p":
            self.paused = not self.paused
            self.log("paused" if self.paused else "resumed")
        elif cmd == "m":
            self.manual = not self.manual
            self.tracker.history.clear()
            self.filter = GazeFilter(self.args.min_cutoff, self.args.beta)
            self.log("manual: drag the blue handle" if self.manual else "following gaze/cursor")
        elif cmd == "v":
            self.toggle_voice()
        elif cmd == "n":
            self.narrate = not self.narrate
            if self.narrate and self.narrator is None:
                self._make_narrator()
            self.log(f"narration {'on' if self.narrate else 'off'}")
        elif cmd == "q":
            self.app.quit()
        else:
            print("commands: Enter=snap  +/-=resize  p=pause  m=manual/follow  v=voice  n=narration  q=quit", flush=True)

    # --- capture ----------------------------------------------------------------
    def snap(self, rect):
        if self.snapping or self.paused:
            return
        self.snapping = True
        self.handle.hide()
        self.resize_handle.hide()
        self.overlay.hidden_for_capture = True
        self.overlay.repaint()
        # give the window server a frame or two to drop the overlay before grabbing
        QTimer.singleShot(80, lambda: self._finish_snap(rect))

    def _finish_snap(self, rect):
        try:
            path = grab_region(rect, self.args.out)
        except Exception as e:
            path = None
            self.log(f"capture failed: {e} (grant Screen Recording to your terminal)")
        self.overlay.hidden_for_capture = False
        self.snapping = False
        if not path:
            return
        if not self.args.no_sound:
            play_shutter()
        self.overlay.flash(rect)
        self.app.clipboard().setImage(QImage(path))
        self.log(f"saved {path}")
        if self.narrate and self.narrator:
            self.overlay.set_status("Describing…", 10)
            self.narrator.describe_async(path)

    # --- run ----------------------------------------------------------------
    def _read_stdin(self):
        for line in sys.stdin:
            self.events.put(("cmd", line.strip()))

    def run(self):
        signal.signal(signal.SIGINT, lambda *_: self.app.quit())
        threading.Thread(target=self._read_stdin, daemon=True).start()
        self.source.start()
        if self.args.voice_control:
            QTimer.singleShot(0, self.toggle_voice)
        self.timer.start(16)  # ~60 Hz; also lets Python handle Ctrl+C
        hint = "press Enter in the terminal" if self.source.uses_cursor and self.args.source == "mouse" \
            else "long blink or double blink"
        self.log(f"GazeSnap ({self.args.source}): look at something, {hint} to snap")
        code = self.app.exec()
        self.source.stop()
        if self.voice_control:
            self.voice_control.stop()
        self.handle.close()
        self.resize_handle.close()
        return code


if __name__ == "__main__":
    sys.exit(GazeSnapApp(parse_args()).run())
