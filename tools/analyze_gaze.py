#!/usr/bin/env python3
"""Analyze a session from tools/record_gaze.py: data health, accuracy per phase, and whether a
better correction would help. Needs no hardware.

    python tools/analyze_gaze.py recordings/<date_time>
    python tools/analyze_gaze.py recordings/<date_time> --plot     # also saves analysis.png
    python tools/analyze_gaze.py recordings/<date_time> --distance-cm 55

Errors are in screen points (what GazeSnap works in) and degrees of visual angle (what Pupil
reports), using the screen's physical size and --distance-cm.
"""
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import msgpack
import numpy as np

MIN_CONFIDENCE = 0.6  # GazeSnap's --min-confidence default


# --- loading -------------------------------------------------------------------
def load(session_dir):
    d = Path(session_dir)
    meta = json.loads((d / "meta.json").read_text())
    events = [json.loads(line) for line in (d / "events.jsonl").read_text().splitlines() if line.strip()]
    with open(d / "stream.msgpack", "rb") as f:
        messages = list(msgpack.Unpacker(f, raw=False))  # [monotonic, topic, payload bytes]
    return meta, events, messages


def decode(messages, prefix):
    for t, topic, payload in messages:
        if topic.startswith(prefix):
            yield t, topic, msgpack.unpackb(payload, raw=False)


def screen_gaze(meta, messages):
    """Surface gaze in screen points, as GazeSnap maps it: array of (t, x, y, confidence),
    t in monotonic seconds from Pupil's timestamp."""
    left, top, right, bottom = meta["span"]
    rows = []
    for _, _, msg in decode(messages, f"surfaces.{meta.get('surface', 'screen')}"):
        for g in msg.get("gaze_on_surfaces", []):
            nx, ny = g["norm_pos"]
            rows.append((g["timestamp"] - meta["clock_offset"], left + nx * (right - left),
                         top + (1 - ny) * (bottom - top), g["confidence"]))
    rows.sort()
    return np.array(rows, float).reshape(-1, 4)


def pupil_samples(meta, messages):
    """{eye id: array of (t, confidence, diameter)}"""
    out = {}
    for _, topic, msg in decode(messages, "pupil."):
        if topic.endswith(".2d"):
            out.setdefault(msg["id"], []).append(
                (msg["timestamp"] - meta["clock_offset"], msg["confidence"], msg.get("diameter", float("nan"))))
    return {k: np.array(sorted(v), float) for k, v in out.items()}


def dot_windows(meta, events):
    """[(phase, i, target, t_from, t_to)] for every dot in the dot phases."""
    phases = {p["name"]: p for p in meta["phases"]}
    out = []
    for ev in events:
        if "i" in ev and ev["phase"] in phases:
            p = phases[ev["phase"]]
            t0 = ev["t"] + p["settle"]
            out.append((ev["phase"], ev["i"], tuple(ev["target"]), t0, t0 + p["collect"]))
    return out


# --- measures --------------------------------------------------------------------
def px_per_degree(meta, distance_cm):
    size = meta.get("physical_size_mm")
    if not size or not size[0]:
        return None
    mm_per_pt = size[0] / meta["screen"][2]
    return 10 * distance_cm * math.tan(math.radians(1)) / mm_per_pt


def apply_affine(affine, xy):
    a = np.asarray(affine, float)
    return xy @ a[:, :2].T + a[:, 2]


def fit(kind, measured, targets):
    """Least-squares correction measured -> targets. Returns a function on (N, 2) arrays."""
    def features(xy):
        x, y = xy[:, 0] / 1000, xy[:, 1] / 1000  # scaled for conditioning
        cols = [np.ones_like(x), x, y]
        if kind == "quadratic":
            cols += [x * x, x * y, y * y]
        return np.column_stack(cols)
    if kind == "offset":
        shift = (targets - measured).mean(axis=0)
        return lambda xy: xy + shift
    coef, *_ = np.linalg.lstsq(features(measured), targets, rcond=None)
    return lambda xy: features(xy) @ coef


def per_dot(gaze, windows):
    """[(phase, target, median gaze or None, sample count, precision)] with precision the RMS
    distance of samples from their median (the noise GazeSnap has to smooth)."""
    ok = gaze[gaze[:, 3] >= MIN_CONFIDENCE] if len(gaze) else gaze
    out = []
    for phase, _, target, t0, t1 in windows:
        s = ok[(ok[:, 0] >= t0) & (ok[:, 0] < t1)][:, 1:3] if len(ok) else np.empty((0, 2))
        if len(s) < 5:
            out.append((phase, target, None, len(s), None))
            continue
        med = np.median(s, axis=0)
        out.append((phase, target, med, len(s), float(np.sqrt(np.mean(np.sum((s - med) ** 2, axis=1))))))
    return out


def errors(measured, targets):
    return np.hypot(*(measured - targets).T)


