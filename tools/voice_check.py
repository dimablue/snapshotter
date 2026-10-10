#!/usr/bin/env python3
"""ElevenLabs check: reports the shape of $ELEVENLABS_API_KEY (never the key itself) and
whether Scribe Realtime accepts it, using the same connection as --voice-control.

    python tools/voice_check.py
"""
import json
import os
import string

from websockets.sync.client import connect

URL = ("wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v2_realtime"
       "&audio_format=pcm_16000&commit_strategy=vad&language_code=en")


def main():
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        print("ELEVENLABS_API_KEY is not set (or not exported) in this terminal.")
        return
    print(f"length {len(key)}, starts with sk_: {key.startswith('sk_')}")
    problems = []
    if key != key.strip():
        problems.append("leading/trailing spaces or line breaks")
    if any(c in key for c in "\"'"):
        problems.append("quote characters")
    if any(c not in string.ascii_letters + string.digits + "_-" for c in key.strip().strip("\"'")):
        problems.append("characters that don't belong in a key")
    if key.startswith("sk_") and key.count("sk_") > 1:
        problems.append("'sk_' appears more than once (pasted twice?)")
    print("looks off: " + "; ".join(problems) if problems else "shape looks fine")

    try:
        with connect(URL, additional_headers={"xi-api-key": key}, open_timeout=10) as ws:
            msg = json.loads(ws.recv(timeout=10))
    except Exception as e:
        print(f"could not reach ElevenLabs: {type(e).__name__}")
        return
    kind = msg.get("message_type")
    if kind == "session_started":
        print("ElevenLabs accepted the key: voice control should work.")
    else:
        print(f"ElevenLabs replied {kind}: {msg.get('error', '')}")
        if kind == "auth_error":
            print("-> the key is wrong, revoked, or lacks Speech to Text access.")


if __name__ == "__main__":
    main()
