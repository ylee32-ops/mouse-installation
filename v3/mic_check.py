#!/usr/bin/env python3
"""
Live microphone meter, using the EXACT same logic as the installation.

Run it, then make the sounds you actually care about — a word from the doorway,
a mug on the counter — and watch whether they cross the line.

    python3 mic_check.py

    band    level in the speech band (300-3000Hz) — what actually decides
    raw     the full-spectrum level, for comparison
    floor   the room's resting level (20th percentile of the last 30s)
    thr     where "sound" begins right now
    peak    the loudest level seen since it started
    TRIGGER printed when the debounce actually fires — this, not the bar,
            is what sends the mouse back in

Ctrl+C to stop. Nothing is recorded.
"""
import json, os, sys, time

sys.argv = [sys.argv[0]]                      # keep mouse_video's flags out of it
from mouse_video import Mic, CONFIG

cfg = json.load(open(CONFIG)).get("audio", {})
mic = Mic(cfg)
mic.start()
time.sleep(0.4)

print(f"device={mic.device}  auto_threshold={mic.auto}  noise_margin={mic.margin}  "
      f"hits={mic.hits_needed}/{mic.hit_window}s  band={mic.band}\n")
print("watch the x column: how many times above the floor the sound is.")
print("above ~2.5x is comfortably detectable; near 1.0x is indistinguishable.\n")

peak, triggers, t0 = 0.0, 0, time.time()
try:
    while True:
        level, status = mic.snapshot()
        raw = mic.level_raw
        if status != "ok":
            print(f"\rmic {status}", end="", flush=True); time.sleep(0.5); continue
        thr = mic.threshold()
        with mic.lock:
            floor = sorted(mic.floor_samples)[len(mic.floor_samples)//5] if len(mic.floor_samples) >= 40 else 0.0
        peak = max(peak, level)
        fired = mic.is_loud(thr)
        if fired:
            triggers += 1

        width = 34
        bar = ["·"] * width
        scale = max(thr * 3, 0.02)
        mark = min(width-1, int(level / scale * width))
        line = min(width-1, int(thr / scale * width))
        for i in range(mark + 1):
            bar[i] = "#"
        bar[line] = "|"
        ratio = level / floor if floor > 0 else 0
        print(f"\r[{''.join(bar)}] band={level:.4f} raw={raw:.4f} floor={floor:.4f} "
              f"thr={thr:.4f} x{ratio:4.1f} peak={peak:.4f} fired={triggers}  "
              + ("<<< TRIGGER" if fired else "           "), end="", flush=True)
        time.sleep(0.05)
except KeyboardInterrupt:
    dt = time.time() - t0
    print(f"\n\nover {dt:.0f}s: peak {peak:.4f}, threshold ended at {mic.threshold():.4f}, "
          f"{triggers} trigger reads")
    print("\nIf your test sounds never printed TRIGGER, the threshold is too high:")
    print("  lower audio.noise_margin (3.0 -> 2.2), or audio.min_threshold")
    print("If it triggers when the room is still, it is too low: raise noise_margin.")
