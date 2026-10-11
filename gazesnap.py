#!/usr/bin/env python3
"""Snapshotter: look at something, blink, and it's captured (and optionally described aloud).

    python gazesnap.py --source mouse            # cursor = gaze, Enter in this terminal = snap
    python gazesnap.py --source webcam --narrate # real blinks from the laptop camera
    python gazesnap.py --source neon --narrate   # Pupil Labs Neon over USB via Pupil Capture
    python gazesnap.py --source neon --wake      # Siri-style: hidden until "hey Neon, take a screenshot"

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
from PyQt6.QtGui import QAction, QColor, QCursor, QIcon, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMenu, QSystemTrayIcon

from capture import grab_region, play_shutter
from core import BlinkDetector, BoxTracker, GazeFilter, CropSelector
from overlay import Overlay, DragHandle, ResizeHandle
from sources import SOURCE_NAMES, get_source_class


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--selection", choices=["box", "crop"], default="box")
    p.add_argument("--dwell-ms", type=int, default=800)
    p.add_argument("--source", choices=SOURCE_NAMES, default="mouse")
    p.add_argument("--narrate", action="store_true", help="describe each snap with Claude and speak it")
    p.add_argument("--analysis", action=argparse.BooleanOptionalAction, default=None,
                   help="ElevenLabs spoken image analysis (on by default with --wake)")
    p.add_argument("--analysis-agent", default=None, help="ElevenLabs image-capable agent ID")
    p.add_argument("--model", default="claude-opus-5-5", help="Claude model for narration")
    p.add_argument("--voice", default=None, help="macOS `say` voice, e.g. Samantha")
    p.add_argument("--out", default=os.path.expanduser("~/Pictures/GazeSnap"))
    p.add_argument("--box", nargs=2, type=int, default=[480, 300], metavar=("W", "H"))
    p.add_argument("--no-gaze-dot", action="store_true", help="hide the red gaze dot")
    p.add_argument("--no-sound", action="store_true")
    p.add_argument("--position", choices=["manual", "follow"], default=None,
                   help="default: manual with mouse/webcam, follow with Neon")
    p.add_argument("--voice-control", action="store_true", help="listen with ElevenLabs for screenshot commands")
    p.add_argument("--wake", action="store_true",
                   help="Siri-style: hidden until you say “hey Neon, take a screenshot” (detected offline); "
                        "ElevenLabs only listens during the selection")
    p.add_argument("--selection-timeout", type=float, default=30,
                   help="with --wake, drop a selection (and disconnect ElevenLabs) after this many seconds")
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
    # neon (both)
    p.add_argument("--flip-y", action="store_true", help="use if the box moves opposite to your gaze vertically")
    p.add_argument("--marker-size", type=int, default=120)
    # neon via Pupil Capture (USB)
    p.add_argument("--pupil-host", default="127.0.0.1", help="machine running Pupil Capture")
    p.add_argument("--pupil-port", type=int, default=50020, help="Pupil Remote port in Pupil Capture")
    p.add_argument("--surface", default="screen", help="surface name in Pupil Capture's Surface Tracker")
    p.add_argument("--min-confidence", type=float, default=0.6, help="drop gaze below this confidence")
    p.add_argument("--blink-confidence", type=float, default=0.3,
                   help="eye counts as closed below this pupil confidence")
    # neon via Companion app (Android)
    p.add_argument("--neon-ip", default=None, help="skip discovery; IP shown in the Companion app")
    p.add_argument("--neon-port", type=int, default=8080)
    p.add_argument("--blink-mode", choices=["auto", "eyelid", "events"], default="auto")
    p.add_argument("--eyelid-ratio", type=float, default=0.4, help="closed below this x open aperture")
    return p.parse_args()


class GazeSnapApp:
    def __init__(self, args):
        self.args = args
        self.app = QApplication.instance() or QApplication(sys.argv)
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
        self.manual = (args.position or ("follow" if args.source.startswith("neon") else "manual")) == "manual"
        self.handle = DragHandle()
        self.handle.moved.connect(self.move_box)
        self.resize_handle = ResizeHandle()
        self.resize_handle.moved.connect(self.resize_box)
        self.voice_control = None
        self.analyzer = None
        self.analysis_enabled = args.analysis if args.analysis is not None else (args.wake and not args.narrate)
        self.analysis_id = None
        self.analysis_speaking = False
        self.analysis_error = False
        self.mic = self.wake = None
        self.pending_start = None  # wake fired; pin the first corner on the next fresh gaze sample
        self.selection_deadline = None
        self.tray = None
        self.detector = BlinkDetector(args.long_blink_ms, args.long_blink_max_ms,
                                      args.double_window_ms, args.cooldown_ms, args.blink_gesture)
        self.narrator = None
        self.narrate = args.narrate
        if args.narrate:
            self._make_narrator()

        self.crop = CropSelector(self.screen_w, self.screen_h, max(200, args.dwell_ms))
        self.crop_active = args.selection == "crop"
        self.crop_by_voice = False  # selection started by "hey Neon"; ends after capture
        self.last_gaze = None
        self.gaze_xy = None  # latest filtered gaze/cursor, where voice pins the first corner
        self.overlay.crop = self.crop if self.crop_active else None
        self.overlay.idle = args.wake
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
            was_alive = self.voice_control.alive
            self.voice_control.stop()
            self.voice_control = None
            if was_alive:
                self.overlay.voice_status = "Voice off • type v + Enter to enable ElevenLabs"
                self.log("ElevenLabs microphone off")
                return
            # voice had already stopped (e.g. rejected key): start it again
        if not self.ensure_api_key():
            self.log("Voice off: no ElevenLabs key provided; type v + Enter to retry")
            return
        from voice_control import VoiceControl
        self.voice_control = VoiceControl(self.events)
        self.overlay.voice_status = "Voice: connecting to ElevenLabs…"
        self.voice_control.start()

    def ensure_api_key(self):
        from voice_control import get_api_key
        if get_api_key():
            return True
        key, accepted = QInputDialog.getText(
            None, "Connect Snapshotter to ElevenLabs",
            "Captures are uploaded when analysis is enabled. Microphone audio streams\n"
            "while voice commands are on.\n"
            "Enter your ElevenLabs API key (kept only for this session):",
            QLineEdit.EchoMode.Password)
        if not accepted or not key.strip():
            return False
        os.environ["ELEVENLABS_API_KEY"] = key.strip()
        return True

    # --- wake mode ("hey Neon, take a screenshot") ----------------------------------
    def wake_up(self):
        """Wake phrase heard: show the overlay, connect ElevenLabs, start a selection."""
        if self.paused or self.snapping or self.analysis_speaking:
            return
        self.overlay.idle = False
        self.pending_start = time.monotonic()
        self.selection_deadline = time.monotonic() + self.args.selection_timeout
        if self.voice_control is None or not self.voice_control.alive:
            from voice_control import VoiceControl
            # skip 0.8 s so the tail of "…take a screenshot" isn't heard as "screenshot"
            self.voice_control = VoiceControl(self.events, self.mic, skip_s=0.8,
                                              ready="listening — say “capture”, “retry” or “cancel”")
            self.voice_control.start()

    def go_idle(self):
        """Selection over: hide the overlay and disconnect ElevenLabs (the offline wake
        listener keeps running)."""
        self.overlay.idle = True
        self.overlay.update()  # clear the selection from the screen
        self.pending_start = self.selection_deadline = None
        if self.voice_control is not None:
            # stop() waits for the thread; don't block the UI on it
            threading.Thread(target=self.voice_control.stop, daemon=True).start()
            self.voice_control = None

    def finish_voice_selection(self):
        self.set_crop_active(self.args.selection == "crop" and not self.args.wake)
        if self.args.wake:
            self.go_idle()

    def make_tray(self):
        """Menu-bar icon: the only UI while GazeSnap sits in the background."""
        pix = QPixmap(36, 36)
        pix.fill(QColor(0, 0, 0, 0))
        qp = QPainter(pix)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setPen(QPen(QColor(0, 0, 0), 3))
        qp.drawEllipse(4, 10, 28, 16)  # an eye
        qp.setBrush(QColor(0, 0, 0))
        qp.drawEllipse(13, 13, 10, 10)
        qp.end()
        icon = QIcon(pix)
        icon.setIsMask(True)  # adapts to light/dark menu bar
        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip("GazeSnap: say “hey Neon, take a screenshot”")
        menu = QMenu()
        menu.addAction("Say “hey Neon, take a screenshot”").setEnabled(False)
        pause = QAction("Pause listening", menu, checkable=True)
        pause.toggled.connect(self.set_listening_paused)
        menu.addAction(pause)
        analysis = QAction("Spoken image analysis", menu, checkable=True)
        analysis.setChecked(self.analysis_enabled)
        analysis.toggled.connect(self.set_analysis_enabled)
        menu.addAction(analysis)
        menu.addAction("Stop spoken analysis", self.cancel_analysis)
        menu.addAction("Open captures folder", lambda: os.system(f"open '{self.args.out}'"))
        menu.addSeparator()
        menu.addAction("Quit GazeSnap", self.app.quit)
        self.tray.setContextMenu(menu)
        self.tray.show()
        self._tray_menu = menu  # keep a reference

    def set_listening_paused(self, paused):
        if self.wake:
            self.wake.paused = paused
        if paused:
            self.finish_voice_selection()
        self.log("wake: paused" if paused else "wake: listening")

    def _make_narrator(self):
        try:
            from narrator import Narrator
            self.narrator = Narrator(self.args.model, self.args.voice,
                                     on_text=lambda t: self.events.put(("status", "“" + t + "”")))
        except Exception as e:
            self.narrate = False
            self.log(f"narration off: {e}")

    def _make_analyzer(self):
        try:
            from image_analyzer import ElevenLabsAnalyzer, agent_id
            agent = self.args.analysis_agent or agent_id()
            if not agent:
                self.log("analysis: off — run python tools/setup_analysis.py first")
                return False
            if not self.ensure_api_key():
                self.log("analysis: off — no ElevenLabs key provided")
                return False
            self.analyzer = ElevenLabsAnalyzer(self.events, agent)
            return True
        except Exception as exc:
            self.log(f"analysis: off ({type(exc).__name__}); install requirements-voice.txt")
            return False

    def _mute_for_analysis(self, muted):
        self.analysis_speaking = muted
        for listener in (self.wake, self.voice_control):
            if listener:
                listener.set_output_muted(muted)

    def cancel_analysis(self):
        if self.analyzer:
            self.analyzer.cancel()
        self.analysis_id = None
        if self.analysis_speaking:
            self._mute_for_analysis(False)

    def set_analysis_enabled(self, enabled):
        self.analysis_enabled = enabled
        if not enabled:
            self.cancel_analysis()
        elif self.analyzer is None:
            self._make_analyzer()
        self.log(f"analysis: {'on' if enabled and self.analyzer else 'off'}")

    def analysis_event(self, ev):
        kind, generation = ev[:2]
        if generation != self.analysis_id:
            return  # a newer capture, toggle, or cancellation invalidated this result
        if kind == "analysis_started":
            self.analysis_error = False
            self.log("analysis: analyzing screenshot…")
        elif kind == "speech_started":
            self._mute_for_analysis(True)
            self.log("analysis: speaking…")
            ev[2].set()  # only start speaker output after listeners have been gated
        elif kind == "analysis_text":
            print(f"[analysis] {ev[2]}", flush=True)
        elif kind == "analysis_failed":
            self.analysis_error = True
            self.log("analysis: " + ev[2])
        elif kind == "analysis_finished":
            if self.analysis_speaking:
                self._mute_for_analysis(False)
            self.analysis_id = None
            if not self.analysis_error:
                self.log("analysis: finished")

    def log(self, msg):
        print(f"[gazesnap] {msg}", flush=True)
        if self.overlay.idle and not msg.startswith(("saved", "wake:", "analysis:", "capture failed", "voice: off")):
            return  # stay invisible between selections
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
                self.last_gaze = t
                self.gaze_xy = (fx, fy)
                if self.pending_start is not None and t >= self.pending_start:
                    self.pending_start = None
                    self.start_selection()
                if self.crop_active and not self.paused and not self.snapping and time.monotonic() - t <= 0.25:
                    self.crop.update(fx, fy, t)
            elif kind == "blink":
                _, t0, t1 = ev
                if not self.paused and not self.args.no_blink_capture and not self.crop_active \
                        and not self.args.wake:
                    start = self.detector.on_blink(t0, t1)
                    if start is not None:
                        self.snap(self.tracker.rect_before(start))
            elif kind == "status":
                self.log(ev[1])
            elif kind == "cmd":
                if not self.analysis_speaking or ev[1] not in ("start", "capture", "r", "cancel"):
                    self.command(ev[1])
            elif kind in ("analysis_started", "analysis_text", "speech_started", "analysis_failed", "analysis_finished"):
                self.analysis_event(ev)

        now = time.monotonic()
        if self.pending_start is not None and now - self.pending_start > 2:
            self.pending_start = None  # no fresh gaze (markers not found yet?): use the last known
            self.log("no fresh gaze yet; starting from the last known point")
            self.start_selection()
        if self.selection_deadline is not None and now > self.selection_deadline:
            self.log("selection timed out")
            self.finish_voice_selection()
        if self.crop_active and (self.last_gaze is None or now - self.last_gaze > 0.25):
            self.crop.clear_dwell()
        self.overlay.box = self.tracker.current_rect()
        self.overlay.gaze = (self.tracker.x, self.tracker.y)
        self.overlay.stable = self.tracker.stable
        self.overlay.paused = self.paused
        left, top, width, height = self.overlay.box
        show_handles = self.manual and not self.paused and not self.snapping and not self.crop_active \
            and not self.args.wake
        self.handle.move(left, top)
        self.handle.setVisible(show_handles)
        self.resize_handle.move(left + width - 26, top + height - 26)
        self.resize_handle.setVisible(show_handles)
        # while idle in wake mode, tick slowly and repaint only for toasts and the capture flash
        self.timer.setInterval(100 if self.overlay.idle else 16)
        if not self.overlay.idle or now < max(self.overlay.status_until, self.overlay.flash_until) + 0.2:
            self.overlay.update()

    def set_crop_active(self, active):
        self.crop_active = active
        self.crop_by_voice = False
        self.crop.reset()
        self.overlay.crop = self.crop if active else None

    def start_selection(self):
        """Pin the first corner where the user is looking; the rectangle then follows gaze."""
        if self.paused or self.snapping:
            return
        x, y = self.gaze_xy or (self.screen_w / 2, self.screen_h / 2)
        self.set_crop_active(True)
        self.crop_by_voice = True
        self.crop.start_at(x, y)
        self.log("selection started: look at the opposite corner, then say “capture”")

    def command(self, cmd):
        if cmd == "start" and self.args.wake:  # offline wake phrase
            self.wake_up()
        elif cmd == "start":  # "hey Neon, take a screenshot"
            self.start_selection()
        elif cmd == "capture" or (cmd == "" and self.crop_active):  # voice, or Enter in crop mode
            if self.crop_active and self.crop.has_rect():
                self.crop.state = "done"
                self.snap(self.crop.rect)
            elif self.crop_active:
                self.log("crop: look at the opposite corner first")
            else:
                self.log("say “hey Neon, take a screenshot” to start a selection")
        elif cmd == "":
            self.snap(self.tracker.current_rect())
        elif cmd == "cancel" and self.crop_by_voice:
            self.finish_voice_selection()
            self.log("selection cancelled")
        elif cmd == "c" or (cmd == "cancel" and self.crop_active):
            self.set_crop_active(not self.crop_active)
            self.log("crop mode" if self.crop_active else "box mode")
        elif cmd == "r" and self.crop_by_voice:
            self.start_selection()
        elif cmd == "r" and self.crop_active:
            self.crop.reset()
        elif cmd in ("r", "cancel"):
            pass  # crop-only voice commands; ignore in box mode
        elif cmd in ("+", "="):
            self.tracker.resize(1.2)
        elif cmd == "-":
            self.tracker.resize(1 / 1.2)
        elif cmd == "p":
            self.paused = not self.paused
            self.crop.clear_dwell()
            if self.paused:
                self.cancel_analysis()
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
        elif cmd == "a":
            self.set_analysis_enabled(not self.analysis_enabled)
        elif cmd == "s":
            self.cancel_analysis()
        elif cmd == "q":
            self.app.quit()
        else:
            print("commands: Enter=snap/save crop  start=new selection at gaze  c=crop toggle  r=retry  "
                  "+/-=resize  p=pause  "
                  "m=manual/follow  v=voice  n=narration  a=analysis  s=stop speech  q=quit", flush=True)

    # --- capture ----------------------------------------------------------------
    def snap(self, rect):
        if self.snapping or self.paused:
            return
        self.cancel_analysis()
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
            if self.crop_active:
                self.crop.state = "preview"
            return
        if self.crop_by_voice:
            self.finish_voice_selection()
        elif self.crop_active:
            self.crop.reset()
        if not self.args.no_sound:
            play_shutter()
        self.overlay.flash(rect)
        self.app.clipboard().setImage(QImage(path))
        self.log(f"saved {path}")
        if self.analysis_enabled and self.analyzer:
            self.analysis_id = self.analyzer.analyze_async(path)
        elif self.narrate and self.narrator:
            self.overlay.set_status("Describing…", 10)
            self.narrator.describe_async(path)

    # --- run ----------------------------------------------------------------
    def start_wake(self):
        from mic import Microphone
        from wake import WakeListener
        hide_dock_icon()
        self.make_tray()
        self.ensure_api_key()  # ask now, not in the middle of the first selection
        self.mic = Microphone()
        try:
            self.mic.start()
        except Exception as e:
            self.log(f"wake: off — microphone unavailable ({type(e).__name__})")
            return
        self.wake = WakeListener(self.events, self.mic)
        self.wake.start()

    def _read_stdin(self):
        for line in sys.stdin:
            self.events.put(("cmd", line.strip()))

    def run(self):
        signal.signal(signal.SIGINT, lambda *_: self.app.quit())
        threading.Thread(target=self._read_stdin, daemon=True).start()
        if self.analysis_enabled:
            self._make_analyzer()
        self.source.start()
        if self.args.wake:
            self.start_wake()
        elif self.args.voice_control:
            QTimer.singleShot(0, self.toggle_voice)
        self.timer.start(16)  # ~60 Hz; also lets Python handle Ctrl+C
        if not self.args.wake:
            hint = "press Enter in the terminal" if self.source.uses_cursor and self.args.source == "mouse" \
                else "long blink or double blink"
            self.log(f"GazeSnap ({self.args.source}): look at something, {hint} to snap")
        code = self.app.exec()
        self.cancel_analysis()
        if self.analyzer:
            self.analyzer.close()
        self.source.stop()
        if self.wake:
            self.wake.stop()
        if self.voice_control:
            self.voice_control.stop()
        if self.mic:
            self.mic.stop()
        self.handle.close()
        self.resize_handle.close()
        return code


def hide_dock_icon():
    """Run as a menu-bar-only app, like Siri: no Dock icon (macOS, via the Objective-C runtime)."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
        send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        set_policy = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(
            ("objc_msgSend", objc))
        ns_app = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        set_policy(ns_app, objc.sel_registerName(b"setActivationPolicy:"), 1)  # Accessory
    except Exception:
        pass  # cosmetic only


if __name__ == "__main__":
    sys.exit(GazeSnapApp(parse_args()).run())
