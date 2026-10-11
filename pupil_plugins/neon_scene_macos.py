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
    for i, device in enumerate(devices):
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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._online = False
        self._index = 0
        self.status = "starting…"
        self._thread = threading.Thread(target=self._capture, name="neon-scene-macos", daemon=True)
        self._thread.start()

    # --- capture thread ---------------------------------------------------------------
    def _capture(self):
        import cv2

        # When this replaces Pupil's raw-USB source, macOS may need a moment to see the camera.
        index = full_name = None
        deadline = time.monotonic() + 20
        while index is None and not self._stop.is_set():
            try:
                index, full_name = find_camera_index(self.camera_name)
            except ImportError:
                self.status = "needs pyobjc-framework-AVFoundation in Pupil's environment"
                logger.error(self.status)
                return
            if index is None:
                if time.monotonic() > deadline:
                    self.status = f"no macOS camera named like {self.camera_name!r}"
                    logger.error(self.status + " (is the Neon plugged in?)")
                    return
                self.status = "waiting for macOS to list the scene camera…"
                time.sleep(1)
        if index is None:
            return
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._frame_size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._frame_size[1])
        cap.set(cv2.CAP_PROP_FPS, self._frame_rate)
        if not cap.isOpened():
            self.status = f"could not open {full_name} (Camera permission for Terminal?)"
            logger.error(self.status)
            return
        self.status = f"streaming {full_name}"
        logger.info(f"Neon scene camera via macOS: {full_name} (index {index})")
        self._online = True
        try:
            while not self._stop.is_set():
                ok, img = cap.read()
                ts = self.g_pool.get_timestamp() - self.latency
                if not ok:
                    time.sleep(0.01)
                    continue
                with self._lock:
                    self._latest = (img, ts)
        finally:
            self._online = False
            cap.release()

    # --- Base_Source --------------------------------------------------------------
    def recent_events(self, events):
        with self._lock:
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
