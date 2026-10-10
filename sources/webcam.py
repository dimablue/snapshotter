"""Webcam source: cursor = gaze, real eye blinks from the laptop camera (MediaPipe Face Mesh).

Blinks use the eye aspect ratio (EAR) against a running open-eye baseline, so it adapts
to different faces and lighting without a fixed threshold.
"""
import time

import cv2
import numpy as np

from .base import GazeSource

# Face Mesh landmark indices: p1, p2, p3, p4, p5, p6 around each eye
LEFT_EYE = [33, 160, 158, 133, 153, 144]
RIGHT_EYE = [362, 385, 387, 263, 373, 380]


def eye_aspect_ratio(pts):
    p1, p2, p3, p4, p5, p6 = pts
    return (np.linalg.norm(p2 - p6) + np.linalg.norm(p3 - p5)) / (2 * np.linalg.norm(p1 - p4) + 1e-6)


class WebcamSource(GazeSource):
    name = "webcam"
    uses_cursor = True

    def run(self):
        import mediapipe as mp

        cap = cv2.VideoCapture(self.args.camera)
        if not cap.isOpened():
            self.emit_status("webcam: could not open camera (check Camera permission for your terminal)")
            return
        mesh = mp.solutions.face_mesh.FaceMesh(max_num_faces=1, refine_landmarks=True,
                                               min_detection_confidence=0.5, min_tracking_confidence=0.5)
        baseline = None
        closed_since = None
        ratio = self.args.ear_ratio
        self.emit_status("webcam: blink tracking on")
        last_face = time.monotonic()
        while not self.stopped:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            now = time.monotonic()
            res = mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if not res.multi_face_landmarks:
                if now - last_face > 2:
                    self.emit_status("webcam: no face found")
                    last_face = now
                continue
            last_face = now
            lm = res.multi_face_landmarks[0].landmark
            h, w = frame.shape[:2]
            pts = lambda idx: np.array([[lm[i].x * w, lm[i].y * h] for i in idx])
            ear = (eye_aspect_ratio(pts(LEFT_EYE)) + eye_aspect_ratio(pts(RIGHT_EYE))) / 2

            if baseline is None:
                baseline = ear
            is_closed = ear < baseline * ratio
            if not is_closed:
                baseline = 0.95 * baseline + 0.05 * ear  # adapt only while eyes are open

            if is_closed and closed_since is None:
                closed_since = now
            elif not is_closed and closed_since is not None:
                if now - closed_since >= 0.05:  # ignore single-frame flicker
                    self.emit_blink(closed_since, now)
                closed_since = None
        cap.release()
