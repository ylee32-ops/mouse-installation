#!/usr/bin/env python3
"""
Turn folders of stop-motion photos into the clips the installation plays.

The important trick here is that each character's out, loop and in sequences are
encoded into a SINGLE continuous file, with the boundaries between them recorded
in build/segments.json. At runtime the mouse comes out, loops and goes back in
without mpv ever loading a file — those transitions are just playback, so there
is no gap at all. Only base -> character and character -> base are real loads.

Also built:
  * <character>__rewind.mp4 — the out sequence reversed, for 1 - 1R - 0
  * base, loading and the easter eggs, one file each

    python3 build_scenes.py            # build what changed
    python3 build_scenes.py --force    # rebuild everything
    python3 build_scenes.py --check    # validate scenes.json, encode nothing

Needs ffmpeg:  sudo apt install ffmpeg
"""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "scenes.json")
BUILD = os.path.join(HERE, "build")
FRAMES_ROOT = HERE

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG")


# --- helpers -----------------------------------------------------------------

def natural_key(name):
    """Sort frame_2.jpg before frame_10.jpg (plain sorting gets this wrong)."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def list_frames(folder):
    path = folder if os.path.isabs(folder) else os.path.join(FRAMES_ROOT, folder)
    if not os.path.isdir(path):
        return None, path
    names = [n for n in os.listdir(path) if n.endswith(IMAGE_EXT) and not n.startswith(".")]
    names.sort(key=natural_key)
    return [os.path.join(path, n) for n in names], path


def ordered(files, order):
    if order == "reverse":
        return list(reversed(files))
    if order == "pingpong":
        # Don't repeat the two turnaround frames, or the loop visibly stutters.
        return files + list(reversed(files[1:-1])) if len(files) > 2 else files
    return files


def output_fps(fps_values):
    """One frame rate for the whole file. Segments can be shot at different
    rates, so use the lowest common multiple — then every source frame maps to a
    whole number of output frames and nothing judders."""
    ints = [int(round(f)) for f in fps_values]
    if any(abs(f - round(f)) > 1e-6 for f in fps_values):
        return 60
    out = 1
    for f in ints:
        out = out * f // math.gcd(out, f)
        if out > 240:
            return 60
    return max(out, 1)


def fingerprint(segments, width, height, fit, fps_out):
    h = hashlib.sha256()
    h.update(f"{width}x{height}|{fit}|{fps_out}|v6-exactframes".encode())
    for files, fps in segments:
        h.update(f"|seg@{fps}|".encode())
        for f in files:
            st = os.stat(f)
            h.update(f"{os.path.basename(f)}|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()


def encode(segments, out_path, width, height, fit, fps_out):
    """Encode segments — each its own list of photos at its own fps — into one
    constant-rate mp4."""
    list_path = out_path + ".txt"
    last = None
    step = 1.0 / float(fps_out)
    with open(list_path, "w") as f:
        for files, fps in segments:
            # Write each photo once per OUTPUT frame it should occupy, all with
            # the same duration. Writing one entry of 1/fps per photo instead
            # let ffmpeg accumulate rounding error across the list, and a
            # segment could come out a frame short — which put a frame of the
            # in sequence inside the loop. With uniform entries the frame count
            # is exact by construction.
            repeat = max(1, int(round(fps_out / float(fps))))
            for img in files:
                for _ in range(repeat):
                    f.write("file '%s'\n" % img.replace("'", r"'\''"))
                    f.write("duration %.6f\n" % step)
                last = img
        # The concat demuxer drops the final entry's duration, so repeat the
        # last image to make sure it is actually held on screen.
        f.write("file '%s'\n" % last.replace("'", r"'\''"))

    if fit == "crop":
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height}")
    else:
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-vf", vf, "-r", str(fps_out),
        # All-intra: every frame a keyframe, so seeking and the first frame
        # after a load are both immediate.
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-g", "1", "-bf", "0",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-an", out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(list_path)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg failed for {out_path}:\n{result.stderr.strip()}")


TINY = "scale=16:16,format=gray"


def _thumbs(path, lo, hi, pre=""):
    """16x16 grey thumbnails of frames lo..hi, as a list of 256-byte blobs."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf",
         f"{pre}select='between(n\\,{lo}\\,{hi})',{TINY}",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
        capture_output=True)
    d = r.stdout
    return [d[i*256:(i+1)*256] for i in range(len(d)//256)]


def _photo_thumb(path, pad):
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", pad + TINY,
         "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True)
    return r.stdout[:256]


def _dist(a, b):
    if not a or not b or len(a) != len(b):
        return 9999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def locate_boundary(main, photo, predicted, pad, window=5):
    """The real frame index where `photo` first appears.

    Predicting this arithmetically does not survive contact with ffmpeg: the
    concat demuxer can emit an extra frame at the head of the file, and
    accumulated durations can lose one in the middle. Both put the loop points
    on the wrong image, which in stop-motion is immediately visible. So search
    for the frame that IS the photo instead of calculating where it ought to be.
    """
    target = _photo_thumb(photo, pad)
    if not target:
        return predicted
    lo = max(0, predicted - window)
    frames = _thumbs(main, lo, predicted + window)
    if not frames:
        return predicted
    dists = [_dist(f, target) for f in frames]
    best = min(dists)
    # the photo spans several output frames; we want the first of them
    for i, d in enumerate(dists):
        if d <= best + 1.0:
            return lo + i
    return predicted


def frame_times(path):
    """Every frame's real presentation time, straight from the encoded file.

    Computing the boundary as frames/fps assumes ffmpeg lays timestamps out
    exactly as predicted. It does not always, and being one frame out puts the
    last frame of the out sequence at the head of every loop cycle — which in
    stop-motion is glaring. So read the truth instead of predicting it.
    """
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=best_effort_timestamp_time",
         "-of", "csv=p=0", path],
        capture_output=True, text=True)
    times = []
    for line in r.stdout.splitlines():
        line = line.strip().rstrip(",")
        if not line or line == "N/A":
            continue
        try:
            times.append(float(line))
        except ValueError:
            pass
    return sorted(times)


