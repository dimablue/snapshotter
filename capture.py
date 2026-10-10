"""Grab a screen region, save it as PNG, put it on the clipboard."""
import os
import subprocess
import time

import mss
import mss.tools


def grab_region(rect, out_dir):
    """rect = (left, top, w, h) in logical points (mss on macOS uses the same). Returns the PNG path."""
    left, top, w, h = rect
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime("gazesnap-%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.png")
    with mss.MSS() if hasattr(mss, "MSS") else mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": w, "height": h})
        mss.tools.to_png(shot.rgb, shot.size, output=path)
    return path


def play_shutter():
    sound = "/System/Library/Sounds/Tink.aiff"
    if os.path.exists(sound):
        subprocess.Popen(["afplay", sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
