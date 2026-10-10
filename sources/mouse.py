"""Mouse source: the cursor stands in for gaze; press Enter in the terminal to "blink".

The Enter-to-blink handling lives in gazesnap.py (it works with every source as a
manual trigger), so this source has nothing to run.
"""
from .base import GazeSource


class MouseSource(GazeSource):
    name = "mouse"
    uses_cursor = True

    def run(self):
        self._stop.wait()
