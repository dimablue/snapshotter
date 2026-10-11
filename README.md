# Snapshotter

Take screenshots with your eyes and your voice. Say "hey Neon, take a screenshot", look at the
area you want, say "capture". The screenshot is saved, copied, opened, and described out loud,
with an offer of something useful to do with it.

Made for the Interface hackathon (Vision track), with a Pupil Labs Neon eye tracker on an M4
MacBook Air.

## How it works

1. **Wake.** An offline speech recognizer (Vosk) listens for "hey Neon, take a screenshot".
   Nothing leaves the computer until it hears that.
2. **First corner.** The overlay appears and pins the first corner where your eyes rested while
   you said the phrase. It uses the longest gaze fixation in the last 1.5 s, not where your eyes
   happen to be when recognition finishes.
3. **Drag with your eyes.** The selection rectangle follows your gaze to the opposite corner.
4. **Capture.** Say "capture" (or "snap", "screenshot"). "retry" re-pins the first corner, and
   "cancel" drops the selection. The opposite corner is again taken from your last fixation.
5. **Result.** The PNG is saved to `~/Pictures/Snapshotter/`, copied to the clipboard, and opened
   in Preview.
6. **Analysis.** An ElevenLabs agent looks at the image, says in one or two sentences what it is,
   and offers one action: a calendar event for a poster, a memory for a photo, a note for an idea,
   questions for a lecture slide, or troubleshooting for an error. You answer by voice.

The app lives in the menu bar (eye icon), with no Dock icon.

## Architecture

```
 Neon glasses ──USB──▶ Pupil Capture (open source, runs locally)
                         ├─ eye cameras ─▶ 2D pupil detection ─▶ gaze in scene camera
                         ├─ scene camera (macOS plugin) ─▶ Surface Tracker finds 4 AprilTags
                         └─ gaze mapped onto the "screen" surface
                                      │ ZMQ (Pupil Remote, 127.0.0.1:50020)
                                      ▼
 microphone ─▶ shared mic ─┬─▶ Vosk (offline): wake phrase + capture/retry/cancel
                           └─▶ ElevenLabs Scribe Realtime: backup for commands, voice replies
                                      │
                                      ▼
                     Snapshotter (PyQt6 overlay + menu bar)
                       gaze correction ▶ fixation detection ▶ selection rectangle
                                      │ "capture"
                                      ▼
                 mss screenshot ▶ clipboard + Preview ▶ ElevenLabs agent (spoken analysis)
```

| File | What it does |
| --- | --- |
| `snapshotter.py` | the app: event loop, selection state, voice commands, menu bar, gaze check |
| `sources/pupil_capture.py` | reads surface gaze and pupil confidence from Pupil Capture over ZMQ |
| `overlay.py` | transparent, click-through overlay: AprilTags, rectangle, dots, toasts |
| `core.py` | gaze filter, fixations, crop selection, 9-dot check and affine correction fit |
| `wake.py` | offline Vosk listener for the wake phrase and the selection commands |
| `voice_control.py` | ElevenLabs Scribe Realtime streaming and command parsing |
| `mic.py` | one microphone stream shared by Vosk and ElevenLabs |
| `image_analyzer.py`, `action_flow.py` | ElevenLabs agent session, structured results, local drafts |
| `pupil_plugins/neon_scene_macos.py` | Pupil Capture plugin: Neon scene camera through macOS |
| `tools/` | setup, calibration, recording, replay and demo tools |

## Pupil Labs Neon

The Neon is usually used with its Companion app on an Android phone. We have no Android phone, so
the glasses plug straight into the Mac over USB and **Pupil Capture** (Pupil Labs' open-source
desktop software) does the eye tracking. Getting that working on Apple Silicon took most of the
effort:

- **Scene camera.** Pupil Capture reads the scene camera over raw USB (libuvc). Under Rosetta that
  produced broken frames ("Premature end of JPEG file", gray blocks). Our plugin
  `neon_scene_macos.py` reads the camera through macOS's own camera stack (AVFoundation via OpenCV)
  instead, and measured 0 bad frames. It picks the camera by name and patches Pupil's Neon manager
  so the eye cameras still go through Pupil.
- **Screen mapping.** The overlay draws four AprilTags in the screen corners. Pupil's Surface
  Tracker finds them in the scene image and maps gaze onto the screen. Its default detection
  missed the tags (they are only ~27 px wide in the wide-angle image), so `tools/neon_setup.py`
  turns on full-resolution detection and writes the surface definition from our exact marker
  geometry. Markers are 180 px.
- **Calibration.** Pupil's screen-marker calibration with the 2D gaze mapper, which suits a screen
  at a fixed distance. The 3D pupil detector is switched off to free CPU for the eye cameras.
- **Correction.** A guided 9-dot check (`snapshotter.py --check`) measures where gaze lands on known
  dots and fits an affine correction. It refuses implausible fits (too much skew, scale or
  leftover error). Our best run: 0.45° Pupil accuracy, then 56 px average error → 19 px after
  correction.
- **Recovery.** The Neon's eye cameras sometimes stall on macOS USB. Snapshotter restarts them
  automatically after 5 s without pupil data.

## Voice

