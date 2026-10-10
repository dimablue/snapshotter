"""Registry of gaze sources. To add hardware: subclass GazeSource, then add it here."""
from .base import GazeSource


def get_source_class(name):
    if name == "mouse":
        from .mouse import MouseSource
        return MouseSource
    if name == "webcam":
        from .webcam import WebcamSource
        return WebcamSource
    if name == "neon":
        from .neon import NeonSource
        return NeonSource
    raise ValueError(f"unknown source {name!r}")


SOURCE_NAMES = ["mouse", "webcam", "neon"]
