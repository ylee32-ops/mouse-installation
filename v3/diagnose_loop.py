#!/usr/bin/env python3
"""
Work out which SOURCE PHOTO each frame of a built clip actually is.

Nothing is transferred: each frame is shrunk to a 16x16 grey thumbnail and
compared against the source photos, so the answer is a few lines of text.

    python3 diagnose_loop.py            # first character in scenes.json
    python3 diagnose_loop.py taro
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")

cfg = json.load(open(os.path.join(HERE, "scenes.json")))
segs = json.load(open(os.path.join(BUILD, "segments.json")))
root = os.path.expanduser(cfg.get("frames_root") or HERE)

name = sys.argv[1] if len(sys.argv) > 1 else cfg["characters"][0]["name"]
ch = next(c for c in cfg["characters"] if c["name"] == name)
seg = segs[name]
W = int(cfg["display"]["width"]); H = int(cfg["display"]["height"])
PAD = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
       f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,")
TINY = "scale=16:16,format=gray"


def run(inp, vf, extra=()):
    """ONE -vf only. Passing it twice makes ffmpeg ignore the first, which is
    how the earlier version of this script silently read frame 0 every time."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", inp, *extra,
         "-vf", vf, "-fps_mode", "passthrough", "-frames:v", "1",
         "-f", "rawvideo", "-"],
        capture_output=True)
    return r.stdout


def frame_of(path, n):
    # seek near the frame first so this stays fast on later frames
    return run(path, f"select='eq(n\\,{n})',{TINY}")


def dist(a, b):
    if not a or not b or len(a) != len(b):
        return 9999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


sources = []
for key in ("out", "loop", "in"):
    s = ch.get(key)
    if not s:
        continue
    d = os.path.join(root, s["frames"])
    for f in sorted(os.listdir(d)):
        if f.lower().endswith(".jpg") and not f.startswith("."):
            sources.append((f"{key}/{f}", os.path.join(d, f)))

loop_i, in_i = seg["loop_frames"]
print(f"{name}: {len(sources)} source photos · loop frames "
      f"{loop_i}..{in_i-1} of {seg['frames_total']}\n")
print("fingerprinting sources ...", flush=True)
src = [(lbl, run(p, PAD + TINY)) for lbl, p in sources]

main = os.path.join(BUILD, f"{name}__main.mp4")
check = sorted(set(list(range(max(0, loop_i - 2), loop_i + 3)) +
                   list(range(in_i - 2, in_i + 3))))

print("\nbuilt frame -> the source photo it actually is:\n")
seen = {}
for n in check:
    t = frame_of(main, n)
    seen[n] = t
    if not t:
        print(f"  frame {n:>4}  ->  (could not read)"); continue
    best = min(src, key=lambda s: dist(t, s[1]))
    mark = ("   <<< LOOP SHOULD START HERE" if n == loop_i else
            "   <<< IN SHOULD START HERE" if n == in_i else "")
    print(f"  frame {n:>4}  ->  {best[0]:<16} (diff {dist(t, best[1]):5.1f}){mark}")

vals = [v for v in seen.values() if v]
if len(vals) > 1 and all(v == vals[0] for v in vals):
    print("\n!! every frame came back identical — the extraction is still wrong,"
          "\n   not the clip. Tell Claude; do not act on the result above.")