# --- build plan --------------------------------------------------------------

def resolve(seq, label, problems, force_order=None):
    """(files, fps, seconds) for one sequence, or None."""
    if not seq:
        return None
    files, path = list_frames(seq["frames"])
    if files is None:
        problems.append(f"{label}: folder not found -> {path}"); return None
    if not files:
        problems.append(f"{label}: no images in {path}"); return None

    order = force_order or seq.get("mode", "loop")
    order = "pingpong" if order == "pingpong" else ("reverse" if order == "reverse" else "forward")
    fps = float(seq.get("fps", 10))
    frames = ordered(files, order)

    min_seconds = float(seq.get("min_seconds", 0) or 0)
    if min_seconds and len(frames) / fps < min_seconds:
        need = int(round(min_seconds * fps))
        frames = (frames * (need // len(frames) + 1))[:max(need, len(frames))]

    return frames, fps, len(frames) / fps


def single(seq, name, jobs, problems, force_order=None):
    r = resolve(seq, name, problems, force_order)
    if not r:
        return None
    files, fps, secs = r
    jobs.append({"name": name, "segments": [(files, fps)], "seconds": secs,
                 "note": "" if not force_order else f"[{force_order}]",
                 "counts": [len(files)]})
    return name


def build_plan(cfg):
    """
        0 base    -> base           4 easter  -> easter_<name>
        5 loading -> loading
        1+2+3     -> <char>__main   one continuous file, boundaries recorded
        1R        -> <char>__rewind
    """
    jobs, problems, segments = [], [], {}

    if not single(cfg.get("base"), "base", jobs, problems):
        problems.append("no base image (0) — add frames/base/")
    single(cfg.get("loading"), "loading", jobs, problems)

    for egg in (cfg.get("easter_eggs") or {}).get("sequences") or []:
        if not egg.get("name"):
            problems.append('an easter egg has no "name"'); continue
        single(egg, "easter_" + egg["name"], jobs, problems)

    names = set()
    for ch in cfg.get("characters", []):
        name = ch.get("name")
        if not name:
            problems.append('a character has no "name"'); continue
        if name in names:
            problems.append(f"duplicate character name: {name}")
        names.add(name)

        out = resolve(ch.get("out"), f"{name}/out", problems)
        loop = resolve(ch.get("loop"), f"{name}/loop", problems)
        inn = resolve(ch.get("in"), f"{name}/in", problems)

        if not out:
            problems.append(f'{name}: needs an "out" sequence (1)'); continue
        if not loop:
            problems.append(f'{name}: needs a "loop" sequence (2)'); continue

        # out + loop + in, back to back, in one file.
        parts = [out, loop] + ([inn] if inn else [])
        jobs.append({
            "name": f"{name}__main",
            "segments": [(f, fps) for f, fps, _ in parts],
            "seconds": sum(s for _, _, s in parts),
            "counts": [len(f) for f, _, _ in parts],
            "note": "[out+loop" + ("+in]" if inn else "]"),
        })
        segments[name] = {
            "out_end":  round(out[2], 6),
            "loop_end": round(out[2] + loop[2], 6),
            "total":    round(sum(s for _, _, s in parts), 6),
            "has_in":   bool(inn),
            # The player nudges the loop points half a frame inward using this,
            # so a seek landing on an exact frame boundary can't pick up the
            # last frame of the out sequence.
            "fps_out":  output_fps([fps for _, fps, _ in parts]),
            # how many output frames each segment occupies, for indexing
            "_counts": [len(f) for f, _, _ in parts],
            "_fps":    [fps for _, fps, _ in parts],
            "_first_loop": loop[0][0],
            "_first_in":   inn[0][0] if inn else None,
        }

        # 1R — always built. Sound during the out sequence rewinds rather than
        # cutting to the in sequence.
        single(ch["out"], f"{name}__rewind", jobs, problems, force_order="reverse")

    return jobs, problems, segments


# --- main --------------------------------------------------------------------

def main():
    force = "--force" in sys.argv
    check_only = "--check" in sys.argv

    if not os.path.exists(CONFIG):
        raise SystemExit(f"Missing {CONFIG}")
    with open(CONFIG) as f:
        cfg = json.load(f)

    global FRAMES_ROOT
    root = cfg.get("frames_root")
    if root:
        FRAMES_ROOT = os.path.expanduser(root)
        if not os.path.isdir(FRAMES_ROOT):
            raise SystemExit(f"frames_root does not exist: {FRAMES_ROOT}\n"
                             "Is the drive plugged in?")
        print(f"reading photos from {FRAMES_ROOT}\n")

    display = cfg.get("display", {})
    width, height = int(display.get("width", 1920)), int(display.get("height", 1080))
    fit = display.get("fit", "pad")
    if fit not in ("pad", "crop"):
        raise SystemExit(f'display.fit must be "pad" or "crop", not {fit!r}')

    jobs, problems, segments = build_plan(cfg)

    if problems:
        print("Problems in scenes.json:\n")
        for p in problems:
            print(f"  - {p}")
        print()
        if not jobs:
            raise SystemExit(1)
        print("Building what is OK anyway.\n")

    note = ("fit=crop — filling the screen, top and bottom cropped off" if fit == "crop"
            else "fit=pad — whole frame kept, black bars where the aspect differs")
    print(f"{len(jobs)} file(s) at {width}x{height} · {note}:\n")
    for j in jobs:
        fps_out = output_fps([fps for _, fps in j["segments"]])
        print(f"  {j['name']:<26} {'+'.join(str(c) for c in j['counts']):>10} photos "
              f"= {j['seconds']:5.2f}s @ {fps_out}fps  {j['note']}")
    if segments:
        print("\nsegment boundaries (seconds into each __main file):")
        for n, s in segments.items():
            print(f"  {n:<14} out 0–{s['out_end']:.2f} · loop {s['out_end']:.2f}–"
                  f"{s['loop_end']:.2f} · in {s['loop_end']:.2f}–{s['total']:.2f}")
    print()

    if check_only:
        print("--check: nothing encoded.")
        return
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found. Install it: sudo apt install ffmpeg")

    os.makedirs(BUILD, exist_ok=True)
    manifest_path = os.path.join(BUILD, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path) and not force:
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

    built = skipped = 0
    for j in jobs:
        outp = os.path.join(BUILD, j["name"] + ".mp4")
        fps_out = output_fps([fps for _, fps in j["segments"]])
        fp = fingerprint(j["segments"], width, height, fit, fps_out)
        if not force and manifest.get(j["name"]) == fp and os.path.exists(outp):
            skipped += 1
            continue
        print(f"  encoding {j['name']} ...", flush=True)
        encode(j["segments"], outp, width, height, fit, fps_out)
        manifest[j["name"]] = fp
        built += 1

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    # Pin the loop boundaries to actual frame timestamps in each built file.
    for name, seg in segments.items():
        main = os.path.join(BUILD, name + "__main.mp4")
        counts, fpss, fps_out = seg.pop("_counts"), seg.pop("_fps"), seg["fps_out"]
        if not os.path.exists(main):
            continue
        times = frame_times(main)
        # output-frame index at which each segment begins
        idx, starts = 0, []
        for n, fps in zip(counts, fpss):
            starts.append(idx)
            idx += n * max(1, int(round(fps_out / float(fps))))
        loop_i = starts[1] if len(starts) > 1 else 0
        in_i = starts[2] if len(starts) > 2 else idx
        pad = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,")
        first_loop, first_in = seg.pop("_first_loop"), seg.pop("_first_in")
        guess_loop, guess_in = loop_i, in_i
        if first_loop:
            loop_i = locate_boundary(main, first_loop, loop_i, pad)
        if first_in:
            in_i = locate_boundary(main, first_in, in_i, pad)
        shift = f"  (predicted {guess_loop}/{guess_in})" if (loop_i, in_i) != (guess_loop, guess_in) else ""
        if loop_i < len(times):
            seg["loop_a"] = round(times[loop_i], 6)
        if in_i < len(times):
            seg["loop_b"] = round(times[in_i], 6)
        else:
            seg["loop_b"] = round(times[-1], 6)
        seg["frames_total"] = len(times)
        seg["loop_frames"] = [loop_i, in_i]
        warn = ""
        # the encoder should now produce exactly idx frames plus the repeated
        # tail frame the concat demuxer needs; anything else means drift is back
        if not (idx <= len(times) <= idx + max(1, int(round(fps_out / fpss[-1]))) + 1):
            warn = f"   !! expected ~{idx} frames, got {len(times)} — boundaries suspect"
        print(f"  {name}: loop frames {loop_i}..{in_i - 1} of {len(times)}  "
              f"-> {seg.get('loop_a')}s .. {seg.get('loop_b')}s{shift}{warn}")

    with open(os.path.join(BUILD, "segments.json"), "w") as f:
        json.dump(segments, f, indent=2)

    print(f"\nDone. {built} built, {skipped} unchanged. Clips are in build/")


if __name__ == "__main__":
    main()
