"""Pupil Capture video source: the Neon scene camera through macOS's own camera stack.

Pupil Capture normally reads the Neon scene camera over raw USB (libuvc). On macOS that
loses parts of frames ("Premature end of JPEG file", gray blocks). This source reads the
same camera the way FaceTime does (AVFoundation via OpenCV), with the stock Neon scene
camera intrinsics, while the eye cameras keep using Pupil's Neon backend.

Install: copy into Pupil Capture's capture_settings/plugins/ (tools/neon_setup.py does this),
then start it with tools/neon_setup.py, or in Pupil Remote:
    notify.start_plugin {"subject": "start_plugin", "name": "Neon_Scene_MacOS_Source", "args": {}}
"""
import logging
import threading
import time

from camera_models import Camera_Model
from pyglui import ui
from video_capture.base_backend import Base_Source
from video_capture.hmd_streaming import BGRFrame

logger = logging.getLogger(__name__)
INTRINSICS_NAME = "Neon Scene Camera v1"  # Pupil ships default intrinsics under this name


def find_camera_index(name_part):
    """OpenCV AVFoundation index of the first camera whose name contains name_part."""
    import AVFoundation as AVF

    devices = AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeVideo)
    # OpenCV numbers cameras in uniqueID order, not in the order macOS lists them
    for i, device in enumerate(sorted(devices, key=lambda d: str(d.uniqueID()))):
        if name_part.lower() in str(device.localizedName()).lower():
            return i, str(device.localizedName())
    return None, None


class Neon_Scene_MacOS_Source(Base_Source):
    def __init__(self, g_pool, camera_name="Neon", frame_size=(1280, 720), frame_rate=30,
                 latency=0.03, *args, **kwargs):
        super().__init__(g_pool, *args, **kwargs)
        self.camera_name = camera_name
        self._frame_size = tuple(frame_size)
        self._frame_rate = frame_rate
        self.latency = latency  # seconds between exposure and the frame reaching us
        self._intrinsics = Camera_Model.from_file(g_pool.user_dir, INTRINSICS_NAME, self._frame_size)
        self._latest = None
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._online = False
        self._index = 0
        self.status = "starting…"
        self._thread = threading.Thread(target=self._capture, name="neon-scene-macos", daemon=True)
        self._thread.start()

    # --- capture thread ---------------------------------------------------------------
    def _capture(self):
        # Reopen whenever the camera goes away (e.g. the Neon is replugged).
        while not self._stop.is_set():
            if self._stream_once() == "fatal":
                return

    def _find_camera(self):
        """Wait until macOS lists the camera; returns (index, name) or (None, None) if stopping."""
        warned, started = False, time.monotonic()
        while not self._stop.is_set():
            index, full_name = find_camera_index(self.camera_name)
            if index is not None:
                return index, full_name
            self.status = f"waiting for a macOS camera named like {self.camera_name!r}…"
            if not warned and time.monotonic() - started > 10:
                warned = True
                logger.error(f"no macOS camera named like {self.camera_name!r}; unplug and replug "
                             "the Neon (still waiting)")
            time.sleep(1)
        return None, None

    def _stream_once(self):
        import cv2

        try:
            index, full_name = self._find_camera()
        except ImportError:
            self.status = "needs pyobjc-framework-AVFoundation in Pupil's environment"
            logger.error(self.status)
            return "fatal"
        if index is None:
            return "stopped"
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._frame_size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._frame_size[1])
        cap.set(cv2.CAP_PROP_FPS, self._frame_rate)
        if not cap.isOpened():
            self.status = f"could not open {full_name} (Camera permission for Terminal?)"
            logger.error(self.status)
            cap.release()
            time.sleep(2)
            return "retry"
        self.status = f"streaming {full_name}"
        logger.info(f"Neon scene camera via macOS: {full_name} (index {index})")
        self._online = True
        last_frame = time.monotonic()
        try:
            while not self._stop.is_set():
                ok, img = cap.read()
                ts = self.g_pool.get_timestamp() - self.latency
                if not ok:
                    if time.monotonic() - last_frame > 2:
                        logger.warning("Neon scene camera stopped sending frames; reopening it")
                        return "retry"
                    time.sleep(0.01)
                    continue
                last_frame = time.monotonic()
                with self._lock:
                    self._latest = (img, ts)
                    self._lock.notify()
        finally:
            self._online = False
            cap.release()
        return "stopped"

    # --- Base_Source --------------------------------------------------------------
    def recent_events(self, events):
        # Wait for the next frame, like Pupil's own camera source: plugins such as the Surface
        # Tracker only map the gaze collected in loop iterations that carry a frame, and a
        # spinning loop would also starve the eye processes of CPU.
        with self._lock:
            if self._latest is None and self._online:
                self._lock.wait(timeout=2 / self._frame_rate)
            item, self._latest = self._latest, None
        if item is None:
            return
        img, ts = item
        h, w = img.shape[:2]
        if (w, h) != self._frame_size:  # camera picked another size: match the intrinsics
            self._frame_size = (w, h)
            self._intrinsics = Camera_Model.from_file(self.g_pool.user_dir, INTRINSICS_NAME, (w, h))
        self._index += 1
        frame = BGRFrame(img.tobytes(), ts, self._index, w, h)
        events["frame"] = frame
        self._recent_frame = frame

    @property
    def name(self):
        return "Neon Scene (macOS camera)"

    @property
    def frame_size(self):
        return self._frame_size

    @property
    def frame_rate(self):
        return self._frame_rate

    @property
    def jpeg_support(self):
        return False

    @property
    def online(self):
        return self._online

    def get_init_dict(self):
        d = super().get_init_dict()
        d.update(camera_name=self.camera_name, frame_size=self._frame_size,
                 frame_rate=self._frame_rate, latency=self.latency)
        return d

    def ui_elements(self):
        return [ui.Info_Text("Neon scene camera read through macOS (AVFoundation) instead of "
                             "raw USB, so frames arrive complete."),
                ui.Text_Input("status", self, label="Status", setter=lambda _: None)]

    def cleanup(self):
        self._stop.set()
        self._thread.join(timeout=2)
        super().cleanup()


