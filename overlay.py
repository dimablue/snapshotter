"""Transparent, click-through, always-on-top window that draws the gaze box and markers.

All state is in global screen coordinates (logical points). Painting converts to the
window's local coordinates, because macOS may push the window below the menu bar.
"""
import time

import numpy as np
from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt6.QtWidgets import QWidget

ACCENT = QColor(45, 127, 249)


class Overlay(QWidget):
    def __init__(self, screen_geometry, show_gaze=True):
        super().__init__()
        self.setWindowTitle("GazeSnap")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(screen_geometry)

        self.crop = None
        self.show_gaze = show_gaze
        self.box = None          # (left, top, w, h) global
        self.gaze = None         # (x, y) global
        self.stable = False
        self.paused = False
        self.hidden_for_capture = False
        self.flash_rect = None
        self.flash_until = 0.0
        self.voice_status = "Voice off • type v + Enter to enable ElevenLabs"
        self.status = ""
        self.status_until = 0.0
        self.markers = []        # [(QImage, QRect global, pad)]

    # --- markers ------------------------------------------------------------
    def place_markers(self, size, available):
        """Draw AprilTags 0-3 in the screen corners. Returns verts for GazeMapper.

        `available` is the screen area minus menu bar and Dock (which would hide the tags).
        """
        from pupil_labs.real_time_screen_gaze import marker_generator

        g = available.intersected(self.geometry())  # actual window may start below the menu bar
        pad = max(size // 4, 12)  # white quiet zone the detector needs around each tag
        inset = pad + 8
        corners = {
            0: (g.left() + inset, g.top() + inset),
            1: (g.right() - inset - size, g.top() + inset),
            2: (g.right() - inset - size, g.bottom() - inset - size),
            3: (g.left() + inset, g.bottom() - inset - size),
        }
        verts = {}
        self.markers = []
        for marker_id, (x, y) in corners.items():
            pixels = np.ascontiguousarray(marker_generator.generate_marker(marker_id))
            h, w = pixels.shape
            img = QImage(pixels.data, w, h, w, QImage.Format.Format_Grayscale8).copy()
            self.markers.append((img, QRect(x, y, size, size), pad))
            # corner order: top-left, top-right, bottom-right, bottom-left (screen px, y down)
            verts[marker_id] = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
        self.update()
        return verts

    # --- state from the app -------------------------------------------------
    def set_status(self, text, seconds=6.0):
        self.status = text
        self.status_until = time.monotonic() + seconds

    def flash(self, rect):
        self.flash_rect = rect
        self.flash_until = time.monotonic() + 0.35

    # --- painting -------------------------------------------------------------
    def _local_rect(self, rect):
        left, top, w, h = rect
        p = self.mapFromGlobal(QPoint(int(left), int(top)))
        return QRectF(p.x(), p.y(), w, h)

    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        qp.fillRect(self.rect(), Qt.GlobalColor.transparent)
        qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self.hidden_for_capture:
            qp.end()
            return
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = time.monotonic()

        for img, rect, pad in self.markers:
            r = self._local_rect((rect.x(), rect.y(), rect.width(), rect.height()))
            qp.fillRect(r.adjusted(-pad, -pad, pad, pad), Qt.GlobalColor.white)
            qp.drawImage(r, img)  # nearest-neighbour by default: crisp tag edges

        if self.box and not self.paused and self.crop is None:
            r = self._local_rect(self.box)
            if self.stable:
                pen = QPen(ACCENT, 3)
                qp.setBrush(QColor(45, 127, 249, 22))
            else:
                pen = QPen(QColor(45, 127, 249, 170), 2, Qt.PenStyle.DashLine)
                qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.setPen(pen)
            qp.drawRoundedRect(r, 12, 12)

        if self.crop is not None and not self.paused:
            crop = self.crop
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.setPen(QPen(ACCENT, 3))
            if crop.rect:
                qp.drawRect(self._local_rect(crop.rect))
            if crop.anchor:
                p = self.mapFromGlobal(QPoint(int(crop.anchor[0]), int(crop.anchor[1])))
                qp.drawEllipse(p, 9, 9)
            if crop.point and crop.state in ("first", "second"):
                p = self.mapFromGlobal(QPoint(int(crop.point[0]), int(crop.point[1])))
                qp.setPen(QPen(QColor(255, 255, 255, 180), 3))
                qp.drawEllipse(p, 22, 22)
                qp.setPen(QPen(ACCENT, 5))
                qp.drawArc(QRectF(p.x()-22, p.y()-22, 44, 44), 90*16, -int(crop.progress*360*16))
            qp.setFont(QFont("Helvetica Neue", 16))
            instructions = {"first": "Look at the first corner and hold steady",
                            "second": "Look at the opposite corner, then say “capture” · “retry” · “cancel”",
                            "preview": "Say “capture” to save · “retry” · “cancel”",
                            "done": "Saving…"}
            qp.setPen(Qt.GlobalColor.white)
            qp.drawText(QRectF(20, 50, self.width()-40, 40), Qt.AlignmentFlag.AlignCenter,
                        instructions[crop.state])

        if self.show_gaze and self.gaze and not self.paused:
            p = self.mapFromGlobal(QPoint(int(self.gaze[0]), int(self.gaze[1])))
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(QColor(255, 80, 80, 200))
            qp.drawEllipse(p, 5, 5)

        if self.flash_rect and now < self.flash_until:
            alpha = int(220 * (self.flash_until - now) / 0.35)
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(QColor(255, 255, 255, alpha))
            qp.drawRoundedRect(self._local_rect(self.flash_rect), 12, 12)

        text = "Paused" if self.paused else (self.status if now < self.status_until else self.voice_status)
        if text:
            qp.setFont(QFont("Helvetica Neue", 15))
            fm = qp.fontMetrics()
            w = min(fm.horizontalAdvance(text) + 32, self.width() - 40)
            pill = QRectF((self.width() - w) / 2, self.height() - 90, w, 40)
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(QColor(20, 20, 24, 210))
            qp.drawRoundedRect(pill, 20, 20)
            qp.setPen(QColor(255, 255, 255))
            elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, int(w - 32))
            qp.drawText(pill, Qt.AlignmentFlag.AlignCenter, elided)
        qp.end()


class DragHandle(QWidget):
    """Small input-enabled window; the full-screen overlay stays click-through."""
    moved = pyqtSignal(float, float)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setFixedSize(150, 26)
        self.offset = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), ACCENT)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Drag capture box")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.offset = event.globalPosition() - self.pos().toPointF()

    def mouseMoveEvent(self, event):
        if self.offset is not None:
            pos = event.globalPosition() - self.offset
            self.moved.emit(pos.x(), pos.y())

    def mouseReleaseEvent(self, event):
        self.offset = None


class ResizeHandle(DragHandle):
    """Bottom-right grip; moved emits the desired bottom-right screen point."""
    def __init__(self):
        super().__init__()
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("Drag to resize the capture box")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), ACCENT)
        painter.setPen(QPen(Qt.GlobalColor.white, 2))
        for inset in (6, 12, 18):
            painter.drawLine(inset, 22, 22, inset)

    def mouseMoveEvent(self, event):
        if self.offset is not None:
            pos = event.globalPosition() - self.offset
            self.moved.emit(pos.x() + self.width(), pos.y() + self.height())