def gaps(times, threshold=0.1):
    d = np.diff(times)
    return d[d > threshold]


# --- report ------------------------------------------------------------------------
def report(meta, events, messages, distance_cm=50.0, out=print):
    ppd = px_per_degree(meta, distance_cm)

    def fmt(px):
        return f"{px:5.0f} pt" + (f" ({px / ppd:4.1f}°)" if ppd else "")

    gaze = screen_gaze(meta, messages)
    pupils = pupil_samples(meta, messages)
    t_start = min(m[0] for m in messages) if messages else 0
    t_end = max(m[0] for m in messages) if messages else 0
    dur = max(t_end - t_start, 1e-6)
    out(f"session: {dur:.0f} s, Pupil {meta.get('pupil_version')}, calibration: {meta.get('calibration')}"
        + (", STOPPED EARLY" if meta.get("aborted") else ""))
    if ppd:
        out(f"1° ≈ {ppd:.0f} pt at {distance_cm:.0f} cm")

    out("\n== data health ==")
    for eye, a in sorted(pupils.items()):
        g = gaps(a[:, 0])
        out(f"eye{eye}: {len(a) / dur:5.0f} Hz, median confidence {np.median(a[:, 1]):.2f}, "
            f"{np.mean(a[:, 1] < MIN_CONFIDENCE):4.0%} below {MIN_CONFIDENCE}, "
            f"{len(g)} gaps > 0.1 s" + (f" (longest {g.max():.1f} s)" if len(g) else ""))
    if not pupils:
        out("eyes: NO pupil data")
    if len(gaze):
        good = gaze[gaze[:, 3] >= MIN_CONFIDENCE]
        g = gaps(good[:, 0]) if len(good) else np.array([])
        out(f"screen gaze: {len(gaze) / dur:5.0f} Hz, {len(good) / max(len(gaze), 1):4.0%} confident, "
            f"{len(g)} gaps > 0.1 s" + (f" (longest {g.max():.1f} s)" if len(g) else ""))
    else:
        out("screen gaze: NONE (surface not detected, or not calibrated)")
    logs = Counter()
    for _, topic, msg in decode(messages, "logging."):
        if topic.startswith(("logging.error", "logging.warning")):
            logs[(topic.split(".")[1], str(msg.get("msg", ""))[:110])] += 1
    for (level, text), n in logs.most_common(6):
        out(f"  pupil {level} x{n}: {text}")

    dots = per_dot(gaze, dot_windows(meta, events))
    saved = (meta.get("correction") or {}).get("affine")
    out("\n== accuracy at the dots (median gaze per dot vs the dot) ==")
    out("phase    dots   raw error (mean / worst)          bias dx, dy     noise    with saved correction")
    for phase in [p["name"] for p in meta["phases"] if p["kind"] == "dots"]:
        rows = [r for r in dots if r[0] == phase]
        have = [r for r in rows if r[2] is not None]
        if not have:
            out(f"{phase:8} 0/{len(rows)}  no gaze")
            continue
        m = np.array([r[2] for r in have])
        t = np.array([r[1] for r in have])
        e = errors(m, t)
        bias = (m - t).mean(axis=0)
        noise = np.median([r[4] for r in have])
        corr = f"{fmt(errors(apply_affine(saved, m), t).mean())}" if saved else "none saved"
        out(f"{phase:8} {len(have):2}/{len(rows):<2} {fmt(e.mean())} / {fmt(e.max())}   "
            f"{bias[0]:+5.0f}, {bias[1]:+5.0f}   {noise:4.0f} pt   {corr}")

    windows = dot_windows(meta, events)
    if any(r[0] == "still" for r in dots):
        out("\n== each 'still' dot, left to right, top to bottom ==")
        out("   dot (x, y)      error    dx    dy   samples  eye0 conf  eye1 conf")
        rows = sorted(((r, w) for r, w in zip(dots, windows) if r[0] == "still"),
                      key=lambda rw: (round(rw[0][1][1]), rw[0][1][0]))
        for (phase, target, med, n, _), (_, _, _, t0, t1) in rows:
            confs = []
            for eye in (0, 1):
                a = pupils.get(eye)
                c = a[(a[:, 0] >= t0) & (a[:, 0] < t1), 1] if a is not None else []
                confs.append(f"{np.median(c):9.2f}" if len(c) else "        -")
            if med is None:
                out(f"({target[0]:5.0f}, {target[1]:4.0f})   no gaze      {n:5}  " + "  ".join(confs))
            else:
                dx, dy = med[0] - target[0], med[1] - target[1]
                out(f"({target[0]:5.0f}, {target[1]:4.0f})  {math.hypot(dx, dy):5.0f}  {dx:+5.0f} {dy:+5.0f}  "
                    f"{n:5}   " + "  ".join(confs))

    still = [r for r in dots if r[0] == "still" and r[2] is not None]
    if len(still) >= 6:
        m = np.array([r[2] for r in still])
        t = np.array([r[1] for r in still])
        out("\n== would a correction fitted on 'still' help? (mean error) ==")
        held = {ph: [r for r in dots if r[0] == ph and r[2] is not None] for ph in ("end", "head")}
        out("model       still (leave-one-out)   end            head")
        for kind in ("none", "offset", "affine", "quadratic"):
            if kind == "none":
                loo = errors(m, t)
            else:
                loo = []
                for k in range(len(m)):
                    keep = np.arange(len(m)) != k
                    loo.append(errors(fit(kind, m[keep], t[keep])(m[k:k + 1]), t[k:k + 1])[0])
                loo = np.array(loo)
            f = (lambda xy: xy) if kind == "none" else fit(kind, m, t)
            cols = [fmt(loo.mean())]
            for ph in ("end", "head"):
                rows = held[ph]
                cols.append(fmt(errors(f(np.array([r[2] for r in rows])), np.array([r[1] for r in rows])).mean())
                            if rows else "   -")
            out(f"{kind:10}  {cols[0]:22}  {cols[1]:13}  {cols[2]}")

    pursuit = [(e["t"], e["target"]) for e in events if e.get("phase") == "pursuit" and "target" in e]
    good = gaze[gaze[:, 3] >= MIN_CONFIDENCE] if len(gaze) else gaze
    if len(pursuit) > 30 and len(good) > 30:
        pt = np.array([p[0] for p in pursuit])
        pxy = np.array([p[1] for p in pursuit])
        inside = good[(good[:, 0] > pt[0] + 1.0) & (good[:, 0] < pt[-1] - 0.6)]  # skip the first second
        if len(inside) > 30:
            best = None
            for lag in np.arange(-0.2, 0.61, 0.02):
                tx = np.interp(inside[:, 0] - lag, pt, pxy[:, 0])
                ty = np.interp(inside[:, 0] - lag, pt, pxy[:, 1])
                e = np.median(np.hypot(inside[:, 1] - tx, inside[:, 2] - ty))
                if best is None or e < best[1]:
                    best = (lag, e)
            tx = np.interp(inside[:, 0], pt, pxy[:, 0])
            ty = np.interp(inside[:, 0], pt, pxy[:, 1])
            e0 = np.median(np.hypot(inside[:, 1] - tx, inside[:, 2] - ty))
            out("\n== pursuit (moving dot) ==")
            out(f"median error {fmt(e0)}; gaze lags the dot by ~{best[0] * 1000:.0f} ms "
                f"(error {fmt(best[1])} once shifted)")
    return dots