def _scene_via_macos_auto_activate(self, scene=True, eyes=True):
    """Neon activation without Pupil's raw-USB scene source: once that has opened the scene
    camera, macOS loses it until the Neon is replugged. Eyes still use Pupil's Neon backend."""
    logger.debug("Auto activating Neon source (scene camera via macOS).")
    if scene:
        self.notify_all({"subject": "start_plugin", "name": "Neon_Scene_MacOS_Source", "args": {}})
    if eyes:
        for eye in range(2):
            self.notify_all({"subject": "start_eye_plugin", "target": f"eye{eye}",
                             "name": "Neon_Eye_Cam_Source"})


def _patch(uvc_init):
    def init(self, g_pool, frame_size, frame_rate, name=None, *args, **kwargs):
        """Pupil restores last session's video source at startup; if that was the raw-USB Neon
        scene camera, open nothing and switch to the macOS source instead."""
        if getattr(g_pool, "process", "") == "world" and "Neon Scene Camera" in str(name or ""):
            kwargs.update(uid=None, preferred_names=())
            uvc_init(self, g_pool, frame_size, frame_rate, None, *args, **kwargs)
            logger.info("Neon scene camera: using the macOS camera instead of raw USB")
            self.notify_all({"subject": "start_plugin", "name": "Neon_Scene_MacOS_Source", "args": {}})
            return
        uvc_init(self, g_pool, frame_size, frame_rate, name, *args, **kwargs)
    return init


# Pupil imports runtime plugins before it restores or activates video sources, so these take
# effect in time.
try:
    from video_capture.neon_backend.plugin import Neon_Manager
    from video_capture.uvc_backend import UVC_Source

    Neon_Manager.auto_activate_source = _scene_via_macos_auto_activate
    UVC_Source.__init__ = _patch(UVC_Source.__init__)
except ImportError:
    pass