- **Vosk, offline.** Listening for the wake phrase happens offline with Vosk and a small English
  model, restricted to a short grammar. It's free, private (no audio leaves the Mac while idle),
  and fast: the phrase usually fires before you finish saying it. Only "neon" plus a capture
  phrase is required, because recognizers often hear "hey" as "a".
- **Fast capture.** While a selection is open, Vosk also listens for "capture", "snap", "retry"
  and "cancel". Locally these fire about 0.35 s into the word, so capture feels immediate and
  works before the cloud connection is even up.
- **ElevenLabs Scribe Realtime.** It connects only after the wake phrase, as a fuzzier backup for
  commands spoken in a full sentence ("ok capture this one") and for free-form replies to the
  analysis. When both recognizers hear the same command, only the first counts. Commands fire on
  partial transcripts, not after the sentence ends. ElevenLabs disconnects after the capture or
  after 30 s.
- **Shared microphone.** macOS won't give one process two input streams on the same device, so Vosk
  and ElevenLabs subscribe to one shared stream. Speaker output is gated out so the app doesn't
  hear itself.

## ElevenLabs analysis and actions

Each capture is sent to an ElevenLabs Conversational AI agent (set up once with
`tools/setup_analysis.py`, voice `eleven_flash_v2`). The agent has to return a structured result
through a tool before it speaks, which the app validates: a short summary plus one offer.

| Screenshot | Offer |
| --- | --- |
| event, poster | draft a calendar event (asks for any missing date, time or timezone) |
| photo, scenery | save as a memory (always asks for your own description first) |
| idea, content | add to a brain dump note |
| lecture, information | answer questions about it |
| error message | help troubleshooting |

Confirmed drafts are saved as local files (`.ics` for calendar, Markdown for notes and memories)
under `~/Pictures/Snapshotter/Items/`. Nothing is sent or published. Image text is treated as
data, never as instructions.

## Decisions

- **Offline wake word with Vosk** instead of streaming the microphone to the cloud all the time:
  private, free and fast. ElevenLabs is only used once you've asked for a screenshot.
- **Vosk also handles "capture"**, so capturing doesn't wait for a network round trip.
- **Voice to capture, not blinks.** In wake mode only a spoken command captures. Long or double
  blinks still capture in the other selection modes.
- **Corners from fixations.** Speech recognition always lags the word, so corners come from where
  the eyes rested, not where they are when the command arrives.
- **Open the screenshot in Preview** right after capture, so you see what you got (`--no-open` turns
  it off).
- **Full-strength markers.** We tested fainter markers: Pupil still found them at 40% opacity but
  not at 25%, and a gaze check with faint markers came out worse. So the markers stay solid, but they
  only appear while a selection is open, and they're hidden for the instant the screenshot is
  taken, so they never end up in your images.
- **Pupil Capture over USB instead of the Companion app**, because the Companion app is
  Android-only.
- **2D calibration, 3D detector off**, because the screen is at a fixed distance and the CPU
  (Rosetta) can't keep up with both.
- **Keep the best calibration.** `tools/neon_setup.py save NAME` and `restore NAME` store and reload
  Pupil's calibration together with the gaze correction, so a good calibration isn't lost.
- **Drafts stay local.** Actions produce files you can review; nothing acts on your behalf.

## Working without the hardware

- `tools/record_gaze.py`: records a session against known targets: 25 dots, a moving dot, dots
  while moving the head, and a drift check. It also starts a full Pupil Capture recording.
- `tools/analyze_gaze.py`: per-dot error in points and degrees, data health, the gaze delay, and
  whether a different correction model would help.
- `tools/replay_gaze.py`: plays a recorded session into Snapshotter as if the Neon were plugged in.
- `tools/demo_neon.py`: scripted fake gaze and voice commands for a demo with no headset.
- `--source mouse` and `--source webcam` stand in for the eye tracker during development.

## Running

```
pip install -r requirements.txt -r requirements-neon.txt -r requirements-voice.txt
python tools/neon_setup.py install
python tools/setup_analysis.py
```

Then, with Pupil Capture running (`sudo ../.venv/bin/python main.py capture` from its
`pupil_src` folder):

```
python tools/neon_setup.py
python tools/neon_setup.py calibrate
python snapshotter.py --source neon --check
python snapshotter.py --source neon --wake
```

The ElevenLabs key comes from `$ELEVENLABS_API_KEY` or the macOS Keychain
(`security add-generic-password -a "$USER" -s snapshotter-elevenlabs -w`). The terminal needs
Screen Recording and Microphone permission.

## Limitations

- Calibration drifts when the glasses shift, and the top-left of the screen is the least accurate.
- The Neon's USB connection to the Mac is unstable, and eye processing is CPU-limited under Rosetta.
- The corner markers must be visible on screen while you select.
- Actions only produce local drafts; nothing is sent to a real calendar, notes app or contact yet.

## Credits

- Pupil Labs: Neon, Pupil Capture, `real-time-screen-gaze`, `pupil-apriltags`
- ElevenLabs: Scribe Realtime speech-to-text and Conversational AI agents
- Vosk and its `vosk-model-small-en-us` model
- PyQt6, mss, OpenCV, MediaPipe, pyzmq, msgpack, sounddevice, numpy