def plot(meta, events, messages, dots, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    gaze = screen_gaze(meta, messages)
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    w, h = meta["screen"][2], meta["screen"][3]
    colors = {"still": "tab:blue", "head": "tab:orange", "end": "tab:green"}
    ax = axes[0]
    for phase, target, med, _, _ in dots:
        ax.plot(*target, "o", color="red", ms=5)
        if med is not None:
            ax.annotate("", xy=med, xytext=target, arrowprops=dict(arrowstyle="->", color=colors.get(phase, "k")))
    for name, c in colors.items():
        ax.plot([], [], color=c, label=name)
    left, top, right, bottom = meta["span"]
    ax.add_patch(plt.Rectangle((left, top), right - left, bottom - top, fill=False, ls="--", color="grey"))
    ax.set(xlim=(0, w), ylim=(h, 0), aspect="equal", title="dot (red) -> median gaze")
    ax.legend()
    ax = axes[1]
    pursuit = np.array([e["target"] for e in events if e.get("phase") == "pursuit" and "target" in e])
    pt = np.array([e["t"] for e in events if e.get("phase") == "pursuit" and "target" in e])
    if len(pursuit) and len(gaze):
        g = gaze[(gaze[:, 0] >= pt[0]) & (gaze[:, 0] <= pt[-1]) & (gaze[:, 3] >= MIN_CONFIDENCE)]
        ax.plot(pursuit[:, 0], pursuit[:, 1], color="red", lw=1, label="dot")
        ax.plot(g[:, 1], g[:, 2], ".", ms=1.5, color="tab:blue", label="gaze")
        ax.legend()
    ax.set(xlim=(0, w), ylim=(h, 0), aspect="equal", title="pursuit")
    fig.tight_layout()
    fig.savefig(path, dpi=110)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("session", help="recordings/<date_time> folder")
    p.add_argument("--distance-cm", type=float, default=50.0, help="eye to screen distance")
    p.add_argument("--plot", action="store_true", help="save analysis.png in the session folder")
    a = p.parse_args()
    meta, events, messages = load(a.session)
    dots = report(meta, events, messages, a.distance_cm)
    if a.plot:
        path = Path(a.session) / "analysis.png"
        plot(meta, events, messages, dots, path)
        print(f"\nsaved {path}")


if __name__ == "__main__":
    sys.exit(main())
